"""yt-dlp 封装 + 快手 SSR 解析:多平台 URL(YouTube/抖音/小红书/快手/B站)解析与音频下载。

- B站 / YouTube / 抖音 / 小红书:走 yt-dlp 原生提取器
- 快手:yt-dlp 无内置提取器,走分享页 SSR 解析(见 app.kuaishou)
- 微信视频号:无公开网页解析通道(需微信登录 + 接口加密),拦截并引导本地上传
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yt_dlp

from app import ffmpeg_bin

ProgressCallback = Callable[[float], None]

# 各平台视频 id 前缀(写入 bv 字段,保证输出目录/搜索跨平台唯一)
PLATFORM_PREFIX = {
    "bilibili": "",
    "youtube": "yt_",
    "douyin": "dy_",
    "xiaohongshu": "xhs_",
    "kuaishou": "ks_",
}

# yt-dlp cookiesfrombrowser 支持的浏览器(传入非法名会让 YoutubeDL 构造直接抛错)
SUPPORTED_BROWSERS = ("chrome", "chromium", "edge", "firefox", "brave", "opera", "vivaldi", "whale")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 微信视频号相关域名
_WECHAT_HOSTS = {"channels.weixin.qq.com"}

# 抖音精选/搜索/个人页弹窗链接:...douyin.com/<path>?...modal_id=<视频id>
_DOUYIN_MODAL_RE = re.compile(
    r"^https?://(?:[\w-]+\.)*douyin\.com/[^\s?#]*\?(?:[^#]*&)?modal_id=(\d+)", re.IGNORECASE
)
# 重定向结果里提取抖音视频 id(iesdouyin 分享页 / douyin 正式页两种形态)
_DOUYIN_ID_RE = re.compile(r"(?:iesdouyin\.com/share/video|douyin\.com/video)/(\d+)")


@dataclass
class MediaInfo:
    """一个待处理的最小单元(单 P 视频或合集中的一个条目)。"""

    media_id: str  # yt-dlp 条目 id,多 P 形如 BV1xx411c7mD_p2,唯一
    bv: str
    title: str
    uploader: str
    duration: float  # 秒
    url: str  # 该条目的网页地址
    part: int = 1  # 第几 P
    total_parts: int = 1
    playlist_title: str = ""  # 合集/多 P 的整体标题(决定输出目录)
    audio_path: Path | None = field(default=None, repr=False)
    platform: str = "bilibili"  # bilibili / youtube / douyin / xiaohongshu / kuaishou
    direct_url: str = ""  # 快手等无 yt-dlp 提取器平台:分享页解析出的直链


def _bilibili_id(raw_id: str) -> str:
    """yt-dlp 对多 P 的 id 形如 'BV1xx411c7mD_p2',BV 号取下划线前部分。"""
    return raw_id.split("_")[0]


def _detect_platform(url: str) -> str:
    """按域名识别平台;未知平台按 bilibili 处理(兼容旧行为)。"""
    host = url.lower()
    if "kuaishou.com" in host or "gifshow.com" in host:
        return "kuaishou"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "douyin.com" in host or "iesdouyin.com" in host:
        return "douyin"
    if "xiaohongshu.com" in host or "xhslink.com" in host:
        return "xiaohongshu"
    return "bilibili"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止 urllib 自动跟随重定向:每一跳都先过协议/域名/解析 IP 校验。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# 允许发起短链解析的入口域名(硬编码白名单,不接受任意目标)
_SHORT_LINK_ENTRY_HOSTS = {
    "v.douyin.com", "iesdouyin.com", "www.iesdouyin.com", "xhslink.com", "www.xhslink.com",
}
# 重定向链路与最终地址允许到达的平台域名(后缀匹配,含子域)
_PLATFORM_HOST_SUFFIXES = (
    "douyin.com", "iesdouyin.com", "xiaohongshu.com", "xhslink.com",
    "bilibili.com", "b23.tv", "youtube.com", "youtu.be", "kuaishou.com", "gifshow.com",
)


def _host_allowed(host: str, suffixes: tuple[str, ...] | set[str]) -> bool:
    host = (host or "").strip().strip("[]").lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _host_ip_safe(host: str) -> bool:
    """域名解析出的所有 IP 均不得是本机/内网/链路本地/保留地址(防 DNS rebinding)。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


def _hop_allowed(url: str) -> bool:
    """单跳 URL 是否允许发起请求:仅 http/https、平台域名白名单、解析 IP 非内网。"""
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").strip().strip("[]").lower().rstrip(".")
    if not host or not _host_allowed(host, _PLATFORM_HOST_SUFFIXES):
        return False
    return _host_ip_safe(host)


def _resolve_redirect(url: str, timeout: int = 10, max_hops: int = 5) -> str:
    """跟随短链重定向取最终地址。

    入口仅限硬编码短链域名;每一跳校验协议/域名/解析后 IP,最多 max_hops 跳。
    任何失败返回空串,调用方回退原地址交给 yt-dlp。用 GET 而非 HEAD:
    部分短链服务不支持 HEAD(405)。
    """
    entry = (urllib.parse.urlparse(url).hostname or "").strip().strip("[]").lower().rstrip(".")
    if entry not in _SHORT_LINK_ENTRY_HOSTS:
        return ""
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    for _ in range(max_hops):
        if not _hop_allowed(current):
            return ""
        req = urllib.request.Request(current, headers={"User-Agent": _UA})
        try:
            with opener.open(req, timeout=timeout):  # 不读正文,拿到响应即关
                return current  # 2xx:无更多重定向,即最终地址
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                return ""
            loc = exc.headers.get("Location") or ""
            if not loc:
                return ""
            current = urllib.parse.urljoin(current, loc)
        except Exception:  # noqa: BLE001 — 网络失败统一回退原地址
            return ""
    return ""  # 超过跳数上限(疑似重定向循环)


def normalize_url(url: str) -> str:
    """把各平台的非标准分享链接规范化为 yt-dlp 可识别的地址。

    - 抖音精选/搜索/个人页弹窗:douyin.com/*?modal_id=N -> douyin.com/video/N
      (yt-dlp 的 DouyinIE 只匹配 /video/<id> 路径)
    - 抖音/小红书短链(v.douyin.com / iesdouyin / xhslink.com):跟随重定向
      拿真实地址;iesdouyin 分享页再改写为 douyin.com/video/N
    - 微信视频号:直接抛错引导走本地上传(网页需微信登录且接口加密)
    """
    url = url.strip()
    host = (urllib.parse.urlparse(url).hostname or "").strip().strip("[]").lower().rstrip(".")
    if host in _WECHAT_HOSTS:
        raise DownloadError(
            "微信视频号暂不支持直接解析(网页版需微信扫码登录且接口加密,无公开视频地址)。"
            "两个办法拿到视频后用「上传本地视频 / 音频」转写:\n"
            "1. 手机微信打开该视频 → 右上角「…」→ 保存到手机 → 传到电脑;\n"
            "2. 电脑微信全屏播放该视频,用录屏软件(如 Xbox Game Bar,Win+G)录制后上传。"
        )
    m = _DOUYIN_MODAL_RE.match(url)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    if host in _SHORT_LINK_ENTRY_HOSTS:
        resolved = _resolve_redirect(url)
        if resolved:
            m2 = _DOUYIN_ID_RE.search(resolved)
            if m2:  # iesdouyin 分享页改写为正式页,确保命中 DouyinIE
                return f"https://www.douyin.com/video/{m2.group(1)}"
            return resolved
    return url


class DownloadError(RuntimeError):
    pass


# ---------- 连通性预检 ----------
# 解析前快速探测平台可达性:yt-dlp 对不可达网络会按 socket_timeout×retries×
# 多个 API 端点依次超时(单任务 2-3 分钟才报错),2 个 worker 全被堵死后,
# 后续正常任务(如 B 站)一直排队——这正是"B站解析好久下载不下来"的根因。
# 预检用一次轻量 HTTPS 请求(约 1-8 秒)快速失败,立即释放 worker。
PROBE_HOSTS = {
    "bilibili": "https://www.bilibili.com/",
    "youtube": "https://www.youtube.com/",
    "douyin": "https://www.douyin.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/",
    "kuaishou": "https://www.kuaishou.com/",
}

_reach_cache: dict[str, tuple[float, bool]] = {}  # platform -> (monotonic_ts, ok)
_REACH_TTL = 60.0  # 秒:预检结果缓存时长,避免合集多 P 反复探测


def _reachable_once(url: str, proxy: str, timeout: float = 5.0) -> bool:
    """单次 HTTPS 可达性探测。

    HTTPError(哪怕 4xx/5xx)说明服务器有响应、网络通,视为可达;
    只有连接层失败(超时/DNS/拒绝)才判不可达。socks 代理 urllib 不支持,
    跳过探测直接交给 yt-dlp(其自带 socks 支持)。
    """
    if (proxy or "").lower().startswith("socks"):
        return True
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with opener.open(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001 — 连接层任何失败都视为不可达
        return False


def ensure_reachable(platform: str, proxy: str = "") -> None:
    """平台连通性预检:不可达立即抛 DownloadError(含代理指引),不进 yt-dlp。"""
    url = PROBE_HOSTS.get(platform)
    if not url:
        return
    now = time.monotonic()
    cached = _reach_cache.get(platform)
    if cached and now - cached[0] < _REACH_TTL:
        ok = cached[1]
    else:
        ok = _reachable_once(url, proxy) or _reachable_once(url, proxy)
        _reach_cache[platform] = (now, ok)
    if ok:
        return
    host = urllib.parse.urlparse(url).hostname or url
    hint = "(YouTube 等境外平台需在设置页配置网络代理)" if platform == "youtube" else ""
    raise DownloadError(f"无法连接 {host},请检查网络连接{hint}")


class Downloader:
    """串行使用;每个实例内部加锁,便于多线程共享。"""

    def __init__(self, ydl_factory: Callable[[dict], "yt_dlp.YoutubeDL"] | None = None,
                 cookies_from_browser: str = "", cookie_file: str = "", proxy: str = ""):
        # ydl_factory 仅供测试注入 mock;生产走 yt_dlp.YoutubeDL
        self._ydl_factory = ydl_factory or (lambda opts: yt_dlp.YoutubeDL(opts))
        self._lock = threading.Lock()
        self._cookies_from_browser = ""
        self._cookie_file = ""
        self._proxy = ""
        self.configure(cookies_from_browser, cookie_file, proxy)

    def configure(self, cookies_from_browser: str = "", cookie_file: str = "",
                  proxy: str = "") -> None:
        """更新 Cookie/代理配置(设置页保存后由 Pipeline.apply_settings 调用)。

        cookie_file 优先于浏览器 Cookie;浏览器名不在 yt-dlp 支持列表时忽略,
        避免非法名让每次 YoutubeDL 构造都抛错。代理形如
        http://127.0.0.1:7890 或 socks5://127.0.0.1:1080(YouTube 等境外平台需要)。
        """
        self._cookies_from_browser = (cookies_from_browser or "").strip().lower()
        self._cookie_file = (cookie_file or "").strip()
        self._proxy = (proxy or "").strip()
        _reach_cache.clear()  # 代理变更后重新探测

    def _cookie_opts(self) -> dict:
        opts: dict = {}
        if self._cookie_file:
            opts["cookiefile"] = self._cookie_file
        elif self._cookies_from_browser in SUPPORTED_BROWSERS:
            opts["cookiesfrombrowser"] = (self._cookies_from_browser, None, None, None)
        return opts

    def _yt_opts(self, **extra) -> dict:
        """yt-dlp 公共参数:静默 + 快速超时 + Cookie/代理注入 + ffmpeg 指路。

        解析阶段超时/重试从严(socket_timeout 20 / retries 2):预检已把不可达
        网络挡在门外,剩下的是偶发抖动,快速失败优于长时间挂起堵 worker。
        下载阶段可通过 extra 覆盖为宽松值。

        PATH 上没有 ffmpeg 时必须把随包静态版指给 yt-dlp(ffmpeg_location),
        否则讲义所需的分离视频/音频流无法合并,只能落到低清合一格式。
        """
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 20,
            "retries": 2,
        }
        if self._proxy:
            opts["proxy"] = self._proxy
        if ffmpeg_bin.using_bundled():
            opts["ffmpeg_location"] = ffmpeg_bin.resolve_ffmpeg()
        opts.update(self._cookie_opts())
        opts.update(extra)
        return opts

    def probe(self, url: str) -> list[MediaInfo]:
        """展开 URL 为待处理条目列表,不下载。"""
        platform = _detect_platform(url)
        ensure_reachable(platform, self._proxy)
        if platform == "kuaishou":
            from app import kuaishou
            return kuaishou.probe(url)
        url = normalize_url(url)
        opts = self._yt_opts(
            noplaylist=False,
            skip_download=True,
            extract_flat=False,
        )
        with self._lock:
            with self._ydl_factory(opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                except yt_dlp.utils.DownloadError as exc:
                    msg = f"解析链接失败: {exc}"
                    # 抖音/部分 YouTube 视频必须带 Cookie,给用户可操作的指引
                    if "cookie" in str(exc).lower() and not self._cookie_opts():
                        msg += "(该内容需要 Cookie:请在设置页配置浏览器 Cookie 或 cookies.txt 文件后重试)"
                    raise DownloadError(msg) from exc
        if info is None:
            raise DownloadError("未获取到视频信息")
        entries = list(info.get("entries") or []) if info.get("_type") == "playlist" else [info]
        # 合集/多 P 展开时个别条目可能拿不到完整信息,过滤掉
        entries = [e for e in entries if e and e.get("id")]
        if not entries:
            raise DownloadError("链接中没有可处理的视频")
        total = len(entries)
        playlist_title = str(info.get("title") or "") if info.get("_type") == "playlist" else ""
        result: list[MediaInfo] = []
        for idx, e in enumerate(entries, start=1):
            raw_id = str(e["id"])
            webpage = e.get("webpage_url") or url
            if platform == "bilibili":
                bv = _bilibili_id(raw_id)
                media_id = raw_id
            else:
                base_id = raw_id.split("_")[0]
                bv = f"{PLATFORM_PREFIX[platform]}{base_id}"
                media_id = raw_id
            result.append(
                MediaInfo(
                    media_id=media_id,
                    bv=bv,
                    title=e.get("title") or raw_id,
                    uploader=e.get("uploader") or e.get("channel") or "",
                    duration=float(e.get("duration") or 0.0),
                    url=webpage,
                    part=idx,
                    total_parts=total,
                    playlist_title=playlist_title,
                    platform=platform,
                )
            )
        return result

    def download_audio(self, info: MediaInfo, dest_dir: Path, progress: ProgressCallback | None = None,
                       want_video: bool = False) -> Path:
        """下载最佳媒体到 dest_dir,返回文件路径。

        want_video=False(默认):只下音频流(bestaudio)。
        want_video=True:下载含画面的格式(图文讲义用),音频转码阶段再从视频抽取;
        优先 ≤1080p 合一格式(无需 ffmpeg 合并),其次分离流合并(需外部 ffmpeg)。
        """
        if info.platform == "kuaishou":
            ensure_reachable("kuaishou", self._proxy)
            from app import kuaishou
            return kuaishou.download_audio(info, dest_dir, progress)  # 快手本就是合一 mp4
        ensure_reachable(info.platform, self._proxy)
        dest_dir.mkdir(parents=True, exist_ok=True)

        def hook(d: dict) -> None:
            if progress is None:
                return
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                if total:
                    progress(min(done / total, 1.0))

        outtmpl = str(dest_dir / f"{output_stem(info)}.%(ext)s")
        fmt = "bestaudio[ext=m4a]/bestaudio/best"
        extra: dict = {}
        if want_video:
            fmt = "b[height<=1080]/bv*[height<=1080]+ba/b"
            extra = {"merge_output_format": "mp4"}
        opts = self._yt_opts(
            format=fmt,
            outtmpl=outtmpl,
            noplaylist=True,
            overwrites=True,
            progress_hooks=[hook],
            nopart=True,
            # 网络健壮性:下载设超时与重试,避免僵死连接永久阻塞工作线程
            socket_timeout=30,
            retries=3,
            fragment_retries=3,
            retry_sleep=3,
            **extra,
        )
        with self._lock:
            with self._ydl_factory(opts) as ydl:
                try:
                    ydl.download([info.url])
                except yt_dlp.utils.DownloadError as exc:
                    raise DownloadError(f"下载音频失败: {exc}") from exc
        # outtmpl 用 %(ext)s,下载成功后按标题前缀找回文件
        stem = output_stem(info)
        candidates = [p for p in dest_dir.iterdir() if p.is_file() and p.stem == stem]
        # .mp4:部分平台(快手/小红书)无独立音轨,下载的是音视频合一的 mp4,转码阶段再抽音频
        audio_exts = {".m4a", ".opus", ".ogg", ".mp3", ".aac", ".webm", ".flac", ".mp4"}
        audio = [p for p in candidates if p.suffix.lower() in audio_exts]
        if not audio:
            raise DownloadError(f"下载完成但未找到音频文件: {dest_dir / stem}.*")
        path = max(audio, key=lambda p: p.stat().st_size)
        info.audio_path = path
        if progress is not None:
            progress(1.0)
        return path

    def download_video(self, info: MediaInfo, dest_dir: Path,
                       progress: ProgressCallback | None = None) -> Path:
        """单独下载视频(仅旧任务重跑时音频已存在、讲义缺视频源的场景使用)。

        产物命名 {stem}.video.{ext},视同临时缓存,由调用方在截帧后删除。
        """
        if info.platform == "kuaishou":
            # 快手走原路径即可得到合一 mp4(命名不带 .video,调用方仍会删除)
            return self.download_audio(info, dest_dir, progress, want_video=True)
        ensure_reachable(info.platform, self._proxy)
        dest_dir.mkdir(parents=True, exist_ok=True)

        def hook(d: dict) -> None:
            if progress is None:
                return
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                if total:
                    progress(min(done / total, 1.0))

        outtmpl = str(dest_dir / f"{output_stem(info)}.video.%(ext)s")
        opts = self._yt_opts(
            format="b[height<=1080]/bv*[height<=1080]+ba/b",
            outtmpl=outtmpl,
            noplaylist=True,
            overwrites=True,
            progress_hooks=[hook],
            nopart=True,
            socket_timeout=30,
            retries=3,
            fragment_retries=3,
            retry_sleep=3,
            merge_output_format="mp4",
        )
        with self._lock:
            with self._ydl_factory(opts) as ydl:
                try:
                    ydl.download([info.url])
                except yt_dlp.utils.DownloadError as exc:
                    raise DownloadError(f"下载视频失败: {exc}") from exc
        prefix = f"{output_stem(info)}.video"
        video_exts = {".mp4", ".webm", ".mkv", ".flv", ".mov", ".avi", ".m4v", ".ts", ".wmv"}
        found = [p for p in dest_dir.iterdir()
                 if p.is_file() and p.stem.startswith(prefix) and p.suffix.lower() in video_exts]
        if not found:
            raise DownloadError(f"下载完成但未找到视频文件: {dest_dir / prefix}.*")
        path = max(found, key=lambda p: p.stat().st_size)
        if progress is not None:
            progress(1.0)
        return path


def output_stem(info: MediaInfo) -> str:
    """输出/下载文件名主干:标题 + (多P时) _P{part} 后缀,避免多P同名互相覆盖。

    单 P 视频保持原文件名(兼容已有产物);多 P 时各 P 文件名带 part 后缀,
    防止 B 站合集各 P 标题相同时音频/SRT/TXT/MD 互相覆盖导致数据丢失。
    """
    stem = _safe_name(info.title) or info.media_id
    if info.total_parts > 1:
        stem = f"{stem}_P{info.part:02d}"
    return stem


def _safe_name(name: str) -> str:
    """Windows 文件名非法字符替换,并限长。"""
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in bad else ch for ch in name).strip().rstrip(".")
    return cleaned[:80]
