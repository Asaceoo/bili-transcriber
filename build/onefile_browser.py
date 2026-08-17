"""单文件便携版专用运行时 hook:默认浏览器模式 + 强制 CPU 转写。

1. 浏览器模式:
   pywebview 在 PyInstaller --onefile 解压环境里初始化原生 WebView2 控件
   时容易静默失败(白屏、不报错),后端 HTTP 服务本身完全正常,
   因此单文件版默认改用浏览器模式(打开默认浏览器访问本地 UI),渲染 100% 可靠。
   设 BILI_FORCE_NATIVE=1 可强制原生窗口。

2. 强制 CPU 转写:
   faster-whisper / ctranslate2 在 Windows 上用 CUDA_DYNAMIC_LOADING=ON 编译,
   即使 device=cpu 也会在模块 import 时尝试 LoadLibrary('cublas64_12.dll')/
   'cublas64_11.dll' 探测 GPU。单文件版打包不含这些 CUDA 运行时(~2GB),
   DLL 缺失即抛 'Library cublas64_12.dll is not found'。在 hook 里预先 import
   ctranslate2 并 monkey-patch get_cuda_device_count() -> 0,绕开 CUDA 探测;
   同时设置 BILI_DEVICE=cpu 让 transcriber 强制走 int8 CPU 路径。

   设 BILI_FORCE_GPU=1 可强制 GPU(需目标机已装 CUDA/cuBLAS 运行时 DLL)。
"""
import os
import sys


def _log(msg: str) -> None:
    """向 stderr 写日志;PyInstaller 启动早期 sys.stderr 可能为 None,防御性处理。"""
    try:
        if sys.stderr is not None:
            sys.stderr.write(f"[bili onefile hook] {msg}\n")
            sys.stderr.flush()
    except Exception:
        pass


def _force_browser_mode() -> None:
    if os.environ.get("BILI_FORCE_NATIVE"):
        _log("BILI_FORCE_NATIVE set, keep native window mode")
    else:
        # 直接赋值而非 setdefault,确保覆盖任何已存在的值
        os.environ["BILI_FORCE_BROWSER"] = "1"
        _log("force browser mode for onefile")


def _force_cpu_whisper() -> None:
    """预先 import ctranslate2 并 patch get_cuda_device_count,避免 LoadLibrary cublas 失败。"""
    if os.environ.get("BILI_FORCE_GPU"):
        _log("BILI_FORCE_GPU set, keep GPU mode (target needs CUDA/cuBLAS runtime)")
        return
    try:
        import ctranslate2  # noqa: F401  # 触发模块加载,后续 patch 才能生效
        ctranslate2.get_cuda_device_count = lambda: 0  # type: ignore[assignment]
        _log("ctranslate2.get_cuda_device_count patched to return 0 (CPU forced)")
    except Exception as e:  # pragma: no cover
        _log(f"ctranslate2 patch failed (will rely on BILI_DEVICE fallback): {e!r}")
    # 同时设置环境变量让 transcriber.py 走 CPU 路径(双保险)
    os.environ["BILI_DEVICE"] = "cpu"
    os.environ["BILI_COMPUTE_TYPE"] = "int8"
    _log("BILI_DEVICE=cpu BILI_COMPUTE_TYPE=int8 (CPU fallback)")


_force_browser_mode()
_force_cpu_whisper()
_log("onefile hook done")
