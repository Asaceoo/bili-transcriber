from pathlib import Path

import pytest

from app.downloader import DownloadError, Downloader, MediaInfo, _detect_platform


class FakeYDL:
    """按脚本依次返回 extract_info / download 的行为。"""

    script: list[dict] = []
    calls: list[tuple[str, dict | list]] = []

    def __init__(self, opts: dict):
        self.opts = opts
        self._hooks = opts.get("progress_hooks", [])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url: str, download: bool):
        FakeYDL.calls.append(("extract", self.opts))
        action = FakeYDL.script.pop(0)
        if action.get("raise"):
            import yt_dlp
            raise yt_dlp.utils.DownloadError(action["raise"])
        return action["info"]

    def download(self, urls: list[str]):
        FakeYDL.calls.append(("download", self.opts))
        action = FakeYDL.script.pop(0)
        if action.get("raise"):
            import yt_dlp
            raise yt_dlp.utils.DownloadError(action["raise"])
        for u in urls:
            # 按 outtmpl 落一个假媒体文件(扩展名由 action["ext"] 控制,默认 .m4a)
            tmpl = self.opts["outtmpl"]
            prefix = Path(tmpl).stem.replace("%(title)s", action.get("title", "t"))
            out = Path(tmpl).parent / f"{prefix}{action.get('ext', '.m4a')}"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"fake-audio" * 100)
        for h in self._hooks:
            h({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
        return 0


@pytest.fixture(autouse=True)
def reset_fake():
    FakeYDL.script = []
    FakeYDL.calls = []
    yield


@pytest.fixture(autouse=True)
def fast_reachable(monkeypatch):
    """跳过真实网络预检(单元测试不触网):只 mock 内层探测函数恒返回可达,
    ensure_reachable 的缓存/抛错逻辑保持真实,便于单测覆盖。"""
    from app import downloader
    monkeypatch.setattr(downloader, "_reachable_once",
                        lambda url, proxy, timeout=5.0: True)
    downloader._reach_cache.clear()
    yield
    downloader._reach_cache.clear()


def _playlist_info():
    return {
        "_type": "playlist",
        "title": "合集:转写教程",
        "entries": [
            {"id": "BV1test_p1", "title": "第一课", "uploader": "UP主",
             "duration": 60, "webpage_url": "https://www.bilibili.com/video/BV1test?p=1"},
            {"id": "BV1test_p2", "title": "第二课", "uploader": "UP主",
             "duration": 90, "webpage_url": "https://www.bilibili.com/video/BV1test?p=2"},
        ],
    }


def test_probe_expands_playlist():
    FakeYDL.script = [{"info": _playlist_info()}]
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    items = dl.probe("https://www.bilibili.com/video/BV1test")
    assert [i.media_id for i in items] == ["BV1test_p1", "BV1test_p2"]
    assert items[0].bv == "BV1test" and items[1].part == 2
    assert items[0].total_parts == 2
    assert items[0].playlist_title == "合集:转写教程"


def test_probe_single_video():
    FakeYDL.script = [{"info": {"id": "BV1single", "title": "单个视频", "uploader": "U",
                                "duration": 10, "webpage_url": "https://x"}}]
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    items = dl.probe("https://x")
    assert len(items) == 1 and items[0].media_id == "BV1single"
    assert items[0].playlist_title == ""


def test_probe_error_wrapped():
    FakeYDL.script = [{"raise": "404"}]
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    with pytest.raises(DownloadError):
        dl.probe("https://bad")


def test_download_audio_writes_file(tmp_path: Path):
    FakeYDL.script = [{"title": "第一课"}, {}]  # download 的 action
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    info = MediaInfo(media_id="BV1t_p1", bv="BV1t", title="第一课", uploader="U",
                     duration=60, url="https://x")
    seen: list[float] = []
    path = dl.download_audio(info, tmp_path, progress=seen.append)
    assert path.exists() and path.suffix == ".m4a"
    assert info.audio_path == path
    assert seen == [0.5, 1.0]


def test_download_audio_multipart_filename_has_part_suffix(tmp_path: Path):
    """回归:多P标题相同时,下载文件名必须带 _P{part} 后缀,避免互相覆盖。"""
    FakeYDL.script = [{"title": "同名标题"}, {}]
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    info = MediaInfo(media_id="BV1t_p2", bv="BV1t", title="同名标题", uploader="U",
                     duration=60, url="https://x", part=2, total_parts=2)
    path = dl.download_audio(info, tmp_path)
    assert path.name == "同名标题_P02.m4a"


def test_download_audio_single_part_keeps_plain_name(tmp_path: Path):
    """单P视频文件名保持原样,不带 _P 后缀。"""
    FakeYDL.script = [{"title": "单个视频"}, {}]
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    info = MediaInfo(media_id="BV1t_p1", bv="BV1t", title="单个视频", uploader="U",
                     duration=60, url="https://x", part=1, total_parts=1)
    path = dl.download_audio(info, tmp_path)
    assert path.name == "单个视频.m4a"


# ---------- 图文讲义:视频流下载 ----------

def test_download_audio_want_video_uses_video_format(tmp_path: Path):
    """want_video=True 时切到含画面的格式串并要求 mp4 合并输出。"""
    FakeYDL.script = [{"title": "第一课", "ext": ".mp4"}]
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    info = MediaInfo(media_id="BV1t_p1", bv="BV1t", title="第一课", uploader="U",
                     duration=60, url="https://x")
    path = dl.download_audio(info, tmp_path, want_video=True)
    opts = FakeYDL.calls[0][1]
    assert opts["format"] == "b[height<=1080]/bv*[height<=1080]+ba/b"
    assert opts["merge_output_format"] == "mp4"
    assert path.suffix == ".mp4" and info.audio_path == path


def test_download_audio_default_format_unchanged(tmp_path: Path):
    """不开讲义时维持纯音频格式(不额外下视频)。"""
    FakeYDL.script = [{"title": "第一课"}]
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    info = MediaInfo(media_id="BV1t_p1", bv="BV1t", title="第一课", uploader="U",
                     duration=60, url="https://x")
    dl.download_audio(info, tmp_path)
    opts = FakeYDL.calls[0][1]
    assert opts["format"] == "bestaudio[ext=m4a]/bestaudio/best"
    assert "merge_output_format" not in opts


def test_download_video_writes_dotted_video_file(tmp_path: Path):
    FakeYDL.script = [{"title": "第一课", "ext": ".mp4"}]
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    info = MediaInfo(media_id="BV1t_p1", bv="BV1t", title="第一课", uploader="U",
                     duration=60, url="https://x")
    seen: list[float] = []
    path = dl.download_video(info, tmp_path, progress=seen.append)
    assert path.name == "第一课.video.mp4"
    assert FakeYDL.calls[0][1]["merge_output_format"] == "mp4"
    assert seen[-1] == 1.0


def test_download_video_no_video_file_raises(tmp_path: Path):
    """yt-dlp 落盘的不是视频扩展名时报错(而不是静默成功)。"""
    FakeYDL.script = [{"title": "第一课", "ext": ".m4a"}]
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    info = MediaInfo(media_id="BV1t_p1", bv="BV1t", title="第一课", uploader="U",
                     duration=60, url="https://x")
    with pytest.raises(DownloadError, match="未找到视频文件"):
        dl.download_video(info, tmp_path)


def test_download_video_kuaishou_delegates(monkeypatch, tmp_path: Path):
    """快手直接复用原下载路径(本就得到合一 mp4)。"""
    from app import kuaishou
    calls: list[bool] = []
    monkeypatch.setattr(kuaishou, "download_audio",
                        lambda info, dest, progress=None: calls.append(True) or (tmp_path / "x.mp4"))
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    info = MediaInfo(media_id="ks_1", bv="ks_1", title="t", uploader="", duration=0,
                     url="https://v.kuaishou.com/abc", platform="kuaishou")
    assert dl.download_video(info, tmp_path) == tmp_path / "x.mp4"
    assert calls == [True]
    assert FakeYDL.calls == []  # 未调用 yt-dlp


def test_safe_name():
    from app.downloader import _safe_name
    assert _safe_name('a<b>:"/\\|?*b') == "a_b________b"
    assert len(_safe_name("长" * 200)) <= 80
    assert _safe_name("  空格结尾..  ") == "空格结尾"


# ---------- 多平台支持 ----------

def test_detect_platform():
    cases = {
        "https://www.bilibili.com/video/BV1xx411c7mD": "bilibili",
        "https://b23.tv/abc": "bilibili",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ": "youtube",
        "https://youtu.be/dQw4w9WgXcQ": "youtube",
        "https://www.youtube.com/shorts/abc123": "youtube",
        "https://v.douyin.com/abc123/": "douyin",
        "https://www.douyin.com/video/123456": "douyin",
        "https://www.xiaohongshu.com/discovery/item/123?xsec_token=xyz": "xiaohongshu",
        "https://xhslink.com/a/b": "xiaohongshu",
        "https://v.kuaishou.com/abc123": "kuaishou",
        "https://www.kuaishou.com/short-video/3xabc": "kuaishou",
        "https://www.kuaishou.com/f/abc123": "kuaishou",
        "https://unknown.example.com/video/1": "bilibili",  # 未知平台按 bilibili 兜底
    }
    for url, expected in cases.items():
        assert _detect_platform(url) == expected, url


def test_probe_youtube_sets_platform_and_bv():
    FakeYDL.script = [{"info": {"id": "dQw4w9WgXcQ", "title": "Rick Roll", "uploader": "R",
                                "duration": 213, "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}}]
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    items = dl.probe("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert len(items) == 1
    assert items[0].platform == "youtube"
    assert items[0].bv == "yt_dQw4w9WgXcQ"
    assert items[0].media_id == "dQw4w9WgXcQ"


def test_probe_douyin_sets_platform_and_bv():
    FakeYDL.script = [{"info": {"id": "7381234567890", "title": "抖音视频", "uploader": "作者",
                                "duration": 30, "webpage_url": "https://www.douyin.com/video/7381234567890"}}]
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    items = dl.probe("https://www.douyin.com/video/7381234567890")
    assert items[0].platform == "douyin"
    assert items[0].bv == "dy_7381234567890"


def test_probe_xiaohongshu_sets_platform_and_bv():
    FakeYDL.script = [{"info": {"id": "65f2abc0000000000102d3e4", "title": "小红书视频",
                                "uploader": "博主", "duration": 45,
                                "webpage_url": "https://www.xiaohongshu.com/discovery/item/65f2abc0000000000102d3e4"}}]
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    items = dl.probe("https://www.xiaohongshu.com/discovery/item/65f2abc0000000000102d3e4")
    assert items[0].platform == "xiaohongshu"
    assert items[0].bv == "xhs_65f2abc0000000000102d3e4"


def test_probe_kuaishou_delegates(monkeypatch):
    """快手 URL 不走 yt-dlp,委托给 app.kuaishou.probe。"""
    from app import kuaishou
    calls: list[str] = []
    monkeypatch.setattr(kuaishou, "probe", lambda url: calls.append(url) or [])
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    dl.probe("https://v.kuaishou.com/abc123")
    assert calls == ["https://v.kuaishou.com/abc123"]
    assert FakeYDL.calls == []  # 未调用 yt-dlp


def test_download_audio_kuaishou_delegates(monkeypatch, tmp_path: Path):
    """快手下载委托给 app.kuaishou.download_audio。"""
    from app import kuaishou
    calls: list[str] = []
    monkeypatch.setattr(kuaishou, "download_audio",
                        lambda info, dest, progress=None: calls.append(info.media_id) or (tmp_path / "x.mp4"))
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    info = MediaInfo(media_id="ks_1", bv="ks_1", title="t", uploader="", duration=0,
                     url="https://v.kuaishou.com/abc", platform="kuaishou")
    dl.download_audio(info, tmp_path)
    assert calls == ["ks_1"]


# ---------- 连通性预检(防不可达平台堵死 worker) ----------

def test_probe_unreachable_fails_fast(monkeypatch):
    """预检不可达时立即抛错,不进 yt-dlp——修复"B站任务被 YouTube 超时堵死"。"""
    from app import downloader

    def boom(platform, proxy=""):
        raise downloader.DownloadError("无法连接 www.youtube.com,请检查网络连接")

    monkeypatch.setattr(downloader, "ensure_reachable", boom)
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    with pytest.raises(DownloadError, match="无法连接"):
        dl.probe("https://www.youtube.com/watch?v=x")
    assert FakeYDL.calls == []  # yt-dlp 完全未被调用


def test_ensure_reachable_raises_when_offline(monkeypatch):
    from app import downloader
    monkeypatch.setattr(downloader, "_reachable_once",
                        lambda url, proxy, timeout=5.0: False)
    downloader._reach_cache.clear()
    with pytest.raises(downloader.DownloadError, match="代理"):
        downloader.ensure_reachable("youtube")


def test_ensure_reachable_ok_when_online(monkeypatch):
    from app import downloader
    monkeypatch.setattr(downloader, "_reachable_once",
                        lambda url, proxy, timeout=5.0: True)
    downloader._reach_cache.clear()
    downloader.ensure_reachable("bilibili")  # 不抛即通过


def test_ensure_reachable_unknown_platform_skips():
    """未登记预检域名的平台直接放行(如本地/未知平台)。"""
    from app import downloader
    downloader.ensure_reachable("unknown-platform")  # 不抛即通过


def test_proxy_injected_into_ydl_opts():
    """配置代理后,yt-dlp 参数必须带上 proxy(YouTube 等境外平台)。"""
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o), proxy="http://127.0.0.1:7890")
    FakeYDL.script = [{"info": {"id": "BV1t", "title": "t", "uploader": "u",
                                 "duration": 1, "webpage_url": "https://x"}}]
    dl.probe("https://www.bilibili.com/video/BV1t")
    assert FakeYDL.calls[0][1]["proxy"] == "http://127.0.0.1:7890"


def test_yt_opts_injects_bundled_ffmpeg_location(monkeypatch):
    """PATH 无 ffmpeg 时把随包静态版指给 yt-dlp,否则分离流无法合并(讲义拿不到高清)。"""
    from app import downloader
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    monkeypatch.setattr(downloader.ffmpeg_bin, "using_bundled", lambda: True)
    monkeypatch.setattr(downloader.ffmpeg_bin, "resolve_ffmpeg", lambda: "D:/bundled/ffmpeg.exe")
    assert dl._yt_opts()["ffmpeg_location"] == "D:/bundled/ffmpeg.exe"

    monkeypatch.setattr(downloader.ffmpeg_bin, "using_bundled", lambda: False)
    assert "ffmpeg_location" not in dl._yt_opts()


def test_configure_resets_proxy():
    """configure 重传配置会更新代理并清空预检缓存。"""
    from app import downloader
    dl = Downloader(ydl_factory=lambda o: FakeYDL(o))
    dl.configure(cookie_file="", proxy="socks5://127.0.0.1:1080")
    assert dl._proxy == "socks5://127.0.0.1:1080"
    assert downloader._reach_cache == {}


# ---------- URL 规范化 ----------

def test_normalize_url_douyin_modal():
    """回归:douyin.com/jingxuan?modal_id=N 曾报 Unsupported URL,须重写为 /video/N。"""
    from app.downloader import normalize_url
    assert normalize_url(
        "https://www.douyin.com/jingxuan?modal_id=7669977095472221476"
    ) == "https://www.douyin.com/video/7669977095472221476"


def test_normalize_url_wechat_rejected_with_guide():
    """微信视频号拦截并给出可操作指引(保存到本地后上传)。"""
    from app.downloader import normalize_url
    with pytest.raises(DownloadError, match="微信视频号"):
        normalize_url("https://channels.weixin.qq.com/web/pages/feed/abc")
