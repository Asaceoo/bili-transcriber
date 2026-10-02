"""任务级暂停/取消控制(线程安全)。

工作线程在处理任务的关键节点调用 wait_if_paused() 检查控制信号:
  - 任务被暂停 -> 阻塞直到 resume() 或 cancel()
  - 任务被取消 -> 抛 CancelledError,上层捕获后把任务标记为 cancelled
"""

from __future__ import annotations

import threading


class CancelledError(Exception):
    """任务被用户取消。"""


class TaskControl:
    """管理每个任务的 running/paused/cancelled 状态。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, str] = {}
        self._resume_events: dict[str, threading.Event] = {}

    def pause(self, media_id: str) -> None:
        with self._lock:
            self._state[media_id] = "paused"
            self._resume_events.setdefault(media_id, threading.Event()).clear()

    def resume(self, media_id: str) -> None:
        with self._lock:
            self._state[media_id] = "running"
            ev = self._resume_events.get(media_id)
        if ev is not None:
            ev.set()

    def cancel(self, media_id: str) -> None:
        with self._lock:
            self._state[media_id] = "cancelled"
            ev = self._resume_events.get(media_id)
        if ev is not None:
            ev.set()  # 唤醒被暂停阻塞的线程,让它检查到 cancelled

    def check(self, media_id: str) -> str:
        with self._lock:
            return self._state.get(media_id, "running")

    def wait_if_paused(self, media_id: str) -> None:
        """若任务处于暂停则阻塞;被取消时抛 CancelledError。"""
        while True:
            with self._lock:
                state = self._state.get(media_id, "running")
                if state == "cancelled":
                    raise CancelledError(media_id)
                if state != "paused":
                    return
                ev = self._resume_events.setdefault(media_id, threading.Event())
            ev.wait(timeout=0.5)

    def clear(self, media_id: str) -> None:
        with self._lock:
            self._state.pop(media_id, None)
            self._resume_events.pop(media_id, None)
