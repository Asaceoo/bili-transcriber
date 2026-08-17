"""转写结果落盘:SRT / TXT / Markdown 三种格式。"""

from __future__ import annotations

from dataclasses import dataclass
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
        f"- UP 主:{meta.uploader or '未知'}",
        f"- 时长:{_fmt_duration(meta.duration)}",
        f"- BV 号:`{meta.bv}`",
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
