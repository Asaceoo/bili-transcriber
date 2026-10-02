from pathlib import Path

from app.transcriber import Segment
from app.writers import (
    NoteMeta,
    SlideSection,
    build_slide_sections,
    write_md,
    write_notes_md,
    write_srt,
    write_txt,
)

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
    assert "- 视频号:`BV1xx`" in text
    assert "**[00:00:00]** 大家好" in text
    assert "**[00:00:02]** 今天讲转写工具" in text


def test_build_slide_sections_assigns_by_start_time(tmp_path: Path):
    segs = [
        Segment(0.0, 3.0, "开场"),        # 首页(在首帧之前也归首页)
        Segment(10.0, 15.0, "第二页内容"),
        Segment(25.0, 30.0, "第三页内容"),
    ]
    frames = [
        (0.2, tmp_path / "f" / "slide_001.jpg"),
        (9.8, tmp_path / "f" / "slide_002.jpg"),
        (24.0, tmp_path / "f" / "slide_003.jpg"),
    ]
    sections = build_slide_sections(segs, frames, tmp_path)

    assert [len(s.segments) for s in sections] == [1, 1, 1]
    assert sections[0].segments[0].text == "开场"
    assert sections[1].segments[0].text == "第二页内容"
    # 图片引用为相对路径(POSIX 风格)
    assert sections[0].image_rel == "f/slide_001.jpg"


def test_slide_image_rel_encodes_link_breaking_chars(tmp_path: Path):
    """标题带空格/括号/井号时,Markdown 图片链接必须转义,否则截图显示不出来。"""
    frames = [(0.0, tmp_path / "我的 视频(第一课)#1_frames" / "slide_001.jpg")]
    sections = build_slide_sections([], frames, tmp_path)
    rel = sections[0].image_rel
    assert rel == "我的%20视频%28第一课%29%231_frames/slide_001.jpg"
    # 中文保留原样,只编码破坏链接解析的字符


def test_build_slide_sections_drops_unmatched_and_sorts(tmp_path: Path):
    # 输入未排序也应正确工作;每句只归属一页,不重复
    segs = [
        Segment(12.0, 20.0, "B"),
        Segment(1.0, 2.0, "A"),
    ]
    frames = [(0.0, tmp_path / "f1.jpg"), (10.0, tmp_path / "f2.jpg")]
    sections = build_slide_sections(segs, frames, tmp_path)
    assert [s.segments[0].text for s in sections] == ["A", "B"]
    # 最后一页无字幕时 segments 为空列表
    assert build_slide_sections([], frames, tmp_path)[0].segments == []


def test_write_notes_md_format(tmp_path: Path):
    meta = NoteMeta(title="讲义视频", uploader="UP", duration=90.0,
                    bv="BV1nb", url="https://www.bilibili.com/video/BV1nb?p=2")
    sections = [
        SlideSection(time=0.0, image_rel="t_frames/slide_001.jpg",
                     segments=[Segment(0.0, 3.0, "第一页讲了概述")]),
        SlideSection(time=60.0, image_rel="t_frames/slide_002.jpg", segments=[]),
    ]
    text = write_notes_md(meta, sections, tmp_path / "t.notes.md").read_text(encoding="utf-8")

    assert "# 讲义视频" in text
    assert "共 2 页画面" in text
    # 页标题:序号 + 可点击时间戳;url 已带 ?p=2 时用 & 追加 t 参数
    assert "## 第 1 页 · [00:00:00](https://www.bilibili.com/video/BV1nb?p=2&t=0)" in text
    assert "## 第 2 页 · [00:01:00](https://www.bilibili.com/video/BV1nb?p=2&t=60)" in text
    # 截图相对路径引用 + 该页字幕
    assert "![第 1 页](t_frames/slide_001.jpg)" in text
    assert "**[00:00:00]** 第一页讲了概述" in text
    assert "*(本页时段无字幕)*" in text


def test_write_notes_md_no_url_is_plain_timestamp(tmp_path: Path):
    meta = NoteMeta(title="本地文件", uploader="", duration=10.0, bv="", url="")
    sections = [SlideSection(time=2.0, image_rel="f/slide_001.jpg",
                             segments=[Segment(2.0, 4.0, "内容")])]
    text = write_notes_md(meta, sections, tmp_path / "n.notes.md").read_text(encoding="utf-8")
    assert "## 第 1 页 · 00:00:02" in text
    assert "](http" not in text
