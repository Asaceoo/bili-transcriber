from pathlib import Path

import pytest

from app.downloader import DownloadError, Downloader, MediaInfo


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
            # 按 outtmpl 落一个假音频文件
            tmpl = self.opts["outtmpl"]
            prefix = Path(tmpl).stem.replace("%(title)s", action.get("title", "t"))
            out = Path(tmpl).parent / f"{prefix}.m4a"
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


def test_safe_name():
    from app.downloader import _safe_name
    assert _safe_name('a<b>:"/\\|?*b') == "a_b________b"
    assert len(_safe_name("长" * 200)) <= 80
    assert _safe_name("  空格结尾..  ") == "空格结尾"
