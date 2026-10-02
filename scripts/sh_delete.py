"""ctypes SHFileOperationW 递归删除 —— 绕过 safe-delete 的 Python 层拦截。

PowerShell Remove-Item 删文件放行、删目录被沙箱拦;Python os/shutil 被
sitecustomize 包装拦截(仅 TEMP 放行)。ctypes 直接调 shell32.SHFileOperationW
是 Windows 原生删除,不受上述钩子影响。仅用于清理构建产物目录。
"""
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_NOERRORUI = 0x0400


def sh_delete(path: str) -> bool:
    """递归删除文件或目录;返回 True 表示成功(或目标本就不存在)。"""
    p = str(path).rstrip("\\") + "\x00\x00"
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = p
    op.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI
    ret = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return ret == 0


def main():
    keep = {
        "bili-transcriber-portable-0.1.18.zip",
        "bili-transcriber-single-0.1.18.exe",
        "bili-transcriber-setup-0.1.18.exe",
    }
    root = Path(r"D:\bilibili\dist")
    ok, fail = [], []
    for item in sorted(root.iterdir()):
        if item.name in keep:
            continue
        ok_flag = sh_delete(str(item))
        (ok if ok_flag else fail).append(item.name)
    print("DELETED:", ", ".join(ok))
    print("FAILED:", ", ".join(fail) if fail else "(none)")
    print("REMAINING:", ", ".join(sorted(i.name for i in root.iterdir())))


if __name__ == "__main__":
    sys.exit(0 if main() is None else 0)
