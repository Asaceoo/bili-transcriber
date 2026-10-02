"""转写结果落盘:SRT / TXT / Markdown 三种格式 + 图文讲义(关键帧截图版)。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from app.transcriber import Segment


@dataclass
class NoteMeta:
    title: str
    uploader: str
    duration: float
    bv: str
    url: str
    model: str = ""


@dataclass
class SlideSection:
    """讲义的一页:一个关键帧画面 + 该画面期间的字幕。"""
    time: float
    image_rel: str  # 相对 notes.md 所在目录的图片路径(POSIX 风格)
    segments: list[Segment] = field(default_factory=list)


def _fmt_ts_srt(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_ts_md(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h} 小时 {m} 分 {sec} 秒"
    return f"{m} 分 {sec} 秒"


def write_srt(segments: Iterable[Segment], path: Path) -> Path:
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{_fmt_ts_srt(seg.start)} --> {_fmt_ts_srt(seg.end)}")
        lines.append(seg.text)
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_txt(segments: Iterable[Segment], path: Path) -> Path:
    text = "\n".join(seg.text for seg in segments)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def write_md(meta: NoteMeta, segments: Iterable[Segment], path: Path) -> Path:
    segs = list(segments)
    lines = [
        f"# {meta.title}",
        "",
        f"- 作者:{meta.uploader or '未知'}",
        f"- 时长:{_fmt_duration(meta.duration)}",
        f"- 视频号:`{meta.bv}`",
        f"- 链接:{meta.url}",
    ]
    if meta.model:
        lines.append(f"- 转写模型:{meta.model}")
    lines += ["", "## 正文", ""]
    for seg in segs:
        lines.append(f"**[{_fmt_ts_md(seg.start)}]** {seg.text}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def build_slide_sections(
    segments: Iterable[Segment],
    frames: list[tuple[float, Path]],
    md_dir: Path,
) -> list[SlideSection]:
    """把字幕按"句子开始时所在画面"分到各关键帧页,每句只出现一次;
    首帧之前的字幕归首页。frames 为 (时间秒, 图片路径) 列表,按时间升序。"""
    segs = sorted(segments, key=lambda s: s.start)
    sections: list[SlideSection] = []
    for i, (t, img) in enumerate(frames):
        t_end = frames[i + 1][0] if i + 1 < len(frames) else float("inf")
        if i == 0:
            own = [s for s in segs if s.start < t_end]
        else:
            own = [s for s in segs if t <= s.start < t_end]
        image_rel = _md_href(Path(os.path.relpath(img, md_dir)).as_posix())
        sections.append(SlideSection(time=t, image_rel=image_rel, segments=own))
    return sections


_MD_HREF_UNSAFE = {" ": "%20", "(": "%28", ")": "%29", "#": "%23", "%": "%25"}


def _md_href(rel: str) -> str:
    """编码会破坏 Markdown 链接解析的字符(标题带空格/括号会让截图显示不出来);
    中文等其余字符保留原样,兼容 Typora / VSCode / GitHub / Obsidian。"""
    return "".join(_MD_HREF_UNSAFE.get(ch, ch) for ch in rel)


def _page_anchor(url: str, seconds: float) -> str:
    """页标题时间戳;有链接时做成可点击跳回视频对应位置的锚点。"""
    ts = _fmt_ts_md(seconds)
    if not url:
        return ts
    sep = "&" if "?" in url else "?"
    return f"[{ts}]({url}{sep}t={int(seconds)})"


def write_notes_md(meta: NoteMeta, sections: list[SlideSection], path: Path) -> Path:
    """图文讲义:每页 = 时间戳标题 + 关键帧截图 + 该页期间的字幕。"""
    lines = [
        f"# {meta.title}",
        "",
        f"- 作者:{meta.uploader or '未知'}",
        f"- 时长:{_fmt_duration(meta.duration)}",
        f"- 视频号:`{meta.bv}`",
        f"- 链接:{meta.url}",
    ]
    if meta.model:
        lines.append(f"- 转写模型:{meta.model}")
    lines += ["", f"共 {len(sections)} 页画面;标题时间戳可点击跳回视频对应位置。", ""]
    for i, sec in enumerate(sections, start=1):
        lines.append(f"## 第 {i} 页 · {_page_anchor(meta.url, sec.time)}")
        lines += ["", f"![第 {i} 页]({sec.image_rel})", ""]
        if sec.segments:
            for seg in sec.segments:
                lines.append(f"**[{_fmt_ts_md(seg.start)}]** {seg.text}")
                lines.append("")
        else:
            lines += ["*(本页时段无字幕)*", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
