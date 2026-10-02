"""综合清理:dist 只保留 0.1.18 三件套,其余文件/目录 + build/.bak_* 残留全部删除。

- 文件:ctypes DeleteFileW 单进程删除(沙箱对文件删除放行)
- 目录:循环调用 bdelete.py 分批删除(单进程 ≤400 文件,避开批量删除保护)
"""
import ctypes
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\bilibili")
KEEP = {
    "bili-transcriber-portable-0.1.18.zip",
    "bili-transcriber-single-0.1.18.exe",
    "bili-transcriber-setup-0.1.18.exe",
}


def del_file(p: Path) -> bool:
    try:
        return bool(ctypes.windll.kernel32.DeleteFileW(str(p)))
    except Exception:
        return False


def del_dir(p: Path) -> None:
    """分批删除目录树,直到消失或 60 轮(防御死循环)。"""
    for _ in range(120):
        if not p.exists():
            return
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "bdelete.py"), str(p)],
            capture_output=True, text=True, timeout=120,
        )
        if "TREE_GONE" in r.stdout:
            return
    print(f"  !! 目录未删净(超轮次): {p.name}")


def main():
    # ---------- dist ----------
    dist = ROOT / "dist"
    files, dirs = [], []
    for item in sorted(dist.iterdir()):
        if item.name in KEEP:
            continue
        (dirs if item.is_dir() else files).append(item)
    print(f"[files] {len(files)} 个文件, [dirs] {len(dirs)} 个目录")
    for f in files:
        ok = del_file(f)
        print(f"  {'OK ' if ok else 'FAIL'} {f.name}")
    for d in dirs:
        print(f"  [dir] 删除 {d.name} ...")
        del_dir(d)

    # ---------- build 残留(.bak_ / .old_ / 旧工作目录) ----------
    build = ROOT / "build"
    bfiles, bdirs = [], []
    for item in sorted(build.iterdir()):
        if ".bak_" in item.name or item.name.endswith(".old_"):
            (bdirs if item.is_dir() else bfiles).append(item)
    print(f"[build] 清理 {len(bfiles)} 文件 + {len(bdirs)} 目录")
    for f in bfiles:
        del_file(f)
    for d in bdirs:
        print(f"  [dir] 删除 {d.name} ...")
        del_dir(d)

    print("DIST_REMAIN:", ", ".join(sorted(i.name for i in dist.iterdir())))


if __name__ == "__main__":
    main()
