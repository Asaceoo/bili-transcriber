"""运行时路径解析(GUI / CLI / MCP 共用)。

冻结(PyInstaller)时以 exe 所在目录为根;源码运行时以项目根为根。
CLI/MCP 与 GUI 必须指向同一份 data/(history.db + settings.json),否则
AI 调用看不到桌面端的历史与设置。
"""

from __future__ import annotations

import sys
from pathlib import Path


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    return base_dir() / "data"
