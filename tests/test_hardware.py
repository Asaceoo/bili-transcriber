"""硬件检测测试(纯逻辑,不加载真实 CUDA DLL / 不调用真实子进程)。"""

from app import hardware


def _mk_info(**kw) -> hardware.HardwareInfo:
    return hardware.HardwareInfo(**kw)


def test_cpu_name_fallback(monkeypatch):
    monkeypatch.setattr(hardware.sys, "platform", "linux")
    monkeypatch.setattr(hardware.platform, "processor", lambda: "TestCPU")
    assert hardware._cpu_name() == "TestCPU"


def test_gpu_info_empty_on_non_windows(monkeypatch):
    monkeypatch.setattr(hardware.sys, "platform", "linux")
    assert hardware._gpu_info() == ("", 0)


def test_detect_hardware_cuda_available(monkeypatch):
    """Whisper 引擎 + CUDA 可用 -> 建议 cuda + float16。"""
    monkeypatch.setattr(hardware, "_cpu_name", lambda: "CPU")
    monkeypatch.setattr(hardware, "_gpu_info", lambda: ("NVIDIA RTX 4090", 24576))
    monkeypatch.setattr(hardware, "_probe_cuda", lambda: True)
    monkeypatch.setattr("app.gpu_runtime.gpu_available", lambda *a, **k: True)

    r = hardware.detect_hardware(engine="whisper")
    assert r.cuda_available is True
    assert r.device == "cuda"
    assert r.compute_type == "float16"
    assert "CUDA" in r.recommendation


def test_detect_hardware_pack_but_no_cuda(monkeypatch):
    """Whisper 引擎:GPU 包存在但 CUDA 初始化失败 -> 建议 CPU。"""
    monkeypatch.setattr(hardware, "_cpu_name", lambda: "CPU")
    monkeypatch.setattr(hardware, "_gpu_info", lambda: ("NVIDIA RTX 4090", 24576))
    monkeypatch.setattr(hardware, "_probe_cuda", lambda: False)
    monkeypatch.setattr("app.gpu_runtime.gpu_available", lambda *a, **k: True)

    r = hardware.detect_hardware(engine="whisper")
    assert r.gpu_pack_present is True
    assert r.cuda_available is False
    assert r.device == "cpu"
    assert r.compute_type == "int8"


def test_detect_hardware_gpu_no_pack(monkeypatch):
    """Whisper 引擎:有显卡但未放 GPU 包 -> 建议 CPU 并提示下载 GPU 包。"""
    monkeypatch.setattr(hardware, "_cpu_name", lambda: "CPU")
    monkeypatch.setattr(hardware, "_gpu_info", lambda: ("NVIDIA GTX 1660", 6144))
    monkeypatch.setattr("app.gpu_runtime.gpu_available", lambda *a, **k: False)

    r = hardware.detect_hardware(engine="whisper")
    assert r.cuda_available is False
    assert r.device == "cpu"
    assert "GPU 加速包" in r.recommendation


def test_detect_hardware_cpu_only(monkeypatch):
    """Whisper 引擎:无独显 -> 建议 CPU + int8。"""
    monkeypatch.setattr(hardware, "_cpu_name", lambda: "CPU")
    monkeypatch.setattr(hardware, "_gpu_info", lambda: ("", 0))
    monkeypatch.setattr("app.gpu_runtime.gpu_available", lambda *a, **k: False)

    r = hardware.detect_hardware(engine="whisper")
    assert r.device == "cpu"
    assert r.compute_type == "int8"
    assert "未检测到独立显卡" in r.recommendation


def test_detect_hardware_qwen_torch_no_cuda(monkeypatch):
    """Qwen3-ASR 引擎 + torch 未启用 CUDA -> 建议 CPU + float32,绝不给 cuda。

    这是回归测试:此前硬件检测会把 whisper 的 cuda 建议应用到 qwen 引擎,
    在 torch CPU-only 构建上抛 "Torch not compiled with CUDA enabled" 崩溃。
    """
    monkeypatch.setattr(hardware, "_cpu_name", lambda: "CPU")
    monkeypatch.setattr(hardware, "_gpu_info", lambda: ("NVIDIA RTX 4090", 24576))
    monkeypatch.setattr(hardware, "_probe_cuda", lambda: True)  # whisper 可用 GPU
    monkeypatch.setattr(hardware, "_probe_torch_cuda", lambda: False)  # 但 torch CPU-only
    monkeypatch.setattr("app.gpu_runtime.gpu_available", lambda *a, **k: True)

    r = hardware.detect_hardware(engine="qwen3-asr")
    assert r.engine == "qwen3-asr"
    assert r.gpu_pack_present is True
    # 即便 ctranslate2 可见 CUDA、GPU 包已放,torch 不支持也绝不能建议 qwen 用 cuda
    assert r.cuda_available is False
    assert r.torch_cuda_available is False
    assert r.device == "cpu"
    assert r.compute_type == "float32"
    assert "PyTorch" in r.recommendation
    assert "Whisper" in r.recommendation


def test_detect_hardware_qwen_torch_cuda(monkeypatch):
    """Qwen3-ASR 引擎 + torch 支持 CUDA -> 建议 cuda + bfloat16。"""
    monkeypatch.setattr(hardware, "_cpu_name", lambda: "CPU")
    monkeypatch.setattr(hardware, "_gpu_info", lambda: ("NVIDIA RTX 4090", 24576))
    monkeypatch.setattr(hardware, "_probe_cuda", lambda: True)
    monkeypatch.setattr(hardware, "_probe_torch_cuda", lambda: True)
    monkeypatch.setattr("app.gpu_runtime.gpu_available", lambda *a, **k: True)

    r = hardware.detect_hardware(engine="qwen3-asr")
    assert r.torch_cuda_available is True
    assert r.device == "cuda"
    assert r.compute_type == "bfloat16"


def test_detect_hardware_sensevoice_always_cpu(monkeypatch):
    """SenseVoice 引擎固定 CPU + int8(内置 onnxruntime 仅 CPU),绝不给 cuda 建议。"""
    monkeypatch.setattr(hardware, "_cpu_name", lambda: "CPU")
    monkeypatch.setattr(hardware, "_gpu_info", lambda: ("NVIDIA RTX 4090", 24576))
    monkeypatch.setattr(hardware, "_probe_cuda", lambda: True)
    monkeypatch.setattr("app.gpu_runtime.gpu_available", lambda *a, **k: True)

    r = hardware.detect_hardware(engine="sensevoice")
    assert r.engine == "sensevoice"
    assert r.device == "cpu"
    assert r.compute_type == "int8"
    assert "SenseVoice" in r.recommendation
