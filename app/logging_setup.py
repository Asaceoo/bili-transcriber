"""全局日志:文件日志(用户数据目录)+ stderr 控制台 + 未捕获异常钩子。

日志文件:%LOCALAPPDATA%/Bili Note/logs/app.log(安装版/便携版均写用户目录,
不受安装目录只读权限影响;滚动保留 3 个备份)。「运行日记」页签读取该文件分页展示。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def log_dir() -> Path:
    """日志目录:%LOCALAPPDATA%/Bili Note/logs(Windows),否则 ~/Bili Note/logs。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Bili Note" / "logs"


def log_file() -> Path:
    return log_dir() / "app.log"


def setup_logging() -> Path:
    """配置根日志器(文件 + stderr),返回日志文件路径。幂等,可重复调用。"""
    root = logging.getLogger()
    if getattr(root, "_bili_configured", False):
        return log_file()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    log_dir().mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(log_file(), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    root._bili_configured = True

    def _excepthook(etype, value, tb) -> None:
        logging.getLogger("uncaught").critical(
            "未捕获异常", exc_info=(etype, value, tb)
        )

    sys.excepthook = _excepthook
    logging.getLogger(__name__).info("日志系统已初始化:%s", log_file())
    return log_file()
