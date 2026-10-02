"""快手分享页 SSR 解析:无登录提取 mp4 直链并下载。

yt-dlp 无内置快手提取器(2026-07 实测),走分享页 SSR:页面 HTML 内联
mp4 直链(hd15 高清 / _b_ 标清两档)。分享页 token 有时效,解析与下载
间隔过久可能失效,故下载时若直链为空会重新抓页。

已验证 URL 模式:
  - https://www.kuaishou.com/f/<shareToken>        (短链)
  - https://www.kuaishou.com/short-video/<photoId> (长链)
  - https://v.kuaishou.com/<shortCode>             (App 分享)
"""

from __future__ import annotations

import logging
import re
import urllib.request
from pathlib import Path
from typing import Callable

from app.downloader import DownloadError, MediaInfo, output_stem

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float], None]

# iPhone Safari UA 最稳:其它 UA 有时拿到不含 mp4 直链的页面
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")

_MP4_RE = re.compile(r'https?://[^\s"\']+?\.mp4(?:\?[^\s"\']+)?')


def _fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 — 网络异常统一包装为 DownloadError
        raise DownloadError(f"快手分享页抓取失败: {exc}") from exc


def _photo_id(html: str, url: str) -> str:
    m = re.search(r"short-video/([a-zA-Z0-9_-]+)", html)
    if m:
        return m.group(1)
    m = re.search(r"clientCacheKey=([a-zA-Z0-9_-]+)", html)
    if m:
        return m.group(1).split("_")[0]
    m = re.search(r"/(?:short-video|f|photo)/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    return "kuaishou_video"


def _pick_mp4(html: str) -> str | None:
    """从分享页 HTML 提取 mp4 直链,优先高清(hd15/_hd),其次标清(_b_),最后兜底。"""
    seen: dict[str, str] = {}
    for u in _MP4_RE.findall(html):
        seen.setdefault(u.split("?")[0], u)
    if not seen:
        return None
    hd = [u for b, u in seen.items() if "hd15" in b or "_hd" in b]
    std = [u for b, u in seen.items() if "_b_" in b]
    other = [u for b, u in seen.items() if u not in hd and u not in std]
    for group in (hd, std, other):
        if group:
            return group[0]
    return None


def probe(url: str) -> list[MediaInfo]:
    html = _fetch(url)
    photo_id = _photo_id(html, url)
    m = re.search(r"<title>([^<]+)", html)
    title = m.group(1).strip() if m else photo_id
    mp4 = _pick_mp4(html)
    if not mp4:
        raise DownloadError("快手分享页未找到视频直链(链接可能已失效或受限)")
    return [MediaInfo(
        media_id=photo_id,
        bv=f"ks_{photo_id}",
        title=title,
        uploader="",
        duration=0.0,
        url=url,
        part=1,
        total_parts=1,
        platform="kuaishou",
        direct_url=mp4,
    )]


def download_audio(info: MediaInfo, dest_dir: Path, progress: ProgressCallback | None = None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    direct = info.direct_url
    if not direct:
        # 分享页 token 有时效,解析与下载间隔过久可能失效,重新抓页
        html = _fetch(info.url)
        direct = _pick_mp4(html) or ""
    if not direct:
        raise DownloadError("快手分享页未找到视频直链(链接可能已失效或受限)")
    out = dest_dir / f"{output_stem(info)}.mp4"
    _download(direct, out, referer=info.url, progress=progress)
    info.audio_path = out
    if progress is not None:
        progress(1.0)
    return out


def _download(url: str, dest: Path, referer: str = "",
              progress: ProgressCallback | None = None) -> None:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress is not None and total:
                        progress(min(done / total, 1.0))
    except Exception as exc:  # noqa: BLE001 — 网络/IO 异常统一包装
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"快手视频下载失败: {exc}") from exc
    tmp.replace(dest)
