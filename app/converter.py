"""FFmpeg 转码:任意音频 → 16kHz 单声道 wav(faster-whisper 推荐输入)。"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


class ConverterError(RuntimeError):
    pass


def to_wav16k_mono(src: Path, dst: Path, ffmpeg: str = "ffmpeg") -> Path:
    """转码失败抛 ConverterError;成功返回 dst。"""
    if not src.exists():
        raise ConverterError(f"源文件不存在: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise ConverterError("未找到 ffmpeg,请安装并加入 PATH") from exc
    if proc.returncode != 0:
        raise ConverterError(f"ffmpeg 转码失败: {proc.stderr.strip()[:500]}")
    return dst


def media_duration(path: Path, ffprobe: str = "ffprobe", ffmpeg: str = "ffmpeg") -> float:
    """尽力探测媒体时长(秒);无可用工具或失败返回 0.0。
    优先用 ffprobe(format/stream 级时长取首个有效值);ffprobe 缺失时回退解析 ffmpeg -i 的 Duration 行。"""
    if not path.exists():
        return 0.0
    # 1) ffprobe
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error",
             "-show_entries", "format=duration:stream=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        proc = None
    if proc is not None and proc.returncode == 0:
        for tok in proc.stdout.strip().split():
            try:
                return float(tok)
            except ValueError:
                continue
    # 2) 回退:ffmpeg -i 输出中的 Duration 行
    try:
        proc2 = subprocess.run(
            [ffmpeg, "-i", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc2.stderr)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return h * 3600 + mi * 60 + s
    return 0.0


def ffmpeg_available(ffmpeg: str = "ffmpeg") -> bool:
    return shutil.which(ffmpeg) is not None
