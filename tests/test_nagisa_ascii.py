"""回归测试:安装路径/用户名含中文时,nagisa 模型(dynet C++ 读取)必须能从 ASCII 缓存加载。

背景:应用安装在含中文目录(如 D:\\B站音频本地转写\\)或 Windows 用户名含中文时,
nagisa 通过 dynet 的 std::ifstream 读模型失败,报 "Could not read model from ..."。
修复:preload_transformers.py 在启动时把 nagisa 包目录复制到 ASCII 缓存目录。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

NAGISA_SRC = Path(sys.prefix) / "Lib" / "site-packages" / "nagisa"


def _chinese_nagisa_copy(tmp_path: Path) -> Path:
    """把 venv 的 nagisa 复制到一个含中文的路径下,模拟中文安装目录。"""
    chinese_root = tmp_path / "中文安装目录"
    chinese_root.mkdir()
    target = chinese_root / "nagisa"
    shutil.copytree(NAGISA_SRC, target)
    return target


def _run_fix(chinese_parent: Path, cache_root: Path) -> str:
    """在子进程中运行 preload_transformers 的 nagisa 修复逻辑并导入 nagisa,返回输出。"""
    build_dir = Path(__file__).resolve().parent.parent / "build"
    code = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(build_dir)!r})\n"
        f"sys.path.insert(0, {str(chinese_parent)!r})\n"
        "import preload_transformers\n"
        "import nagisa\n"
        "print('NAGISA_FILE=' + nagisa.__file__)\n"
    )
    env = dict(os.environ)
    env["BILI_NAGISA_CACHE"] = str(cache_root)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(Path(__file__).resolve().parent.parent),
        timeout=180,
    )
    return proc.stdout + proc.stderr


def test_nagisa_loads_from_ascii_cache_when_path_is_chinese(tmp_path: Path):
    chinese_nagisa = _chinese_nagisa_copy(tmp_path)
    cache_root = tmp_path / "ascii_cache"
    out = _run_fix(chinese_nagisa.parent, cache_root)
    assert "NAGISA_FILE=" in out, f"nagisa 未成功加载:\n{out}"
    loaded = out.split("NAGISA_FILE=")[1].strip()
    # 必须从 ASCII 缓存加载,而不是中文路径
    assert str(cache_root.resolve()) in loaded, f"nagisa 应从 ASCII 缓存加载,实际: {loaded}"
    assert cache_root.joinpath("nagisa", "data", "nagisa_v001.model").exists()


def test_nagisa_ascii_cache_reused_on_second_run(tmp_path: Path):
    """第二次运行应复用缓存,不重复复制。"""
    chinese_nagisa = _chinese_nagisa_copy(tmp_path)
    cache_root = tmp_path / "ascii_cache"
    _run_fix(chinese_nagisa.parent, cache_root)
    model = cache_root / "nagisa" / "data" / "nagisa_v001.model"
    mtime = model.stat().st_mtime_ns
    out = _run_fix(chinese_nagisa.parent, cache_root)
    assert "NAGISA_FILE=" in out
    assert model.stat().st_mtime_ns == mtime, "缓存应被复用而非重新复制"


def test_ascii_path_skips_copy(tmp_path: Path):
    """ASCII 路径不应触发复制。"""
    ascii_nagisa = tmp_path / "nagisa"
    shutil.copytree(NAGISA_SRC, ascii_nagisa)
    cache_root = tmp_path / "ascii_cache"
    out = _run_fix(ascii_nagisa.parent, cache_root)
    assert "NAGISA_FILE=" in out
    loaded = out.split("NAGISA_FILE=")[1].strip()
    assert str(ascii_nagisa) in loaded, f"ASCII 路径应直接使用,实际: {loaded}"
    assert not cache_root.exists(), "ASCII 路径不应产生缓存"
