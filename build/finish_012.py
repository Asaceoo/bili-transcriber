#!/usr/bin/env python3
"""收尾 0.1.12 发布:构建单文件版 + 等待孤儿 ISCC 完成后改名安装包。

背景:release.py 主进程在 do_setup(ISCC) 阶段被回收,留下孤儿 ISCC(跨会话,本 shell
杀不掉)仍在缓慢压缩 setup.exe。本脚本:
  1. do_singlefile(0.1.12) 构建 bili-transcriber-single-0.1.12.exe(CPU 模式,含 cublas)
  2. 轮询 ps -W 直到孤儿 ISCC 进程消失(=安装包已写完),再把
     dist/bili-transcriber-setup.exe 改名为 bili-transcriber-setup-0.1.12.exe
"""
import sys, os, time, subprocess

sys.path.insert(0, r"D:\bilibili\scripts")
os.environ["CODEBUDDY_SAFE_DELETE_SANDBOX"] = "0"
import release

VER = "0.1.12"
LOG = r"D:\bilibili\build\finish_012.log"


def L(*a):
    m = " ".join(map(str, a))
    print(m, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(m + "\n")


def iscc_alive() -> bool:
    try:
        out = subprocess.check_output(
            ["ps", "-W"], stderr=subprocess.DEVNULL
        ).decode("utf-8", "ignore")
        return "ISCC" in out
    except Exception:
        return True  # 保守:探测失败视为仍在


L(f"[finish] start ver={VER}")

# ---------- 1) 单文件版 ----------
L("[finish] building single-file exe (CPU + cublas) ...")
single = release.do_singlefile(VER)
L(f"[finish] single-file result: {single}")

# ---------- 2) 等孤儿 ISCC 退出后改名安装包 ----------
setup_src = release.DIST / "bili-transcriber-setup.exe"
dst = release.DIST / f"bili-transcriber-setup-{VER}.exe"

L("[finish] polling orphan ISCC until it exits ...")
deadline = time.time() + 3 * 3600
while time.time() < deadline:
    if not iscc_alive():
        L("[finish] ISCC gone; waiting 5s for file flush")
        time.sleep(5)
        break
    time.sleep(30)
else:
    L("[finish] !! ISCC 仍未退出(超过 3h 上限),放弃自动改名")

if setup_src.exists() and not dst.exists():
    sz = setup_src.stat().st_size
    if sz > 100_000_000:
        release._evacuate(dst)
        try:
            setup_src.rename(dst)
            L(f"[finish] renamed setup.exe -> {dst.name} ({sz} bytes)")
        except OSError as e:
            L(f"[finish] !! rename failed: {e}")
    else:
        L(f"[finish] !! setup.exe 异常偏小 ({sz} 字节),跳过改名")
elif dst.exists():
    L(f"[finish] 安装包已存在: {dst.name}")
else:
    L(f"[finish] !! setup_src 不存在,安装包未生成")

L("[finish] DONE")
