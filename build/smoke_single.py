"""单文件 exe 冒烟测试:启动后在限定时间内探测 UI HTTP 200,并确认无 _ctypes 导入期崩溃。

用法:
  python build/smoke_single.py <exepath> [port]
"""
import subprocess, sys, time, urllib.request, os, signal

EXE = sys.argv[1] if len(sys.argv) > 1 else r"D:\bilibili\dist\bili-transcriber-single-0.1.9.exe"
PORT = sys.argv[2] if len(sys.argv) > 2 else "8891"
URL = f"http://127.0.0.1:{PORT}/"

env = dict(os.environ)
env["BILI_PORT"] = PORT
# 避免自动打开系统默认浏览器(无头环境下无意义)
env["BILI_NO_BROWSER"] = "1"

log_path = r"D:\bilibili\build\smoke_single.log"
logf = open(log_path, "w", encoding="utf-8", buffering=1)
print(f"[smoke] launch {EXE} port={PORT} (log={log_path})")
proc = subprocess.Popen([EXE], env=env, stdout=logf, stderr=subprocess.STDOUT)
print(f"[smoke] pid={proc.pid}")

ok = False
ctypes_err = False
deadline = time.time() + 90
while time.time() < deadline:
    if proc.poll() is not None:
        print(f"[smoke] process exited early, rc={proc.returncode}")
        break
    try:
        with urllib.request.urlopen(URL, timeout=3) as r:
            body = r.read(200).decode("utf-8", "ignore")
            print(f"[smoke] HTTP {r.status}, body[:200]={body!r}")
            ok = r.status == 200
            break
    except urllib.error.HTTPError as e:
        print(f"[smoke] HTTP {e.code}")
        ok = e.code == 200
        break
    except Exception as e:
        time.sleep(2)

if not ok and proc.poll() is None:
    # 再给一次机会读日志判定
    time.sleep(3)

# 读日志判定是否仍有 _ctypes 导入期崩溃
with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
    log = f.read()
ctypes_err = "DLL load failed while importing _ctypes" in log or "ImportError" in log

print("---- SMOKE LOG TAIL ----")
print(log[-2000:])
print("---- RESULT ----")
print(f"HTTP_OK={ok}  CTYPES_IMPORT_ERROR={ctypes_err}")

# 清理进程
if proc.poll() is None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
print("[smoke] done")
