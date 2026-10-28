"""界面形态(原生窗口 / 浏览器)与单实例唤醒的回归测试。

对应 0.1.41 修复:
- 双击 exe 默认弹独立原生窗口,不再把用户丢进浏览器;
  `window_size` 会强制 native,所以只有真要原生窗口时才传它。
- 第二个实例不再"开个浏览器标签页然后自己退出"(用户看到的"自动退出"),
  改为轮询等待已有窗口并提到前台。
- WebView2 用户数据目录固定到 data/webview2:pywebview 默认用
  `tempfile.TemporaryDirectory().name`,那个临时对象当场被回收、目录被删,
  会导致程序跑几分钟后静默退出。
"""

import sys

import pytest

import app.main as main_mod
from app.tray import SystemTray


# ---------- _env_flag ----------

@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("", False),
])
def test_env_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("BILI_TEST_FLAG", raw)
    assert main_mod._env_flag("BILI_TEST_FLAG") is expected


def test_env_flag_unset(monkeypatch):
    monkeypatch.delenv("BILI_TEST_FLAG_ABSENT", raising=False)
    assert main_mod._env_flag("BILI_TEST_FLAG_ABSENT") is False


# ---------- _resolve_ui_mode ----------

@pytest.fixture
def win32(monkeypatch):
    monkeypatch.setattr(main_mod.sys, "platform", "win32")
    for name in ("BILI_FORCE_BROWSER", "BILI_FORCE_NATIVE", "BILI_NO_BROWSER"):
        monkeypatch.delenv(name, raising=False)


def test_default_is_native_window_when_webview2_present(win32, monkeypatch):
    """有 WebView2 就默认原生窗口,且不再 show 浏览器。"""
    monkeypatch.setattr(main_mod, "_has_webview2", lambda: True)
    assert main_mod._resolve_ui_mode() == (True, False)


def test_fallback_to_browser_without_webview2(win32, monkeypatch):
    monkeypatch.setattr(main_mod, "_has_webview2", lambda: False)
    assert main_mod._resolve_ui_mode() == (False, True)


def test_force_browser_wins(win32, monkeypatch):
    """出问题时用户可以用 BILI_FORCE_BROWSER=1 退回浏览器。"""
    monkeypatch.setattr(main_mod, "_has_webview2", lambda: True)
    monkeypatch.setenv("BILI_FORCE_BROWSER", "1")
    assert main_mod._resolve_ui_mode() == (False, True)


def test_force_native_without_webview2(win32, monkeypatch):
    monkeypatch.setattr(main_mod, "_has_webview2", lambda: False)
    monkeypatch.setenv("BILI_FORCE_NATIVE", "1")
    assert main_mod._resolve_ui_mode() == (True, False)


def test_no_browser_env_suppresses_show(win32, monkeypatch):
    monkeypatch.setattr(main_mod, "_has_webview2", lambda: False)
    monkeypatch.setenv("BILI_NO_BROWSER", "1")
    assert main_mod._resolve_ui_mode() == (False, False)


def test_non_windows_uses_browser(monkeypatch):
    monkeypatch.setattr(main_mod.sys, "platform", "linux")
    monkeypatch.delenv("BILI_FORCE_NATIVE", raising=False)
    assert main_mod._resolve_ui_mode()[0] is False


# ---------- _activate_existing_window ----------

def test_activate_returns_false_on_non_windows(monkeypatch):
    monkeypatch.setattr(main_mod.sys, "platform", "linux")
    assert main_mod._activate_existing_window(wait_s=0) is False


def test_activate_finds_window_and_raises_it(win32, monkeypatch):
    monkeypatch.setattr(main_mod, "_find_window", lambda title=main_mod.WINDOW_TITLE: 1234)
    calls = []
    monkeypatch.setattr(main_mod, "_activate_window", lambda hwnd: calls.append(hwnd) or True)
    assert main_mod._activate_existing_window(wait_s=0) is True
    assert calls == [1234]


def test_activate_gives_up_when_no_window(win32, monkeypatch):
    """没有窗口时不能卡住:wait_s=0 立即返回 False(调用方据此决定提示或兜底)。"""
    monkeypatch.setattr(main_mod, "_find_window", lambda title=main_mod.WINDOW_TITLE: 0)
    assert main_mod._activate_existing_window(wait_s=0) is False


def test_activate_waits_for_late_window(win32, monkeypatch):
    """冷启动窗口十几秒才出现:轮询期间一出现就要立刻认到,不能提前放弃。"""
    seen = {"n": 0}

    def fake_find(title=main_mod.WINDOW_TITLE):
        seen["n"] += 1
        return 4321 if seen["n"] >= 3 else 0

    monkeypatch.setattr(main_mod, "_find_window", fake_find)
    monkeypatch.setattr(main_mod, "_activate_window", lambda hwnd: True)
    assert main_mod._activate_existing_window(wait_s=5) is True
    assert seen["n"] >= 3


# ---------- _configure_native_window ----------

def test_configure_native_window_pins_webview2_storage(tmp_path, monkeypatch):
    """必须显式给 storage_path:pywebview 默认的临时目录会被回收删掉。

    走 `app.native.start_args`(即 `webview.start(**...)` 的入参),不碰
    `app.native.settings` —— 后者是只允许改已有键的 ImmutableDict,
    多塞一个键会让开窗子进程抛 KeyError,窗口直接起不来。
    """
    from nicegui import app as nicegui_app

    saved = dict(nicegui_app.native.start_args)
    try:
        monkeypatch.setattr(main_mod, "DATA_DIR", tmp_path)
        main_mod._configure_native_window()
        assert nicegui_app.native.start_args["storage_path"] == str(tmp_path / "webview2")
        assert (tmp_path / "webview2").is_dir()
    finally:
        nicegui_app.native.start_args.clear()
        nicegui_app.native.start_args.update(saved)


def test_configure_native_window_never_raises(monkeypatch):
    """配置失败(如目录不可写)只记日志,不能阻断启动。"""
    class _Boom:
        def mkdir(self, *args, **kwargs):
            raise OSError("disk full")

    class _FakeDir:
        def __truediv__(self, name):
            return _Boom()

    monkeypatch.setattr(main_mod, "DATA_DIR", _FakeDir())
    main_mod._configure_native_window()   # 不抛异常即通过


# ---------- 托盘"打开主窗口" ----------

def test_tray_open_prefers_native_window():
    """原生窗口模式下托盘应唤回窗口,而不是再开一个浏览器标签页。"""
    opened = []
    tray = SystemTray(url="http://127.0.0.1:8765", icon_path="", on_open=lambda: opened.append(1) or True)
    called = []
    # 若逻辑正确,不应该走到 webbrowser
    import app.tray as tray_mod
    original = tray_mod.webbrowser.open
    tray_mod.webbrowser.open = lambda url: called.append(url)
    try:
        tray._open_main()
    finally:
        tray_mod.webbrowser.open = original
    assert opened == [1]
    assert called == []


def test_tray_open_falls_back_to_browser():
    """没有 on_open(浏览器模式)或回调失败时,仍用浏览器打开。"""
    import app.tray as tray_mod
    called = []
    original = tray_mod.webbrowser.open
    tray_mod.webbrowser.open = lambda url: called.append(url)
    try:
        SystemTray(url="http://127.0.0.1:8765", icon_path="")._open_main()
    finally:
        tray_mod.webbrowser.open = original
    assert called == ["http://127.0.0.1:8765"]


def test_tray_open_falls_back_when_on_open_returns_false():
    import app.tray as tray_mod
    called = []
    original = tray_mod.webbrowser.open
    tray_mod.webbrowser.open = lambda url: called.append(url)
    try:
        SystemTray(url="http://127.0.0.1:8765", icon_path="", on_open=lambda: False)._open_main()
    finally:
        tray_mod.webbrowser.open = original
    assert called == ["http://127.0.0.1:8765"]
