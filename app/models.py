"""模型外置:模型目录约定、扫描检测、本地路径解析。

用户只下载软件,模型包自行下载放入模型目录(默认 %LOCALAPPDATA%/Bili Note/models,
安装版无写权限,必须走用户目录;可在设置中改 model_dir)。软件启动时扫描目录,
识别已放置的模型包,转写时优先从本地目录加载,未命中才走在线下载。

支持的模型包目录结构(用户解压后整体放入模型目录):
    faster-whisper-large-v3-turbo/   # HF: Systran/faster-whisper-* (ct2 格式)
        model.bin config.json tokenizer.json vocabulary.txt
    Qwen3-ASR-0.6B/                  # ModelScope: Qwen/Qwen3-ASR-0.6B (transformers 格式)
        config.json model.safetensors tokenizer.json ...
    SenseVoiceSmall/                 # ModelScope: iic/SenseVoiceSmall (funasr 格式)
        config.yaml model.pt tokens.json am.mvn ...
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# 引擎标识(与 Settings.engine 对齐)
ENGINE_WHISPER = "whisper"
ENGINE_QWEN3_ASR = "qwen3-asr"
ENGINE_SENSEVOICE = "sensevoice"


@dataclass
class DetectedModel:
    """模型目录中检测到的一个模型包。"""

    engine: str      # ENGINE_* 之一
    name: str        # 目录名,如 large-v3-turbo / Qwen3-ASR-0.6B / SenseVoiceSmall
    path: Path       # 模型包目录绝对路径
    valid: bool = True            # 模型包是否完整可加载
    missing: list[str] = field(default_factory=list)  # 缺失/异常文件清单
    detail: str = ""              # 额外人类可读说明


def default_model_dir() -> Path:
    """默认模型目录:%LOCALAPPDATA%/Bili Note/models(Windows),否则 ~/Bili Note/models。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Bili Note" / "models"


def resolve_model_dir(configured: str) -> Path:
    """设置值 -> 实际模型目录;空值回退默认目录。

    注意:Windows 打包环境下 LOCALAPPDATA 通常仍正确,若为空则回退到用户目录。
    """
    if configured and configured.strip():
        p = Path(configured.strip())
        return p if p.is_absolute() else (Path.home() / p)
    return default_model_dir()


def _is_whisper_dir(path: Path) -> tuple[bool, list[str]]:
    """faster-whisper 目录特征:model.bin + config.json(ct2 格式)。"""
    missing = []
    for name in ("model.bin", "config.json"):
        if not (path / name).is_file():
            missing.append(name)
    return (not missing), missing


def _is_qwen_asr_dir(path: Path) -> tuple[bool, list[str]]:
    """Qwen3-ASR 目录特征:config.json + 至少一个权重文件。"""
    missing = []
    if not (path / "config.json").is_file():
        missing.append("config.json")
    has_weight = any((path / name).is_file() for name in (
        "model.safetensors", "pytorch_model.bin", "model.safetensors.index.json"
    ))
    if not has_weight:
        missing.append("model.safetensors|pytorch_model.bin")
    return (not missing), missing


def _is_sensevoice_dir(path: Path) -> tuple[bool, list[str]]:
    """SenseVoice 目录特征:两种格式任一完整即可。

    - sherpa-onnx 格式(推荐):model.int8.onnx + tokens.txt
    - funasr 格式:config.yaml + model.pt
    """
    missing = []
    sherpa_ok = (path / "model.int8.onnx").is_file() and (path / "tokens.txt").is_file()
    if sherpa_ok:
        return True, []
    for name in ("config.yaml", "model.pt"):
        if not (path / name).is_file():
            missing.append(name)
    return (not missing), missing


def scan_models(model_dir: Path) -> list[DetectedModel]:
    """扫描模型目录,返回所有已识别的模型包(按目录名排序)。"""
    if not model_dir.is_dir():
        return []
    found: list[DetectedModel] = []
    for child in sorted(model_dir.iterdir()):
        if not child.is_dir():
            continue
        w_ok, w_missing = _is_whisper_dir(child)
        q_ok, q_missing = _is_qwen_asr_dir(child)
        s_ok, s_missing = _is_sensevoice_dir(child)
        if w_ok:
            engine, name = ENGINE_WHISPER, child.name
            if child.name.startswith("faster-whisper-"):
                name = child.name[len("faster-whisper-"):]
            found.append(DetectedModel(engine=engine, name=name, path=child, valid=True))
        elif q_ok:
            found.append(DetectedModel(engine=ENGINE_QWEN3_ASR, name=child.name, path=child, valid=True))
        elif s_ok:
            found.append(DetectedModel(engine=ENGINE_SENSEVOICE, name=child.name, path=child, valid=True))
        elif w_missing and not q_ok and not s_ok and _looks_like_model(child):
            # 目录看起来像模型但关键文件缺失 -> 标记为不完整
            if (child / "config.json").is_file() and not (child / "model.bin").is_file():
                found.append(DetectedModel(
                    engine=ENGINE_QWEN3_ASR, name=child.name, path=child,
                    valid=False, missing=q_missing,
                    detail="疑似 Qwen3-ASR,但缺少权重文件",
                ))
            else:
                found.append(DetectedModel(
                    engine=ENGINE_WHISPER, name=child.name, path=child,
                    valid=False, missing=w_missing,
                    detail="疑似 faster-whisper,但缺少关键文件",
                ))
    return found


def _looks_like_model(path: Path) -> bool:
    """目录是否至少包含一个模型常见文件(用于识别"残缺"模型包)。"""
    hints = (
        "config.json", "config.yaml", "model.bin", "model.safetensors",
        "pytorch_model.bin", "tokenizer.json", "tokenizer.yaml", "vocabulary.txt",
    )
    return any((path / name).is_file() for name in hints)


def find_whisper_model(model_dir: Path, model_size: str) -> Path | None:
    """在模型目录中查找指定 faster-whisper 模型包,返回目录路径或 None。

    兼容两种目录命名:faster-whisper-{model_size}(官方仓库名)与 {model_size}(用户重命名)。
    """
    for candidate in (f"faster-whisper-{model_size}", model_size):
        p = model_dir / candidate
        ok, _ = _is_whisper_dir(p)
        if ok:
            return p
    return None


def find_qwen_asr_model(model_dir: Path, model_name: str) -> Path | None:
    """在模型目录中查找指定 Qwen3-ASR 模型包,返回目录路径或 None。

    兼容目录命名:Qwen3-ASR-0.6B / Qwen3-ASR-1.7B / 用户自定义名(只要 config.json + 权重存在)。
    匹配策略:精确优先 → 版本号兼容 → 唯一 Qwen 目录兜底(仅当用户未指定明确版本号时)。
    """
    if not model_dir.is_dir():
        return None

    # 1) 精确匹配:model_name 本身就是目录名,或加上官方前缀
    for candidate in (model_name, f"Qwen3-ASR-{model_name}"):
        p = model_dir / candidate
        ok, _ = _is_qwen_asr_dir(p)
        if ok:
            return p

    # 2) 版本号兼容:model_name 是 "0.6B"/"1.7B"/"0.5B" 等明确版本时,
    #    要求目录名含该版本且是 Qwen 目录。
    looks_like_version = bool(re.fullmatch(r"v?\d+(\.\d+)?[bB]?", model_name.strip()))
    version = model_name.lower().lstrip("v").rstrip("b")
    if looks_like_version and version:
        for child in sorted(model_dir.iterdir()):
            if not child.is_dir():
                continue
            lower = child.name.lower()
            if version in lower and "qwen" in lower:
                ok, _ = _is_qwen_asr_dir(child)
                if ok:
                    return child
        # 明确要求具体版本但未命中 -> 不兜底,避免用户选 1.7B 却跑到 0.6B 上
        return None

    # 3) 兜底:如果模型目录只有一个有效 Qwen 目录,返回它(适用于用户自定义名/未指定版本)
    qwen_hits = []
    for child in sorted(model_dir.iterdir()):
        if not child.is_dir():
            continue
        if "qwen" in child.name.lower():
            ok, _ = _is_qwen_asr_dir(child)
            if ok:
                qwen_hits.append(child)
    if len(qwen_hits) == 1:
        return qwen_hits[0]
    return None


def find_sensevoice_model(model_dir: Path, model_name: str) -> Path | None:
    """在模型目录中查找 SenseVoice 模型包,返回目录路径或 None。

    兼容目录命名:sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17(官方名)、
    SenseVoiceSmall(默认名)、用户自定义名(只要含 model.int8.onnx + tokens.txt)。
    匹配策略:精确优先 → 含 "sensevoice"/"sense-voice" 关键字 → 唯一 SenseVoice 目录兜底。
    """
    if not model_dir.is_dir():
        return None

    # 1) 精确匹配:model_name 本身就是目录名,或含官方前缀
    for candidate in (model_name, f"sherpa-onnx-{model_name}"):
        p = model_dir / candidate
        ok, _ = _is_sensevoice_dir(p)
        if ok:
            return p

    # 2) 关键字匹配:目录名含 sensevoice / sense-voice
    for child in sorted(model_dir.iterdir()):
        if not child.is_dir():
            continue
        lower = child.name.lower()
        if "sensevoice" in lower or "sense-voice" in lower:
            ok, _ = _is_sensevoice_dir(child)
            if ok:
                return child

    # 3) 兜底:如果模型目录只有一个有效 SenseVoice 目录,返回它(适用于用户自定义名)
    sense_hits = []
    for child in sorted(model_dir.iterdir()):
        if not child.is_dir():
            continue
        ok, _ = _is_sensevoice_dir(child)
        if ok:
            sense_hits.append(child)
    if len(sense_hits) == 1:
        return sense_hits[0]
    return None


def describe_models(model_dir: Path) -> str:
    """人类可读的模型目录状态摘要(设置页展示)。"""
    if not model_dir.is_dir():
        return f"目录不存在:{model_dir}\n请点击「打开模型目录」自动创建,然后放入模型包。"
    models = scan_models(model_dir)
    if not models:
        return f"未检测到模型包\n请在 {model_dir} 下放置 faster-whisper 或 Qwen3-ASR 模型目录。"
    lines = []
    for m in models:
        status = "✓" if m.valid else "✗ 不完整"
        line = f"{status} {m.name} ({m.engine})"
        if m.missing:
            line += f" 缺:{','.join(m.missing)}"
        if m.detail:
            line += f" — {m.detail}"
        lines.append(line)
    return "\n".join(lines)
