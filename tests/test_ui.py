"""主页面渲染冒烟测试。

本地覆盖 plugin 的 user fixture,改用 user_simulation(root=main_page) 的
root 模式:不依赖 nicegui user_plugin 的 main_file runpy 重放。原因:
其他测试文件(test_main_helpers)在收集阶段 import app.main 后,
app.ui.pages 进入 sys.modules 缓存,runpy 重放 main.py 时 @ui.page("/")
装饰器不会在新 app 上重新注册,页面 404。root 模式与 import 顺序解耦。
"""
from pathlib import Path

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


async def test_settings_has_channel_switches(user: User):
    """设置页要有 CLI / MCP 两个通道开关,并能看到 MCP 客户端配置。"""
    await user.open("/")
    user.find("设置").click()
    await user.should_see("AI 调用与命令行")
    await user.should_see("允许命令行(CLI)调用")
    await user.should_see("允许 MCP 调用")
    await user.should_see("MCP 客户端配置")


async def test_settings_has_smart_network_section(user: User):
    """网络与 Cookie 区块要有一键检测 / 测试 / 帮助三件套。"""
    await user.open("/")
    user.find("设置").click()
    await user.should_see("网络与 Cookie(下载视频用)")
    await user.should_see("一键检测本机网络与 Cookie")
    await user.should_see("② 测试连接")
    await user.should_see("各平台到底需不需要 Cookie / 代理?")
    await user.should_see("三种 Cookie 来源怎么选?")


async def test_detect_autofills_settings(user: User, monkeypatch):
    """点「一键检测」后,识别到的浏览器/代理要自动写进设置。"""
    from app import netcheck
    from app.ui import state

    monkeypatch.setattr(state.settings, "proxy", "")
    monkeypatch.setattr(state.settings, "cookies_from_browser", "")
    monkeypatch.setattr(state.settings, "cookie_file", "")
    monkeypatch.setattr(netcheck, "detect_browsers", lambda: [
        netcheck.BrowserInfo(key="chrome", label="Chrome", profile="Default",
                             cookie_db=Path("x"), cookie_count=8,
                             platforms={"bilibili": netcheck.PlatformCookie(
                                 "bilibili", "SESSDATA")}),
    ])
    monkeypatch.setattr(netcheck, "detect_cookie_files", lambda *a, **k: [])
    monkeypatch.setattr(netcheck, "probe_network", lambda **k: netcheck.NetworkReport(
        direct={p: netcheck.PlatformProbe(p, p != "youtube", 120) for p in netcheck.PLATFORMS},
        proxies=[netcheck.ProxyCandidate("http://127.0.0.1:7890", "系统代理", verified=True)],
        working_proxy="http://127.0.0.1:7890",
    ))

    await user.open("/")
    user.find("设置").click()
    user.find("一键检测本机网络与 Cookie").click()
    await user.should_see("检测完成,已按最优来源填好", retries=20)

    assert state.settings.cookies_from_browser == "chrome"
    assert state.settings.proxy == "http://127.0.0.1:7890"


async def test_test_button_renders_platform_results(user: User, monkeypatch):
    """点「测试连接」后要逐平台展示网络与 Cookie 状态。"""
    from app import netcheck

    def fake_test(browser="", cookie_file="", proxy="", timeout=4.0):
        return netcheck.TestReport(
            items=[
                netcheck.PlatformTest("bilibili", True, 80, "verified", "在线校验通过:测试用户"),
                netcheck.PlatformTest("youtube", False, 3000, "missing", "未找到登录 Cookie"),
            ],
            source="chrome 浏览器 Cookie",
            summary="配置可用:国内平台网络正常,已识别到登录 Cookie",
            ok=True,
        )

    monkeypatch.setattr(netcheck, "test_configuration", fake_test)

    await user.open("/")
    user.find("设置").click()
    user.find("② 测试连接").click()
    await user.should_see("在线校验通过:测试用户", retries=20)
    await user.should_see("不可达")
