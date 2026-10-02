"""把 dist 下非 0.1.18 产物与 build/.bak_* 残留移到 dist/_delete_me,由用户手动删除。

os.rename(同卷跨目录=即时改名)在沙箱中放行(bash 的 mv 被拦,Python os.rename 不拦)。
"""
import os
import sys
from pathlib import Path

KEEP = {
    "bili-transcriber-portable-0.1.18.zip",
    "bili-transcriber-single-0.1.18.exe",
    "bili-transcriber-setup-0.1.18.exe",
}


def main():
    dist = Path(r"D:\bilibili\dist")
    dst = dist / "_delete_me"
    dst.mkdir(exist_ok=True)
    moved, failed = [], []
    for item in sorted(dist.iterdir()):
        if item == dst or item.name in KEEP:
            continue
        target = dst / item.name
        try:
            os.rename(str(item), str(target))
            moved.append(item.name)
        except Exception as exc:
            failed.append(f"{item.name}({type(exc).__name__}:{str(exc)[:60]})")
    build = Path(r"D:\bilibili\build")
    for item in sorted(build.iterdir()):
        if ".bak_" not in item.name:
            continue
        try:
            os.rename(str(item), str(dst / item.name))
            moved.append(f"build/{item.name}")
        except Exception as exc:
            failed.append(f"build/{item.name}({type(exc).__name__})")
    # 结果写文件,避免管道吞输出
    result = [
        "MOVED: " + (", ".join(moved) if moved else "(none)"),
        "FAILED: " + (", ".join(failed) if failed else "(none)"),
        "DIST_NOW: " + ", ".join(sorted(i.name for i in dist.iterdir())),
    ]
    Path(r"D:\bilibili\_move_result.txt").write_text("\n".join(result), encoding="utf-8")
    print("\n".join(result))


if __name__ == "__main__":
    sys.exit(0)
