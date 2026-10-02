"""系统托盘:托盘图标 + 打开主窗口 + 退出。

基于 pystray(PIL 提供图标)。在独立守护线程运行,不阻塞主流程;
托盘不可用(缺依赖/环境异常)时静默降级,不影响主程序。
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from typing import Callable

logger = logging.getLogger(__name__)


class SystemTray:
    """系统托盘图标。

    - 左键/双击:打开主窗口(默认浏览器打开应用 URL)
    - 右键菜单:打开主窗口 / 退出
    """

    def __init__(self, url: str, icon_path: str, title: str = "B站音频本地转写",
                 on_exit: Callable[[], None] | None = None):
        self.url = url
        self.icon_path = icon_path
        self.title = title
        self.on_exit = on_exit
        self._icon = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._icon is not None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="system-tray", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            import pystray
            from PIL import Image
        except Exception as exc:  # noqa: BLE001 — 托盘不可用不应拖垮主程序
            logger.warning("系统托盘不可用(缺少 pystray/PIL): %s", exc)
            return
        try:
            image = Image.open(self.icon_path)
        except Exception:  # noqa: BLE001 — 图标加载失败时用内置占位
            logger.warning("托盘图标加载失败,使用占位图标: %s", self.icon_path)
            image = Image.new("RGBA", (64, 64), (0, 120, 215, 255))
        menu = pystray.Menu(
            pystray.MenuItem("打开主窗口", self._open_main, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._quit),
        )
        self._icon = pystray.Icon("bili-transcriber", image, self.title, menu)
        try:
            self._icon.run()
        except Exception:  # noqa: BLE001
            logger.exception("托盘运行失败")

    def _open_main(self, icon=None, item=None) -> None:
        try:
            webbrowser.open(self.url)
        except Exception:  # noqa: BLE001
            logger.exception("打开主窗口失败")

    def _quit(self, icon=None, item=None) -> None:
        try:
            if self._icon is not None:
                self._icon.stop()
        except Exception:  # noqa: BLE001
            pass
        if self.on_exit is not None:
            try:
                self.on_exit()
            except Exception:  # noqa: BLE001
                logger.exception("托盘退出回调失败")

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
