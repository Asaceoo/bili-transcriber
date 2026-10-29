"""原生文件选择框:多选缓冲区解析与过滤器格式。"""

from pathlib import Path

from app import native_dialog


def test_parse_single_selection():
    assert native_dialog.parse_multiselect("D:\\videos\\课.mp4") == [Path("D:\\videos\\课.mp4")]


def test_parse_multi_selection():
    raw = "D:\\videos\0第一课.mp4\0第二课.mp4\0"
    assert native_dialog.parse_multiselect(raw) == [
        Path("D:\\videos\\第一课.mp4"),
        Path("D:\\videos\\第二课.mp4"),
    ]


def test_parse_empty_when_cancelled():
    assert native_dialog.parse_multiselect("") == []


def test_media_filter_double_null_terminated():
    """comdlg32 要求过滤器以双 \\0 结尾:形如 描述\\0通配\\0…描述\\0通配\\0。"""
    f = native_dialog.MEDIA_FILTER
    assert f.endswith("\0")
    parts = f.split("\0")
    # 去掉结尾空串后,描述与通配必须成对
    body = [p for p in parts if p]
    assert len(body) % 2 == 0
    assert body[0] == "视频/音频文件"
    assert "*.mp4" in body[1] and "*.flac" in body[1]
    assert body[-1] == "*.*"


def test_cookie_filter_is_paired():
    """选 cookies.txt 用的过滤器同样是 描述\\0通配 成对,且只挑 txt。"""
    body = [p for p in native_dialog.COOKIE_FILTER.split("\0") if p]
    assert len(body) % 2 == 0
    assert body[0].startswith("Cookie")
    assert body[1] == "*.txt"


def test_pick_file_returns_none_when_cancelled(monkeypatch):
    monkeypatch.setattr(native_dialog, "_open_dialog", lambda *a, **k: [])
    assert native_dialog.pick_file() is None


def test_pick_file_returns_first_selection(monkeypatch):
    monkeypatch.setattr(native_dialog, "_open_dialog",
                        lambda *a, **k: [Path("C:/tmp/cookies.txt")])
    assert native_dialog.pick_file("选文件", native_dialog.COOKIE_FILTER) == \
        Path("C:/tmp/cookies.txt")


def test_pick_files_keeps_multiselect(monkeypatch):
    seen = {}

    def fake(title, file_filter, multiple):
        seen.update(title=title, file_filter=file_filter, multiple=multiple)
        return [Path("a.mp4"), Path("b.mp3")]

    monkeypatch.setattr(native_dialog, "_open_dialog", fake)
    assert native_dialog.pick_files() == [Path("a.mp4"), Path("b.mp3")]
    assert seen["multiple"] is True
    assert seen["file_filter"] == native_dialog.MEDIA_FILTER
