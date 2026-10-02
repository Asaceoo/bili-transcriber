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
