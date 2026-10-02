"""GPU 加速包检测/注册测试(纯逻辑,不加载真实 CUDA DLL)。"""

from pathlib import Path

import pytest

from app import gpu_runtime
from app.gpu_runtime import default_gpu_dir, gpu_available, register_gpu_runtime


def _mk_gpu_pack(root: Path, flat: bool = False) -> Path:
    """构造 GPU 加速包目录(nvidia/cublas/bin 标准布局或扁平布局)。"""
    if flat:
        d = root
        d.mkdir(parents=True, exist_ok=True)
    else:
        d = root / "nvidia" / "cublas" / "bin"
        d.mkdir(parents=True, exist_ok=True)
    (d / "cublas64_12.dll").write_bytes(b"x")
    (d / "cublasLt64_12.dll").write_bytes(b"x")
    # cudnn / cuda_runtime 目录
    for sub in ("cudnn", "cuda_runtime", "cuda_nvrtc"):
        sub_d = (root / "nvidia" / sub / "bin") if not flat else (root / sub / "bin")
        sub_d.mkdir(parents=True, exist_ok=True)
        (sub_d / "cudart64_12.dll").write_bytes(b"x")
    return root


def test_default_gpu_dir_uses_localappdata(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\tester\AppData\Local")
    assert default_gpu_dir() == Path(r"C:\Users\tester\AppData\Local\Bili Note\gpu")


def test_default_gpu_dir_fallback_home(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert default_gpu_dir() == Path.home() / "Bili Note" / "gpu"


def test_gpu_available_standard_layout(tmp_path: Path):
    _mk_gpu_pack(tmp_path)
    assert gpu_available(tmp_path) is True


def test_gpu_available_flat_layout(tmp_path: Path):
    _mk_gpu_pack(tmp_path, flat=True)
    assert gpu_available(tmp_path) is True


def test_gpu_available_missing(tmp_path: Path):
    assert gpu_available(tmp_path) is False
    assert gpu_available(tmp_path / "nope") is False


def test_gpu_available_empty_dir(tmp_path: Path):
    (tmp_path / "nvidia").mkdir(parents=True)
    assert gpu_available(tmp_path) is False


def test_register_gpu_runtime_calls_add_dll_directory(tmp_path: Path, monkeypatch):
    _mk_gpu_pack(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(gpu_runtime.os, "add_dll_directory", lambda d: calls.append(str(d)))
    assert register_gpu_runtime(tmp_path) is True
    # 应注册 cublas/cudnn/cuda_runtime/cuda_nvrtc 四个 bin 目录
    assert len(calls) == 4
    assert any("cublas" in c for c in calls)


def test_register_gpu_runtime_keeps_dll_handles(tmp_path: Path, monkeypatch):
    """add_dll_directory 返回的句柄必须被保存:句柄被 GC 回收会导致
    DLL 搜索路径失效(表现为模型加载成功、转写时 cublas64_12.dll 加载失败)。"""
    _mk_gpu_pack(tmp_path)
    monkeypatch.setattr(gpu_runtime.os, "add_dll_directory", lambda d: object())
    before = len(gpu_runtime._dll_handles)
    assert register_gpu_runtime(tmp_path) is True
    assert len(gpu_runtime._dll_handles) == before + 4


def test_register_gpu_runtime_prepends_path(tmp_path: Path, monkeypatch):
    """GPU bin 目录必须前置到 PATH:ctranslate2 延迟加载 cuBLAS 不使用
    Windows 安全搜索模式,add_dll_directory 对其不可见,必须靠 PATH。"""
    _mk_gpu_pack(tmp_path)
    monkeypatch.setattr(gpu_runtime.os, "add_dll_directory", lambda d: object())
    monkeypatch.setenv("PATH", r"C:\Windows\System32")
    assert register_gpu_runtime(tmp_path) is True
    path = gpu_runtime.os.environ["PATH"]
    # 4 个 bin 目录都应前置到 PATH 开头(逆序:后注册的在最前)
    parts = path.split(gpu_runtime.os.pathsep)
    first4 = parts[:4]
    assert any(p.endswith("cublas\\bin") for p in first4)
    assert any(p.endswith("cudnn\\bin") for p in first4)
    assert any(p.endswith("cuda_runtime\\bin") for p in first4)
    assert any(p.endswith("cuda_nvrtc\\bin") for p in first4)
    # 原 PATH 应保留在末尾
    assert path.endswith(r"C:\Windows\System32")


def test_register_gpu_runtime_missing_returns_false(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(gpu_runtime.os, "add_dll_directory", lambda d: None)
    assert register_gpu_runtime(tmp_path) is False


def test_register_gpu_runtime_fallback_path(monkeypatch):
    """无 add_dll_directory(非 Windows)时回退 PATH 追加。"""
    gpu_runtime.os = __import__("os")
    # 构造一个没有 add_dll_directory 的假 os
    import types
    fake_os = types.SimpleNamespace(
        environ=dict(gpu_runtime.os.environ),
        pathsep=gpu_runtime.os.pathsep,
    )
    monkeypatch.setattr(gpu_runtime, "os", fake_os)
    assert register_gpu_runtime(Path("/tmp/nonexistent")) is False


def test_ensure_gpu_or_cpu_non_windows(monkeypatch):
    monkeypatch.setattr(gpu_runtime.sys, "platform", "linux")
    assert gpu_runtime.ensure_gpu_or_cpu() == "cpu"


def test_ensure_gpu_or_cpu_missing_pack(monkeypatch):
    monkeypatch.setattr(gpu_runtime.sys, "platform", "win32")
    monkeypatch.setattr(gpu_runtime, "register_gpu_runtime", lambda *a, **k: False)
    monkeypatch.setattr(gpu_runtime, "default_gpu_dir",
                        lambda: Path("C:/nonexistent-gpu-dir"))
    assert gpu_runtime.ensure_gpu_or_cpu() == "cpu"


def test_ensure_gpu_or_cpu_registers_and_detects(monkeypatch):
    monkeypatch.setattr(gpu_runtime.sys, "platform", "win32")
    monkeypatch.setattr(gpu_runtime, "register_gpu_runtime", lambda *a, **k: True)

    class FakeCTranslate2:
        @staticmethod
        def get_cuda_device_count():
            return 1

    monkeypatch.setitem(gpu_runtime.sys.modules, "ctranslate2", FakeCTranslate2)
    assert gpu_runtime.ensure_gpu_or_cpu() == "cuda"


def test_ensure_gpu_or_cpu_registers_but_no_gpu(monkeypatch):
    monkeypatch.setattr(gpu_runtime.sys, "platform", "win32")
    monkeypatch.setattr(gpu_runtime, "register_gpu_runtime", lambda *a, **k: True)

    class FakeCTranslate2:
        @staticmethod
        def get_cuda_device_count():
            return 0

    monkeypatch.setitem(gpu_runtime.sys.modules, "ctranslate2", FakeCTranslate2)
    assert gpu_runtime.ensure_gpu_or_cpu() == "cpu"
