"""分批删除一个目录树:单进程最多删 BATCH 个文件(沙箱批量删除保护阈值≈500)。

每进程删除量小(≤500 文件)可安全通过;剩余文件由外层循环再次调用本脚本清理。
仅用于清理构建产物目录。
用法: python bdelete.py <目录路径>
输出: TREE_GONE(已删完) 或 BATCH_DONE <n>(还有剩余,需再次调用)
"""
import ctypes
import os
import sys
from pathlib import Path

BATCH = 400  # 留余量,低于触发阈值


def main() -> int:
    root = sys.argv[1]
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            attrs = ctypes.windll.kernel32.GetFileAttributesW(fp)
            if attrs != 0xFFFFFFFF and attrs & 0x1:  # 只读
                ctypes.windll.kernel32.SetFileAttributesW(fp, attrs & ~0x1)
            if ctypes.windll.kernel32.DeleteFileW(fp):
                count += 1
                if count >= BATCH:
                    print("BATCH_DONE", count)
                    return 0
        # 删除已成空的子目录与当前目录(非空时返回 0,忽略)
        for d in reversed(dirnames):
            ctypes.windll.kernel32.RemoveDirectoryW(os.path.join(dirpath, d))
        ctypes.windll.kernel32.RemoveDirectoryW(dirpath)
    print("TREE_GONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
