"""ffmpeg 二进制定位:优先 PATH 上的完整版,其次 imageio-ffmpeg 随包静态版。

背景:转码主力是 PyAV(随包内置),ffmpeg 二进制只用于三处——
1) converter 的外部回退;2) keyframes 的 ffmpeg 场景检测后端;
3) yt-dlp 合并分离视频/音频流(讲义下载 1080p 需要,PATH 无 ffmpeg 时必须指路)。
引入 imageio-ffmpeg 后用户机器无需自行安装 ffmpeg。
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_resolved: str | None = None
_checked = False


def bundled_ffmpeg() -> str | None:
    """随包静态 ffmpeg 路径;不可用返回 None。

    两条定位路径:优先 imageio_ffmpeg.get_ffmpeg_exe()(源码/venv 环境);
    冻结(PyInstaller)环境下 importlib.resources 可能解析不到 binaries 子包
    (collect_data_files 默认不收 .py,binaries 不是 PYZ 里的可导入模块),
    此时直查 sys._MEIPASS/imageio_ffmpeg/binaries/ 下的 exe——
    collect_data_files 恰好按该相对路径落盘,onedir 与 onefile 均适用。
    """
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        # env 变量 IMAGEIO_FFMPEG_EXE 指定时 get_ffmpeg_exe 不校验存在性,这里补验
        if exe and Path(exe).exists():
            return exe
    except Exception as exc:  # noqa: BLE001 — 缺包/定位失败都走冻结兜底
        logger.debug("imageio-ffmpeg 定位失败: %s", exc)
    return _frozen_bundled_ffmpeg()


def _frozen_bundled_ffmpeg() -> str | None:
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    binaries_dir = Path(base) / "imageio_ffmpeg" / "binaries"
    if not binaries_dir.is_dir():
        return None
    exes = sorted(binaries_dir.glob("ffmpeg*.exe"))
    return str(exes[0]) if exes else None


def resolve_ffmpeg() -> str | None:
    """解析可用的 ffmpeg:PATH 完整版优先(功能最全),否则随包静态版。"""
    global _resolved, _checked
    if _checked:
        return _resolved
    _checked = True
    _resolved = shutil.which("ffmpeg") or bundled_ffmpeg()
    if _resolved:
        logger.info("ffmpeg 解析:%s", _resolved)
    return _resolved


def using_bundled() -> bool:
    """当前实际使用的是否为随包静态版(PATH 上没有 ffmpeg 时)。"""
    return resolve_ffmpeg() is not None and shutil.which("ffmpeg") is None


def _reset_cache() -> None:
    """仅测试用:清空解析缓存。"""
    global _resolved, _checked
    _resolved, _checked = None, False
