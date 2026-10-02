"""快手 SSR 解析器测试(纯逻辑,不发起真实网络请求)。"""

from pathlib import Path

import pytest

from app import kuaishou
from app.downloader import DownloadError

_HTML = """<!DOCTYPE html><html><head><title>  快手视频标题  </title></head>
<body>
<script>
window.__INITIAL_STATE__ = {
  "video": {
    "photoId": "3xabc123",
    "photoUrl": "https://v.kuaishou.com/short-video/3xabc123"
  }
};
</script>
<video src="https://xxx.kwimgs.com/abc/hd15_3xabc123.mp4?sign=abc&t=123"></video>
<video src="https://xxx.kwimgs.com/abc/b_3xabc123.mp4?sign=def&t=456"></video>
</body></html>"""


def test_photo_id_from_html():
    assert kuaishou._photo_id(_HTML, "https://v.kuaishou.com/xyz") == "3xabc123"


def test_photo_id_from_url_fallback():
    html = "<html><body>no id here</body></html>"
    assert kuaishou._photo_id(html, "https://www.kuaishou.com/short-video/3xurlid") == "3xurlid"


def test_photo_id_default():
    html = "<html><body>nothing</body></html>"
    assert kuaishou._photo_id(html, "https://v.kuaishou.com/xyz") == "kuaishou_video"


def test_pick_mp4_prefers_hd():
    url = kuaishou._pick_mp4(_HTML)
    assert url is not None and "hd15" in url.split("?")[0]


def test_pick_mp4_none():
    assert kuaishou._pick_mp4("<html>no mp4</html>") is None


def test_probe_extracts_info(monkeypatch):
    monkeypatch.setattr(kuaishou, "_fetch", lambda url: _HTML)
    items = kuaishou.probe("https://v.kuaishou.com/xyz")
    assert len(items) == 1
    info = items[0]
    assert info.platform == "kuaishou"
    assert info.media_id == "3xabc123"
    assert info.bv == "ks_3xabc123"
    assert info.title == "快手视频标题"
    assert info.direct_url and "hd15" in info.direct_url.split("?")[0]


def test_probe_no_mp4_raises(monkeypatch):
    monkeypatch.setattr(kuaishou, "_fetch", lambda url: "<html><title>t</title></html>")
    with pytest.raises(DownloadError):
        kuaishou.probe("https://v.kuaishou.com/xyz")


def test_probe_fetch_error_wrapped(monkeypatch):
    """真实 _fetch 的网络异常应包装为 DownloadError。"""
    def boom(req, timeout=20):
        raise OSError("network down")
    monkeypatch.setattr(kuaishou.urllib.request, "urlopen", boom)
    with pytest.raises(DownloadError):
        kuaishou.probe("https://v.kuaishou.com/xyz")


def test_download_audio_writes_mp4(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(kuaishou, "_fetch", lambda url: _HTML)
    monkeypatch.setattr(
        kuaishou, "_download",
        lambda url, dest, referer="", progress=None: dest.write_bytes(b"fake-mp4"),
    )
    info = kuaishou.probe("https://v.kuaishou.com/xyz")[0]
    seen: list[float] = []
    out = kuaishou.download_audio(info, tmp_path, progress=seen.append)
    assert out.exists() and out.suffix == ".mp4"
    assert out.name == "快手视频标题.mp4"
    assert info.audio_path == out
    assert seen == [1.0]


def test_download_audio_refetches_when_direct_url_empty(monkeypatch, tmp_path: Path):
    """direct_url 为空(如 token 失效)时重新抓页。"""
    monkeypatch.setattr(kuaishou, "_fetch", lambda url: _HTML)
    monkeypatch.setattr(
        kuaishou, "_download",
        lambda url, dest, referer="", progress=None: dest.write_bytes(b"fake-mp4"),
    )
    info = kuaishou.probe("https://v.kuaishou.com/xyz")[0]
    info.direct_url = ""  # 模拟失效
    out = kuaishou.download_audio(info, tmp_path)
    assert out.exists()


def test_download_audio_no_url_raises(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(kuaishou, "_fetch", lambda url: "<html>no mp4</html>")
    from app.downloader import MediaInfo
    info = MediaInfo(media_id="ks_1", bv="ks_1", title="t", uploader="", duration=0,
                     url="https://v.kuaishou.com/xyz", platform="kuaishou", direct_url="")
    with pytest.raises(DownloadError):
        kuaishou.download_audio(info, tmp_path)
