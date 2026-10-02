"""音频转码:任意音频 → 16kHz 单声道 wav(faster-whisper 推荐输入)。

后端选择:优先 PyAV(随包内置,不依赖外部 ffmpeg);外部 ffmpeg 仅作回退。
历史上依赖 PATH 上的 ffmpeg,但常见机器上 PATH 里可能是精简构建
(如仅含 mp4 muxer),导致 "Unable to choose an output format for '...wav'"
转写必失败。PyAV 自带完整 libavcodec,可稳定完成转码。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from app.ffmpeg_bin import bundled_ffmpeg, resolve_ffmpeg

logger = logging.getLogger(__name__)


class ConverterError(RuntimeError):
    pass


def to_wav16k_mono(src: Path, dst: Path, ffmpeg: str = "ffmpeg") -> Path:
    """转码失败抛 ConverterError;成功返回 dst。

    ffmpeg 参数仅在显式传入时使用;默认值表示"自动解析"
    (PATH 完整版优先,否则 imageio-ffmpeg 随包静态版)。
    """
    if not src.exists():
        raise ConverterError(f"源文件不存在: {src}")
    if ffmpeg == "ffmpeg":
        ffmpeg = resolve_ffmpeg() or "ffmpeg"
    dst.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    # 1) PyAV 优先:随包内置,不依赖外部 ffmpeg(修复 PATH 精简 ffmpeg 缺 wav muxer 导致转写失败)
    try:
        _pyav_to_wav16k_mono(src, dst)
        logger.info("转码完成(PyAV):%s → %s", src, dst)
        return dst
    except Exception as exc:  # noqa: BLE001 — 任何失败都回退 ffmpeg
        errors.append(f"PyAV 转码失败:{exc}")
        logger.warning("PyAV 转码失败(%s),回退 ffmpeg", exc)
    # 2) 外部/随包 ffmpeg 回退(仅当可用且支持 wav muxer)
    if _ffmpeg_capable(ffmpeg):
        try:
            _ffmpeg_to_wav16k_mono(src, dst, ffmpeg)
            logger.info("转码完成(ffmpeg):%s → %s", src, dst)
            return dst
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ffmpeg 转码失败:{exc}")
    raise ConverterError("; ".join(errors))


def _pyav_to_wav16k_mono(src: Path, dst: Path) -> None:
    """用 PyAV 把任意音频转成 16kHz 单声道 wav(不依赖外部 ffmpeg)。"""
    import av

    inp = av.open(str(src))
    try:
        if not inp.streams.audio:
            raise ConverterError(f"未检测到音频流:{src}")
        out = av.open(str(dst), "w")
        try:
            ost = out.add_stream("pcm_s16le", rate=16000)
            ost.layout = "mono"
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in inp.decode(audio=0):
                for rframe in resampler.resample(frame):
                    rframe.pts = None
                    for p in ost.encode(rframe):
                        out.mux(p)
            for rframe in resampler.resample(None):
                rframe.pts = None
                for p in ost.encode(rframe):
                    out.mux(p)
            for p in ost.encode():
                out.mux(p)
        finally:
            out.close()
    finally:
        inp.close()


def _ffmpeg_to_wav16k_mono(src: Path, dst: Path, ffmpeg: str) -> None:
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(dst),
    ]
    try:
        # timeout 防止 ffmpeg 僵死(如损坏源、设备忙)永久阻塞串行工作线程
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=1800)
    except FileNotFoundError as exc:
        raise ConverterError("未找到 ffmpeg,请安装并加入 PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConverterError(f"ffmpeg 转码超时(>30min): {src}") from exc
    if proc.returncode != 0:
        raise ConverterError(f"ffmpeg 转码失败: {proc.stderr.strip()[:500]}")


_ffmpeg_capable_cache: dict[str, bool] = {}


def _ffmpeg_capable(ffmpeg: str = "ffmpeg") -> bool:
    """外部 ffmpeg 是否可用且支持 wav muxer(精简构建常缺 wav,转码必失败)。"""
    if ffmpeg in _ffmpeg_capable_cache:
        return _ffmpeg_capable_cache[ffmpeg]
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-muxers"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _ffmpeg_capable_cache[ffmpeg] = False
        return False
    ok = proc.returncode == 0 and bool(re.search(r"(^|\s)wav(\s|$)", proc.stdout))
    _ffmpeg_capable_cache[ffmpeg] = ok
    return ok


def media_duration(path: Path, ffprobe: str = "ffprobe", ffmpeg: str = "ffmpeg") -> float:
    """尽力探测媒体时长(秒);无可用工具或失败返回 0.0。

    优先 PyAV(随包内置);再回退 ffprobe(format/stream 级时长取首个有效值);
    ffprobe 缺失时再回退解析 ffmpeg -i 的 Duration 行。
    """
    if not path.exists():
        return 0.0
    # 1) PyAV(随包内置,不依赖外部 ffmpeg)
    try:
        import av
        with av.open(str(path)) as c:
            if c.duration:
                return c.duration / 1_000_000.0
            for s in c.streams:
                if s.duration:
                    return float(s.duration * s.time_base)
    except Exception:  # noqa: BLE001 — 探测失败继续回退
        pass
    # 2) ffprobe
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
    # 3) 回退:ffmpeg -i 输出中的 Duration 行
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
    """PATH 或随包静态版任一可用即 True(转码有 PyAV 兜底,此检测仅用于提示)。"""
    return shutil.which(ffmpeg) is not None or bundled_ffmpeg() is not None
