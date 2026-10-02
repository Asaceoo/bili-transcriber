"""Windows 原生文件选择框(ctypes comdlg32):本机桌面场景免浏览器上传。

NiceGUI 的 ui.upload 走 base64-over-websocket,几十 MB 的视频要传数分钟;
本机使用时直接弹系统文件框,把原始文件路径交给 pipeline(零拷贝,秒提交)。
仅 Windows;其他平台返回空列表,UI 侧回退浏览器上传。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

OFN_ALLOWMULTISELECT = 0x00000200
OFN_EXPLORER = 0x00080000
OFN_FILEMUSTEXIST = 0x00001000
OFN_HIDEREADONLY = 0x00000004
OFN_NOCHANGEDIR = 0x00000008

# 与任务页浏览器上传一致的扩展名白名单
MEDIA_FILTER = (
    "视频/音频文件\0"
    "*.mp4;*.mkv;*.mov;*.avi;*.webm;*.flv;*.wmv;*.ts;"
    "*.mp3;*.m4a;*.wav;*.ogg;*.opus;*.flac;*.aac\0"
    "所有文件\0*.*\0"
)


def parse_multiselect(raw: str) -> list[Path]:
    """解析 GetOpenFileNameW 的缓冲区:单选 = 完整路径;
    多选 = 目录\\0文件1\\0文件2\\0…(首段是目录,其余是文件名)。"""
    parts = [p for p in raw.split("\0") if p]
    if not parts:
        return []
    if len(parts) == 1:
        return [Path(parts[0])]
    base = Path(parts[0])
    return [base / name for name in parts[1:]]


def pick_files(title: str = "选择要转写的视频/音频文件") -> list[Path]:
    """弹出系统文件选择框(可多选);取消或失败返回空列表。"""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", wintypes.LPVOID),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", wintypes.LPVOID),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    buf = ctypes.create_unicode_buffer(64 * 1024)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = None
    ofn.lpstrFilter = MEDIA_FILTER
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = len(buf)
    ofn.lpstrTitle = title
    # NOCHANGEDIR:防止对话框改变进程工作目录(PyInstaller 相对路径依赖 cwd)
    ofn.Flags = (OFN_EXPLORER | OFN_ALLOWMULTISELECT | OFN_FILEMUSTEXIST
                 | OFN_HIDEREADONLY | OFN_NOCHANGEDIR)
    try:
        if not ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            return []
        return parse_multiselect(buf.value)
    except Exception:  # noqa: BLE001 — 对话框失败不应崩 UI,回退浏览器上传
        logger.exception("原生文件选择框调用失败")
        return []
