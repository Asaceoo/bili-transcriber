"""NiceGUI 桌面入口:进程启动(日志/托盘/WebView2)。

UI 部分自 v0.1.35 起拆分:
- app/ui/state.py   : 路径常量 + 全局单例(store/settings/pipeline/events)
- app/ui/helpers.py : 动作辅助(打开文件夹/删除/重跑/上传落盘等)
- app/ui/pages.py   : 页面构建(任务/历史/运行日记/设置 四页签)
本文件保留进程入口并**再导出 UI 名字**,保持既有测试/外部的导入兼容
(`from app.main import _first_arg, ...` 与 `monkeypatch app.main.*` 仍可用)。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from nicegui import ui

if sys.platform == "win32":
    import winreg

from app import APP_VERSION
from app.logging_setup import log_dir, setup_logging
from app.tray import SystemTray
# --- UI 再导出(兼容旧导入路径) ---
from app.ui.helpers import (  # noqa: F401
    _cancel, _confirm_clear_finished, _confirm_delete, _delete_job_files,
    _first_arg, _fmt_dur, _job_reveal_path, _launch_explorer, _on_upload,
    _open_dir, _open_path, _pause, _rerun, _reset_settings, _resume,
    _search_job_file, _shell_select, _stage_text,
)
from app.ui.pages import main_page  # noqa: F401  @ui.page("/") 装饰器在导入时注册
from app.ui.state import (  # noqa: F401
    ACTIVE_STATUSES, APP_ICON, ASSETS_DIR, BASE_DIR, DATA_DIR, MODEL_OPTIONS,
    STATUS_LABEL, diary_state, events, hw_results, pipeline, settings, store,
)

logger = logging.getLogger(__name__)


def _has_webview2() -> bool:
    """检查 Windows 是否安装了 Microsoft Edge WebView2 Runtime。

    pywebview 在 Windows 上默认使用 WebView2 渲染;若未安装,会静默回退到
    MSHTML(IE),导致 NiceGUI 的 Vue 前端白屏。通过注册表键判断是否存在。
    """
    if sys.platform != "win32":
        return False
    # 每机安装通常在此键;也存在每用户安装路径,一并检查。
    candidates = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
    ]
    for hkey, subkey in candidates:
        try:
            with winreg.OpenKey(hkey, subkey, 0, winreg.KEY_READ) as key:
                pv, _ = winreg.QueryValueEx(key, "pv")
                if pv:
                    return True
        except OSError:
            continue
    return False


_instance_mutex = None  # 进程存活期间保持句柄,进程退出由 OS 释放

WINDOW_TITLE = "B站音频本地转写"


def _env_flag(name: str) -> bool:
    """布尔型环境变量:未设置、"0"、"false"、"no"、"off" 都算关闭。"""
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _find_window(title: str = WINDOW_TITLE) -> int:
    """按标题查原生窗口句柄;没有则返回 0。"""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.restype = wintypes.HWND
        return int(user32.FindWindowW(None, title) or 0)
    except Exception:  # noqa: BLE001 — 查不到不该影响启动
        logger.exception("查找窗口失败")
        return 0


def _activate_window(hwnd: int) -> bool:
    """还原并置顶窗口(SW_RESTORE 对最小化/隐藏都有效)。"""
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:  # noqa: BLE001
        logger.exception("窗口提到前台失败")
        return False


def _activate_existing_window(title: str = WINDOW_TITLE, wait_s: float = 30.0) -> bool:
    """唤醒已在运行的那个窗口;返回是否成功。

    冷启动要先导入 torch/transformers,原生窗口十几秒后才出现。第二次双击时
    如果立刻判定"没有窗口",就会退回浏览器 —— 用户看到的正是"一点就弹浏览器",
    所以这里轮询等待,窗口一出现就把它提到前台。
    """
    if sys.platform != "win32":
        return False
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        hwnd = _find_window(title)
        if hwnd:
            return _activate_window(hwnd)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


def _message_box(text: str, title: str = WINDOW_TITLE) -> None:
    """窗口态 exe 没有控制台,给用户的提示只能走原生消息框。"""
    if sys.platform == "win32":
        try:
            import ctypes

            # MB_ICONINFORMATION | MB_SETFOREGROUND
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x40 | 0x10000)
            return
        except Exception:  # noqa: BLE001
            logger.exception("消息框弹出失败")
    _print_err(f"{title}: {text}")


def _resolve_ui_mode() -> tuple[bool, bool]:
    """决定界面形态,返回 (use_native, show)。

    默认原生窗口:双击 exe 就该看到一个独立程序窗口,而不是被丢进浏览器。
    只有目标机没有 WebView2 运行时,或显式设了 BILI_FORCE_BROWSER=1 才退回浏览器。
    """
    if _env_flag("BILI_FORCE_BROWSER"):
        use_native = False
    elif _env_flag("BILI_FORCE_NATIVE"):
        use_native = True
    else:
        use_native = sys.platform == "win32" and _has_webview2()
    show = not use_native and not _env_flag("BILI_NO_BROWSER")
    return use_native, show


def _configure_native_window() -> None:
    """给 WebView2 指定固定的用户数据目录。

    pywebview 在 private_mode(默认)下把 WebView2 的 UserDataFolder 设成
    `tempfile.TemporaryDirectory().name`:那个临时对象当场就被回收,目录随即
    被删,WebView2 却还在往里写 —— 渲染进程崩溃,窗口消失,NiceGUI 的
    check_shutdown 发现窗口进程已死就调用 stop_and_exit,整个程序静默退出
    (退出码 0、日志无任何报错)。这正是"开着几分钟自己没了"的根因。
    固定到 data/webview2 后不会再有目录被回收,顺带让不同安装目录的实例
    各用各的 profile,不再抢同一个 WebView2 目录。
    """
    try:
        from nicegui import app as nicegui_app

        candidates = [DATA_DIR / "webview2"]
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            # 装到 Program Files 时 data/ 可能不可写,退到每用户目录
            candidates.append(Path(local_appdata) / "Bili Note" / "webview2")
        path = None
        for candidate in candidates:
            try:
                Path(candidate).mkdir(parents=True, exist_ok=True)
                path = candidate
                break
            except OSError:
                logger.warning("原生窗口数据目录不可写,换下一个: %s", candidate)
        if path is None:
            logger.error("找不到可写的原生窗口数据目录,沿用 pywebview 默认值(可能不稳定)")
            return
        # 注意走 start_args 而不是 settings:nicegui 把 settings 传给
        # `webview.settings.update(**...)`,那是个只允许改已有键的 ImmutableDict,
        # 多塞一个键会让子进程直接 KeyError;start_args 才是 `webview.start(**...)` 的入参。
        # 只要 storage_path 有值,pywebview 的 init_storage 就不会用 TemporaryDirectory。
        nicegui_app.native.start_args["storage_path"] = str(path)
    except Exception:  # noqa: BLE001 — 配置失败退回默认值,不阻断启动
        logger.exception("设置原生窗口数据目录失败,继续使用默认值")


def _watch_native_window(wait_s: float = 60.0) -> None:
    """记录窗口就绪耗时;迟迟不出现时留下可诊断的错误日志。"""
    start = time.monotonic()
    while time.monotonic() - start < wait_s:
        if _find_window():
            logger.info("原生窗口已就绪(进程启动后 %.1f 秒)", time.monotonic() - start)
            return
        time.sleep(0.5)
    logger.error(
        "原生窗口 %.0f 秒内没有出现;可设 BILI_FORCE_BROWSER=1 改用浏览器界面", wait_s
    )


def _is_spawn_child() -> bool:
    """当前是否 NiceGUI/multiprocessing 派生出来的子进程。

    子进程会重新执行本模块的 `__mp_main__` 分支(因而再次走到 main()),
    它不是"第二个实例":若也去抢互斥体,必然抢不到,然后按"已有实例"分支
    再开一次浏览器 —— 真机日志里每次启动都多出
    `__mp_main__: 已有实例在运行,本进程退出` 一行,浏览器也被多开一个。
    """
    try:
        import multiprocessing
        return multiprocessing.current_process().name != "MainProcess"
    except Exception:  # noqa: BLE001 — 判断不了就当作主进程
        return False


def _acquire_single_instance() -> bool:
    """同一 data 目录只允许一个桌面实例;返回 False 表示已有实例在跑。

    两个实例会各自拉起 pipeline 续跑同一批未完成任务、各自加载一份模型
    (显存翻倍),并向同一个 SQLite 并发写入 —— 真机已复现:同一条任务被两个
    实例并行处理,先完成的删掉缓存 wav,后一个随即失败。
    无界面模式(CLI/MCP)不经过这里:它们本就该与桌面端共享同一份历史。
    """
    global _instance_mutex
    if sys.platform != "win32" or _is_spawn_child():
        return True
    try:
        import ctypes
        import hashlib
        from ctypes import wintypes

        name = ("Local\\bili-transcriber-"
                + hashlib.sha1(str(DATA_DIR).lower().encode("utf-8")).hexdigest()[:12])
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, wintypes.BOOL(False), name)
        if not handle:
            return True  # 无法创建互斥体时不做限制,不影响正常使用
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _instance_mutex = handle
        return True
    except Exception:  # noqa: BLE001 — 检查失败不应阻断启动
        logger.exception("单实例检查失败,继续启动")
        return True


def _print_err(msg: str) -> None:
    try:
        if sys.stderr is not None:
            print(msg, file=sys.stderr)
    except Exception:
        pass


def _test_imports() -> None:
    """打包运行时导入自检:设 BILI_TEST_IMPORTS=1 时执行,通过后退出(0),失败退出(1)。"""
    try:
        import nagisa  # noqa: F401
        print("[bili test] nagisa import OK")
    except Exception as exc:
        print(f"[bili test] FAIL nagisa: {exc}")
        sys.exit(1)
    try:
        from qwen_asr import Qwen3ASRModel  # noqa: F401
        print("[bili test] qwen_asr import OK")
    except Exception as exc:
        print(f"[bili test] FAIL qwen_asr: {exc}")
        sys.exit(1)
    try:
        from qwen_asr.inference.qwen3_forced_aligner import Qwen3ForceAlignProcessor  # noqa: F401
        print("[bili test] forced_aligner import OK")
    except Exception as exc:
        print(f"[bili test] FAIL forced_aligner: {exc}")
        sys.exit(1)
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
        print("[bili test] pystray import OK")
    except Exception as exc:
        print(f"[bili test] FAIL pystray: {exc}")
        sys.exit(1)
    print("[bili test] ALL_IMPORTS_OK")
    sys.exit(0)


def _tray_exit() -> None:
    """托盘"退出":优雅停止 NiceGUI 服务,让 ui.run() 返回后走正常清理。"""
    try:
        from nicegui import app
        app.shutdown()
    except Exception:
        logger.exception("托盘退出失败,强制退出")
        os._exit(0)


def _dispatch_headless(argv: list[str]) -> int | None:
    """识别无界面参数;返回退出码,None 表示继续走 GUI。

    打包版 exe 与 `bili-transcriber` 控制台入口都会经过这里,保证
    `bili-transcriber --mcp` / `--cli ...` / `--version` 三种写法一致。
    """
    if "--mcp" in argv:
        from app.mcp_server import main as _mcp_main
        return _mcp_main()
    if "--cli" in argv:
        from app.cli import main as _cli_main
        return _cli_main(argv[argv.index("--cli") + 1:])
    if argv[:1] == ["--version"]:
        print(APP_VERSION)
        return 0
    return None


def main() -> None:
    if _is_spawn_child():
        # spawn 出来的子进程只负责开窗(multiprocessing 会把 target 传进去)。
        # 走到这里说明它把入口又跑了一遍 —— 源码模式下 multiprocessing 用
        # runpy 以 __mp_main__ 重执行本模块,就会这样;此时必须立刻返回,
        # 否则会再起一个服务、再抢一次端口。
        return
    code = _dispatch_headless(sys.argv[1:])
    if code is not None:
        raise SystemExit(code)
    # 全局日志:文件日志 + 未捕获异常钩子,先于一切业务逻辑初始化
    setup_logging()
    try:
        port = int(os.environ.get("BILI_PORT", "8765"))
    except ValueError:
        # 环境变量被误设成非数字(如 "abc")时回退默认端口,避免启动即崩溃
        port = 8765
    # 界面形态先定下来:下面的"已有实例"分支要靠它决定是唤醒窗口还是兜底开浏览器
    use_native, show = _resolve_ui_mode()
    logger.info(
        "界面模式:%s(WebView2=%s, show=%s)",
        "原生窗口" if use_native else "浏览器",
        _has_webview2() if sys.platform == "win32" else "n/a",
        show,
    )
    # 单实例:已经有桌面端在跑时不再启动第二份(否则同一任务会被并行处理两次)
    if not _acquire_single_instance():
        logger.info("已有实例在运行,尝试把它的窗口提到前台")
        if _activate_existing_window():
            logger.info("已唤醒现有窗口,本进程退出")
            return
        # 没有窗口可唤醒:原生模式下给个提示,浏览器模式才开浏览器
        if use_native:
            _message_box(
                "B站音频本地转写已经在运行。\n请从任务栏或系统托盘打开它的窗口。"
            )
        else:
            try:
                import webbrowser

                webbrowser.open(f"http://127.0.0.1:{port}")
            except Exception:  # noqa: BLE001 — 打不开浏览器不影响退出
                pass
        return
    # 启动即拉起工作线程:顺带恢复上次会话遗留的"排队中/已暂停/进行中"任务。
    # 之前只有"提交新任务"才会 start(),关闭应用时排队中的任务没人消费,
    # 重启后 UI 一直显示排队中,用户只能手动重跑。
    try:
        pipeline.start()
    except Exception:  # noqa: BLE001 — 启动失败不应阻断界面
        logger.exception("启动任务管道失败")
    if os.environ.get("BILI_TEST_IMPORTS"):
        _test_imports()
    # 系统托盘:常驻后台,可"打开主窗口"或"退出"。浏览器模式关闭窗口后服务仍在,
    # 托盘是重新打开窗口/彻底退出的入口。托盘不可用时静默降级,不影响主程序。
    tray = None
    if not _env_flag("BILI_NO_TRAY"):
        try:
            tray = SystemTray(
                url=f"http://127.0.0.1:{port}",
                icon_path=str(APP_ICON) if APP_ICON else "",
                title=WINDOW_TITLE,
                on_exit=_tray_exit,
                # 原生模式下"打开主窗口"是把那个窗口提到前台,不该再开浏览器
                on_open=(
                    (lambda: _activate_existing_window(WINDOW_TITLE, wait_s=0.0))
                    if use_native
                    else None
                ),
            )
            tray.start()
        except Exception:
            logger.exception("系统托盘启动失败")
    if use_native:
        _configure_native_window()
        threading.Thread(
            target=_watch_native_window, name="window-watch", daemon=True
        ).start()
    run_kwargs: dict[str, Any] = {
        "title": WINDOW_TITLE,
        "port": port,
        "reload": False,
        "favicon": str(APP_ICON) if APP_ICON else "🅑",
        "native": use_native,
        "show": show,
    }
    if use_native:
        # window_size 不传时 nicegui 用 800x600;注意它本身也会把 native 置 True,
        # 所以只在确实要原生窗口时才带这个参数。
        run_kwargs["window_size"] = (980, 720)
    started = time.monotonic()
    try:
        ui.run(**run_kwargs)
    finally:
        logger.info("界面已退出,本次运行 %.1f 秒", time.monotonic() - started)
    # ui.run 返回后进程随即退出(托盘"退出"经 app.shutdown 走到这里);
    # 不做显式清理:daemon 线程随进程消亡,避免测试环境 timer 访问已关闭的 store。


if __name__ in {"__main__", "__mp_main__"}:
    try:
        # PyInstaller 冻结 + multiprocessing spawn:原生窗口子进程会带
        # --multiprocessing-fork 参数重执行本 exe。必须在任何应用逻辑前
        # 处理该参数(标准 freeze_support),否则子进程会重跑完整启动流程,
        # 在 native_mode 深处再次 freeze_support 时因父进程句柄失效抛
        # WinError 87(参数错误),弹出 "Failed to execute script" 错误框。
        import multiprocessing
        multiprocessing.freeze_support()
    except SystemExit:
        raise
    except Exception:
        # spawn 子进程初始化失败(如父进程已退出导致 OpenProcess 失败):
        # 静默退出,不弹错误框。
        sys.exit(0)

    # 无界面模式(`--mcp` / `--cli` / `--version`)在 main() 内统一分发,
    # 打包版 exe 与控制台入口行为一致。
    main()
