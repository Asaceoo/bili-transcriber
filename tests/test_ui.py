"""主页面渲染冒烟测试。

本地覆盖 plugin 的 user fixture,改用 user_simulation(root=main_page) 的
root 模式:不依赖 nicegui user_plugin 的 main_file runpy 重放。原因:
其他测试文件(test_main_helpers)在收集阶段 import app.main 后,
app.ui.pages 进入 sys.modules 缓存,runpy 重放 main.py 时 @ui.page("/")
装饰器不会在新 app 上重新注册,页面 404。root 模式与 import 顺序解耦。
"""
import pytest
from nicegui.testing import User
from nicegui.testing.user_simulation import user_simulation

from app.ui.pages import main_page


@pytest.fixture
async def user() -> User:
    async with user_simulation(root=main_page) as u:
        yield u


async def test_main_page_renders(user: User):
    await user.open("/")
    await user.should_see("视频音频本地转写")
    await user.should_see("粘贴视频链接")
    await user.should_see("输出目录")
