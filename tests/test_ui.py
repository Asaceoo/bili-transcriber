import pytest
from nicegui.testing import User


async def test_main_page_renders(user: User):
    await user.open("/")
    await user.should_see("B站音频本地转写")
    await user.should_see("粘贴 B 站视频/合集链接")
    await user.should_see("输出目录")
