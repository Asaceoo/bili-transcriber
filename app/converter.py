"""FFmpeg 转码:任意音频 → 16kHz 单声道 wav(faster-whisper 推荐输入)。"""

from __future__ import annotations

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


def ffmpeg_available(ffmpeg: str = "ffmpeg") -> bool:
    return shutil.which(ffmpeg) is not None
