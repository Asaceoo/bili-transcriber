"""Transcriber 模型外置加载逻辑测试(通过 monkeypatch 拦截 faster_whisper 导入)。"""

import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest

from app.transcriber import Segment, Transcriber


class FakeWhisperModel:
    """记录构造参数,模拟 faster_whisper.WhisperModel。"""

    instances: list[tuple] = []

    def __init__(self, model_ref, device, compute_type, **kwargs):
        FakeWhisperModel.instances.append((model_ref, device, compute_type))

    def transcribe(self, *a, **k):
        raise AssertionError("测试不应真正转写")


@pytest.fixture
def fake_faster_whisper(monkeypatch):
    """把 faster_whisper 模块替换为只有 WhisperModel 假类的模块。"""
    mod = ModuleType("faster_whisper")
    mod.WhisperModel = FakeWhisperModel
    FakeWhisperModel.instances = []
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return mod


def _mk_local_model(root: Path, name: str = "faster-whisper-large-v3-turbo") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "model.bin").write_bytes(b"x")
    (d / "config.json").write_text("{}")
    return d


def test_load_uses_local_model_dir_when_present(fake_faster_whisper, tmp_path: Path, monkeypatch):
    model_dir = _mk_local_model(tmp_path)
    monkeypatch.setenv("BILI_DEVICE", "cpu")   # 跳过 CUDA 探测
    monkeypatch.setenv("BILI_COMPUTE_TYPE", "int8")

    t = Transcriber(model_size="large-v3-turbo", model_dir=str(tmp_path))
    t.load()

    ref, device, compute = FakeWhisperModel.instances[0]
    assert ref == str(model_dir)          # 本地目录优先
    assert device == "cpu" and compute == "int8"


def test_load_falls_back_to_model_name_when_missing(fake_faster_whisper, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BILI_DEVICE", "cpu")
    monkeypatch.setenv("BILI_COMPUTE_TYPE", "int8")

    t = Transcriber(model_size="tiny", model_dir=str(tmp_path))  # 目录存在但无 tiny 模型
    t.load()

    ref, _, _ = FakeWhisperModel.instances[0]
    assert ref == "tiny"  # 回退在线下载


def test_load_ignores_model_dir_when_blank(fake_faster_whisper, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BILI_DEVICE", "cpu")
    monkeypatch.setenv("BILI_COMPUTE_TYPE", "int8")

    t = Transcriber(model_size="small", model_dir="")
    t.load()

    ref, _, _ = FakeWhisperModel.instances[0]
    assert ref == "small"


def test_segment_dataclass():
    s = Segment(1.0, 2.5, "文本")
    assert s.start == 1.0 and s.end == 2.5 and s.text == "文本"


# ---------- 设备解析 / GPU 运行时注册 ----------

def _patch_gpu_runtime(monkeypatch, cublas_ok: bool, cuda_count: int = 0,
                       register_ok: bool = True, register: callable | None = None):
    """把 ctypes.WinDLL、ctranslate2 探测和 register_gpu_runtime 替换为可控假实现。"""
    import ctypes as _ctypes
    import app.gpu_runtime
    if register is None:
        register = lambda *a, **k: register_ok  # noqa: E731
    monkeypatch.setattr(app.gpu_runtime, "register_gpu_runtime", register)

    class _WinDLLStub:
        def __init__(self, *a, **k):
            if not cublas_ok:
                raise OSError("cublas64_12.dll not found")

    class _Ctranslate2Stub:
        @staticmethod
        def get_cuda_device_count():
            return cuda_count

    monkeypatch.setattr(_ctypes, "WinDLL", _WinDLLStub)
    monkeypatch.setitem(sys.modules, "ctranslate2", _Ctranslate2Stub)


def _make_t(device: str, compute: str):
    import app.transcriber
    t = app.transcriber.Transcriber.__new__(app.transcriber.Transcriber)
    t.device = device
    t.compute_type = compute
    return t


def test_resolve_device_auto_registers_gpu_when_cuda_ok(monkeypatch):
    """device=auto + GPU 包就绪 + cuda 设备可见 -> cuda + float16。"""
    _patch_gpu_runtime(monkeypatch, cublas_ok=True, cuda_count=1, register_ok=True)
    dev, compute = _make_t("auto", "auto")._resolve()
    assert dev == "cuda" and compute == "float16"


def test_resolve_device_auto_falls_back_cpu_when_no_gpu(monkeypatch):
    """device=auto + GPU 不可用 -> cpu + int8。"""
    _patch_gpu_runtime(monkeypatch, cublas_ok=False, cuda_count=0, register_ok=False)
    dev, compute = _make_t("auto", "auto")._resolve()
    assert dev == "cpu" and compute == "int8"


def test_resolve_device_auto_falls_back_cpu_when_cublas_unloadable(monkeypatch):
    """device=auto + GPU 包在但 cublas 无法加载 -> cpu + int8(不崩)。"""
    _patch_gpu_runtime(monkeypatch, cublas_ok=False, cuda_count=0, register_ok=True)
    dev, compute = _make_t("auto", "auto")._resolve()
    assert dev == "cpu" and compute == "int8"


def test_resolve_device_explicit_cuda_registers_gpu_and_succeeds(monkeypatch):
    """回归:显式 device=cuda(硬件检测持久化)时同样注册 GPU 运行时。

    此前只在 device=auto 时注册,持久化 device=cuda 会在转写 encode 阶段
    因 cublas 延迟加载失败。现在即便 device 已是 cuda 也会走注册+提前验证。
    """
    calls = []
    register = lambda *a, **k: calls.append(True) or True  # noqa: E731
    _patch_gpu_runtime(monkeypatch, cublas_ok=True, cuda_count=1, register=register)
    dev, compute = _make_t("cuda", "float16")._resolve()
    assert calls, "显式 device=cuda 也应当触发 GPU 运行时注册"
    assert dev == "cuda" and compute == "float16"


def test_resolve_device_explicit_cuda_falls_back_cpu_when_gpu_missing(monkeypatch):
    """回归:显式 device=cuda 但 GPU 包缺失/DLL 不可加载 -> 回退 CPU + int8。

    此前持久化的 device=cuda 在无 GPU 包时会直接崩,现在自动回退 CPU。
    """
    _patch_gpu_runtime(monkeypatch, cublas_ok=False, cuda_count=0, register_ok=False)
    dev, compute = _make_t("cuda", "auto")._resolve()
    assert dev == "cpu" and compute == "int8"


def test_resolve_device_explicit_cuda_falls_back_when_cublas_unloadable(monkeypatch):
    """回归:GPU 包在但 cublas 无法加载(转写 encode 期崩的场景)-> 提前回退 CPU。"""
    _patch_gpu_runtime(monkeypatch, cublas_ok=False, cuda_count=0, register_ok=True)
    dev, compute = _make_t("cuda", "auto")._resolve()
    assert dev == "cpu" and compute == "int8"


def test_resolve_device_explicit_cuda_falls_back_when_no_cuda_device(monkeypatch):
    """回归:cublas 可加载但 ctranslate2 探测不到 CUDA 设备 -> 回退 CPU。"""
    _patch_gpu_runtime(monkeypatch, cublas_ok=True, cuda_count=0, register_ok=True)
    dev, compute = _make_t("cuda", "auto")._resolve()
    assert dev == "cpu" and compute == "int8"


def test_resolve_device_env_override_cpu(monkeypatch):
    """BILI_DEVICE=cpu(单文件版 hook)直接使用,不做任何 GPU 探测。"""
    monkeypatch.setenv("BILI_DEVICE", "cpu")
    monkeypatch.setenv("BILI_COMPUTE_TYPE", "int8")
    dev, compute = _make_t("cuda", "float16")._resolve()
    assert dev == "cpu" and compute == "int8"


def test_load_loads_model_once_under_concurrent_workers(fake_faster_whisper, tmp_path, monkeypatch):
    """回归:WORKERS=2 共享同一 transcriber,并发首次转写只应加载一次模型。

    修复前 load() 判空在锁外、赋值在锁内,两个线程会各自通过前置判空、
    再轮流持锁各加载一次(FakeWhisperModel.instances 出现 2 条)。
    修复后判空+加载整体在锁内,仅 1 条。
    """
    monkeypatch.setenv("BILI_DEVICE", "cpu")
    monkeypatch.setenv("BILI_COMPUTE_TYPE", "int8")
    _mk_local_model(tmp_path)
    t = Transcriber(model_size="large-v3-turbo", model_dir=str(tmp_path))

    barrier = threading.Barrier(2)
    errors: list = []

    def worker() -> None:
        try:
            barrier.wait()
            t.load()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=15)

    assert not errors, f"load() 并发执行抛错: {errors}"
    assert len(FakeWhisperModel.instances) == 1, (
        f"并发下模型被加载了 {len(FakeWhisperModel.instances)} 次,应为 1 次"
    )
    # 已加载后再次 load 不应重复创建
    t.load()
    assert len(FakeWhisperModel.instances) == 1
