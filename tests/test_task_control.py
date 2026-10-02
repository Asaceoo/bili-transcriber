"""任务暂停/取消控制测试(纯逻辑,不依赖 UI/线程调度)。"""

import threading
import time

import pytest

from app.task_control import CancelledError, TaskControl


def test_default_state_is_running():
    c = TaskControl()
    assert c.check("job1") == "running"


def test_pause_then_resume():
    c = TaskControl()
    c.pause("job1")
    assert c.check("job1") == "paused"
    c.resume("job1")
    assert c.check("job1") == "running"


def test_cancel_sets_state():
    c = TaskControl()
    c.cancel("job1")
    assert c.check("job1") == "cancelled"


def test_wait_if_paused_blocks_until_resume():
    c = TaskControl()
    c.pause("job1")
    released: list[str] = []

    def worker():
        c.wait_if_paused("job1")
        released.append("ok")

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.1)
    assert not released  # 暂停中应阻塞
    c.resume("job1")
    t.join(timeout=2)
    assert released == ["ok"]


def test_wait_if_paused_raises_on_cancel():
    c = TaskControl()
    c.pause("job1")

    def worker():
        try:
            c.wait_if_paused("job1")
        except CancelledError:
            pass

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.1)
    c.cancel("job1")
    t.join(timeout=2)
    assert not t.is_alive()  # 取消后线程应退出(内部抛 CancelledError)


def test_wait_if_paused_raises_cancelled_error():
    c = TaskControl()
    c.cancel("job1")
    with pytest.raises(CancelledError):
        c.wait_if_paused("job1")


def test_wait_if_paused_passthrough_when_running():
    c = TaskControl()
    c.wait_if_paused("job1")  # 不应抛异常


def test_clear_removes_state():
    c = TaskControl()
    c.pause("job1")
    c.clear("job1")
    assert c.check("job1") == "running"
    c.wait_if_paused("job1")  # 不应抛异常
