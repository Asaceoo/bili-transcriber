from pathlib import Path

from app.transcriber import Segment
from app.writers import NoteMeta, write_md, write_srt, write_txt

SEGS = [
    Segment(0.0, 2.5, "大家好"),
    Segment(2.5, 61.2, "今天讲转写工具"),
]


def test_srt_format(tmp_path: Path):
    p = write_srt(SEGS, tmp_path / "a.srt")
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "1"
    assert lines[1] == "00:00:00,000 --> 00:00:02,500"
    assert lines[2] == "大家好"
    assert lines[4] == "2"
    assert lines[5] == "00:00:02,500 --> 00:01:01,200"


def test_txt_joins_text(tmp_path: Path):
    text = write_txt(SEGS, tmp_path / "a.txt").read_text(encoding="utf-8")
    assert text.splitlines() == ["大家好", "今天讲转写工具"]


def test_md_has_header_and_timestamps(tmp_path: Path):
    meta = NoteMeta(title="测试视频", uploader="UP", duration=61.2,
                    bv="BV1xx", url="https://b23.tv/x", model="turbo")
    text = write_md(meta, SEGS, tmp_path / "a.md").read_text(encoding="utf-8")
    assert "# 测试视频" in text
    assert "- BV 号:`BV1xx`" in text
    assert "**[00:00:00]** 大家好" in text
    assert "**[00:00:02]** 今天讲转写工具" in text
