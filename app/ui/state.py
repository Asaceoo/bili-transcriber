"""UI 共享状态:路径常量 + 全局单例(store/settings/pipeline/events)。

原本在 main.py 顶部;拆出后由 helpers/pages 共享。单例在模块首次导入时创建,
与拆分前 main.py 的行为一致(测试经 monkeypatch app.ui.helpers.* 替换引用)。
"""

from __future__ import annotations

import logging
import queue
import sys
from collections import deque
from pathlib import Path

from app.downloader import Downloader
from app.logging_setup import log_file
from app.paths import base_dir, data_dir
from app.pipeline import ACTIVE_STATUSES, Pipeline  # noqa: F401 — 再导出供 pages/helpers 使用
from app.store import Store, load_settings

logger = logging.getLogger(__name__)

BASE_DIR = base_dir()
DATA_DIR = data_dir()
# 应用图标资产的运行时定位:开发=app/assets;onedir=exe 旁 _internal/frozen;
# onefile=解压缓存 sys._MEIPASS。取第一个存在的目录。
_meipass = getattr(sys, "_MEIPASS", None)
_candidate_assets = [
    BASE_DIR / "app" / "assets",
    BASE_DIR / "_internal" / "app" / "assets",
    Path(_meipass) / "app" / "assets" if _meipass else None,
]
ASSETS_DIR = next((d for d in _candidate_assets if d and d.is_dir()), None)
APP_ICON = next(ASSETS_DIR.glob("app_256.png")) if ASSETS_DIR else None

STATUS_LABEL = {
    "queued": "排队中", "downloading": "下载中", "converting": "转码中",
    "transcribing": "转写中", "extracting": "截帧中", "saving": "保存中",
    "done": "已完成", "failed": "失败",
    "paused": "已暂停", "cancelled": "已取消",
}
MODEL_OPTIONS = ["large-v3-turbo", "large-v3", "medium", "small", "base", "tiny"]

store = Store(DATA_DIR / "history.db")
settings = load_settings(DATA_DIR / "settings.json")
events: deque[str] = deque(maxlen=200)
hw_results: queue.Queue = queue.Queue()  # 后台硬件检测结果 -> UI 线程回填
pipeline = Pipeline(
    downloader=Downloader(), store=store, settings=settings,
    base_dir=BASE_DIR, on_event=events.append,
)

# 运行日记分页状态(运行日记页签读取日志文件分页展示)
diary_state = {"page": 1, "page_size": 100, "total_pages": 1}


def diary_lines() -> list[str]:
    try:
        return log_file().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
