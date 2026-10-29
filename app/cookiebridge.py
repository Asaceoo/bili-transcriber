"""Chrome / Edge 的 App-Bound 加密 Cookie(v20)读取桥。

Chrome 127 起启用了 App-Bound Encryption:Cookie 密文从 v10 变成 v20,
密钥由 Google 的 elevation service 包裹,只有浏览器本体(签名路径校验)能解。
yt-dlp 只实现了 v10,遇到 v20 会回落到 DPAPI 解密并直接抛
Failed to decrypt with DPAPI (issues/10927) —— 表现就是设置页「测试连接」
里全平台的 Cookie 一起「读取失败」。

本模块的做法:把 profile 里解密必需的 Local State + Cookie 库复制到临时目录,
用 --headless 起一个独立浏览器实例(绝不碰用户正在用的那个),经 CDP
Network.getAllCookies 让浏览器自己解密,拿回明文 Cookie 后立刻删掉临时目录。
这样 v10 / v20 都不再依赖第三方解密实现。

只用标准库:WebSocket 手写(浏览器帧必须带 mask,服务端帧不带),
不引入新的运行时依赖,打包体积不变。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

try:  # Windows 才有的「不弹控制台」标志
    _CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
except AttributeError:  # pragma: no cover - 非 Windows
    _CREATE_NO_WINDOW = 0

# 浏览器 key → 可执行名(注册表 App Paths 优先,下面是兜底路径)
_EXE_NAMES: dict[str, tuple[str, ...]] = {
    "chrome": ("chrome.exe",),
    "edge": ("msedge.exe",),
    "brave": ("brave.exe",),
    "vivaldi": ("vivaldi.exe",),
    "chromium": ("chromium.exe",),
    "whale": ("whale.exe",),
    "opera": ("opera.exe",),
}

_EXE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "chrome": (
        r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
        r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ),
    "edge": (
        r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
        r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
        r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    ),
    "brave": (r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe",),
    "vivaldi": (r"%PROGRAMFILES%\Vivaldi\Application\vivaldi.exe",),
    "chromium": (r"%PROGRAMFILES%\Chromium\Application\chromium.exe",),
    "whale": (r"%PROGRAMFILES%\Naver\Naver Whale\Application\whale.exe",),
}

# Cookie 库相对 profile 的两种布局(Chromium 新版本 / 老版本与部分分支)
_COOKIE_DB_RELPATHS: tuple[tuple[str, ...], ...] = (("Network", "Cookies"), ("Cookies",))

# v20 = Chrome 127+ 的 App-Bound Encryption;v10 是老的 DPAPI + AES-GCM 组合
_PREFIX_LABELS = {"763130": "v10", "763230": "v20"}


class CookieBridgeError(RuntimeError):
    """桥接失败(文案已可直接展示给用户)。"""


# ---------------------------------------------------------------- 路径发现


# 浏览器名的别名(yt-dlp 与各家客户端都用自己的写法,统一到这里收口)
_BROWSER_ALIASES: dict[str, str] = {
    "google-chrome": "chrome",
    "chrome-beta": "chrome",
    "chrome-dev": "chrome",
    "googlechrome": "chrome",
    "msedge": "edge",
    "microsoft-edge": "edge",
    "edge-chromium": "edge",
    "brave-browser": "brave",
    "mozilla-firefox": "firefox",
}


def normalize_browser(browser: str) -> str:
    """把用户/客户端写法的浏览器名收口成内部 key。

    AI 与配置文件里常见 'Chrome'、'CHROME'、'edge '、'MS Edge' 这类写法;
    不做规范化时它们全部静默失败,而上层仍显示「Chrome 浏览器 Cookie」——
    用户以为在读 Chrome,实际什么都没读到。这是最难排查的一类错误。
    """
    raw = (browser or "").strip().lower().replace("_", "-")
    if not raw:
        return ""
    if raw in _BROWSER_ALIASES:
        return _BROWSER_ALIASES[raw]
    compact = raw.replace(" ", "")
    if compact in _BROWSER_ALIASES:
        return _BROWSER_ALIASES[compact]
    # "ms edge" / "microsoft edge" 这类两词写法
    if "edge" in compact:
        return "edge"
    if "firefox" in compact:
        return "firefox"
    if "chrome" in compact:
        return "chrome"
    return compact


def _user_data_root(browser: str) -> Path | None:
    """浏览器的 User Data 根目录(Chromium 系布局)。"""
    browser = normalize_browser(browser)
    home = Path.home()
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        roam = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        table = {
            "chrome": local / "Google" / "Chrome" / "User Data",
            "edge": local / "Microsoft" / "Edge" / "User Data",
            "brave": local / "BraveSoftware" / "Brave-Browser" / "User Data",
            "vivaldi": local / "Vivaldi" / "User Data",
            "chromium": local / "Chromium" / "User Data",
            "whale": local / "Naver" / "Naver Whale" / "User Data",
            "opera": roam / "Opera Software" / "Opera Stable",
        }
    elif sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        table = {
            "chrome": support / "Google" / "Chrome",
            "edge": support / "Microsoft Edge",
            "brave": support / "BraveSoftware" / "Brave-Browser",
            "vivaldi": support / "Vivaldi",
            "chromium": support / "Chromium",
        }
    else:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        table = {
            "chrome": cfg / "google-chrome",
            "chromium": cfg / "chromium",
            "edge": cfg / "microsoft-edge",
            "brave": cfg / "BraveSoftware" / "Brave-Browser",
            "vivaldi": cfg / "vivaldi",
        }
    root = table.get(browser)
    return root if root and root.is_dir() else None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def db_rel_path(profile: Path, cookie_db: Path) -> Path:
    """Cookie 库相对 profile 的位置(Network/Cookies 或 Cookies)。"""
    try:
        return cookie_db.relative_to(profile)
    except ValueError:
        return Path(cookie_db.name)


def cookie_db_in(profile: Path) -> Path | None:
    for rel in _COOKIE_DB_RELPATHS:
        candidate = profile.joinpath(*rel)
        if candidate.is_file():
            return candidate
    return None


def newest_profile(browser: str) -> tuple[Path, Path] | None:
    """返回 (profile 目录, Cookie 库路径);取最近写入的那个 profile。"""
    root = _user_data_root(browser)
    if root is None:
        return None
    direct = cookie_db_in(root)  # Opera:根目录自身就是 profile
    candidates = [root] if direct else []
    try:
        for entry in root.iterdir():
            if entry.is_dir() and cookie_db_in(entry) is not None:
                candidates.append(entry)
    except OSError:
        return None
    if not candidates:
        return None
    best = max(candidates, key=lambda p: _mtime(cookie_db_in(p)))
    db = cookie_db_in(best)
    return (best, db) if db else None


def find_executable(browser: str) -> Path | None:
    """找浏览器可执行文件:注册表 App Paths → 常见安装路径 → PATH。"""
    names = _EXE_NAMES.get(normalize_browser(browser))
    if not names:
        return None
    if os.name == "nt":
        try:
            import winreg

            for name in names:
                for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    try:
                        with winreg.OpenKey(
                            hive,
                            "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + name,
                        ) as key:
                            raw = winreg.QueryValue(key, None)
                        if raw:
                            path = Path(raw.strip().strip('"'))
                            if path.is_file():
                                return path
                    except OSError:
                        continue
        except Exception:  # noqa: BLE001 — 注册表读不到就走兜底路径
            pass
        for pattern in _EXE_FALLBACKS.get(normalize_browser(browser), ()):
            path = Path(os.path.expandvars(pattern).strip())
            if path.is_file():
                return path
    found = shutil.which(names[0])
    return Path(found) if found else None

# ---------------------------------------------------------------- 密文类型嗅探


def encryption_prefixes(cookie_db: Path) -> dict[str, int]:
    """统计 Cookie 密文前缀(v10 / v20 …),用于诊断与文案。

    只读密文前 3 字节,不解密;库被占用时返回空字典。
    """
    import sqlite3

    tmpdir: Path | None = None
    try:
        tmpdir = Path(tempfile.mkdtemp(prefix="bili-prefix-"))
        target = tmpdir / "p.db"
        try:
            shutil.copy2(cookie_db, target)
        except (PermissionError, OSError):
            return {}
        conn = sqlite3.connect(str(target))
        try:
            rows = conn.execute(
                "SELECT substr(hex(encrypted_value),1,6) p, count(*) "
                "FROM cookies GROUP BY p ORDER BY 2 DESC LIMIT 8"
            ).fetchall()
        finally:
            conn.close()
        return {(str(r[0] or "")).lower(): int(r[1]) for r in rows if r and r[0]}
    except Exception:  # noqa: BLE001 — 嗅探失败不影响主流程
        return {}
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


def describe_prefixes(prefixes: dict[str, int]) -> str:
    """把前缀统计翻译成人话(v20 = 应用绑定加密,需要浏览器本体解密)。"""
    if not prefixes:
        return ""
    parts = [f"{_PREFIX_LABELS.get(raw, raw)} {count} 条"
             for raw, count in prefixes.items()]
    text = "、".join(parts)
    if any(_PREFIX_LABELS.get(raw) == "v20" for raw in prefixes):
        text += "(应用绑定加密,需要浏览器本体解密)"
    return text


# ---------------------------------------------------------------- 最小 profile 复制


def profile_root(profile: Path) -> tuple[Path, Path]:
    """拆出 (User Data 根, profile 相对根的路径)。

    Chromium 系的布局是 <User Data>/Local State + <User Data>/<Default>/Network/Cookies:
    Local State 在根上,Cookie 库在子 profile 里。Opera 例外(根目录自身就是 profile)。
    复制时必须还原这一层,否则浏览器找不到 Cookie 库、读出 0 条。
    """
    if (profile / "Local State").is_file():
        return profile, Path(".")
    if (profile.parent / "Local State").is_file():
        return profile.parent, Path(profile.name)
    return profile, Path(".")


def find_local_state(profile: Path) -> Path | None:
    """Local State(存放 Cookie 加密密钥)的实际路径。"""
    root, _rel = profile_root(profile)
    candidate = root / "Local State"
    return candidate if candidate.is_file() else None


def layout_target(profile: Path, cookie_db: Path, dest: Path) -> Path:
    """最小 profile 里 Cookie 库该落到哪(还原 <root>/<profile>/Network/Cookies)。"""
    _root, rel = profile_root(profile)
    inner = dest / rel / db_rel_path(profile, cookie_db)
    return inner


def copy_min_profile(profile: Path, cookie_db: Path, dest: Path,
                     wait: float = 6.0) -> None:
    """只复制解密必需的 Local State + Cookie 库(含 WAL/日志)。

    浏览器运行时库被独占打开(Windows 上连共享读都不给),复制直接
    PermissionError —— 这里不硬绕,转成「关掉浏览器」的可行动提示。
    """
    state = find_local_state(profile)
    if state is None:
        raise CookieBridgeError("没找到 Local State,无法读取 Cookie 密钥")
    inner = layout_target(profile, cookie_db, dest)
    inner.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(state, dest / "Local State")
    except (PermissionError, OSError) as exc:
        raise CookieBridgeError(f"读取浏览器密钥失败:{exc}") from exc
    deadline = time.monotonic() + wait
    while True:
        try:
            shutil.copy2(cookie_db, inner)
            break
        except PermissionError:
            if time.monotonic() >= deadline:
                raise CookieBridgeError(
                    "浏览器正在运行,Cookie 库被独占锁定:关闭浏览器后重试,"
                    "或点「从浏览器导出 cookies.txt」再填路径",
                ) from None
            time.sleep(0.4)
        except OSError as exc:
            raise CookieBridgeError(f"读取 Cookie 库失败:{exc}") from exc
    for suffix in ("-wal", "-shm", "-journal"):
        side = cookie_db.with_name(cookie_db.name + suffix)
        if side.exists():
            try:
                shutil.copy2(side, inner.with_name(inner.name + suffix))
            except OSError:
                pass


# ---------------------------------------------------------------- CDP(标准库)


class _CdpConnection:
    """够用的 CDP 客户端:握手 + 单帧收发。

    浏览器发出的帧必须带 mask(否则服务端直接断开),服务端帧不带 mask ——
    这条规则踩错的现象就是「握手 101 之后立刻被关闭」。
    """

    def __init__(self, url: str, timeout: float = 25.0):
        rest = url.split("://", 1)[1]
        host_port, _, path = rest.partition("/")
        host, _, port = host_port.rpartition(":")
        self._sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET /" + path + " HTTP/1.1\r\nHost: " + host_port + "\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: " + key + "\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CookieBridgeError("浏览器调试端口握手失败")
            head += chunk
        status = head.split(b"\r\n")[0]
        if b"101" not in status:
            raise CookieBridgeError(f"浏览器拒绝调试连接:{status[:80]!r}")
        self._next_id = 0

    def _send(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        header = b"\x81"
        mask = os.urandom(4)
        if len(data) < 126:
            header += struct.pack("!B", len(data) | 0x80)
        elif len(data) < 65536:
            header += struct.pack("!BH", 126 | 0x80, len(data))
        else:
            header += struct.pack("!BI", 127 | 0x80, len(data))
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        self._sock.sendall(header + mask + masked)

    def _recv_frame(self) -> dict | None:
        head = self._sock.recv(2)
        if len(head) < 2:
            return None
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._sock.recv(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._sock.recv(8))[0]
        body = b""
        while len(body) < length:
            chunk = self._sock.recv(length - len(body))
            if not chunk:
                return None
            body += chunk
        if opcode == 0x8:  # close
            return None
        if opcode not in (0x1, 0x2):
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}

    def call(self, method: str, params: dict | None = None,
             session: str | None = None, timeout: float = 25.0) -> dict:
        self._next_id += 1
        request = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            request["sessionId"] = session
        self._sock.settimeout(timeout)
        self._send(request)
        while True:
            message = self._recv_frame()
            if message is None:
                raise CookieBridgeError(f"浏览器调试连接中断({method})")
            if message.get("id") == self._next_id:
                return message

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _wait_devtools_port(profile: Path, timeout: float) -> int | None:
    marker = profile / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.is_file():
            try:
                lines = marker.read_text(encoding="utf-8", errors="replace").split()
            except OSError:
                lines = []
            if len(lines) >= 2 and lines[0].strip().isdigit():
                return int(lines[0])
        time.sleep(0.1)
    return None


def _launch(exe: Path, profile: Path, headless_flag: str,
            profile_dir: str = "") -> subprocess.Popen:
    """起一个临时浏览器实例。

    profile_dir 只在「原路径」策略里用:多 profile 用户(User Data/Default、
    Profile 1、Profile 2…)必须指名读哪个,否则 Chrome 一律回到 Default,
    读到的是别人的登录态(静默错误,比报错更糟)。
    """
    args = [
        str(exe), headless_flag, "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--disable-extensions",
        f"--user-data-dir={profile}", "--remote-debugging-port=0", "about:blank",
    ]
    if profile_dir:
        args.insert(-2, f"--profile-directory={profile_dir}")
    return subprocess.Popen(  # noqa: S603 - 参数全部由本模块给出,无外部输入
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=8)
        return
    except Exception:  # noqa: BLE001 — 进程已退出/句柄失效
        pass
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _read_all_cookies(port: int, timeout: float) -> list[dict]:
    """经 CDP 拿全部 Cookie(浏览器自己解密,我们只读明文)。"""
    url = f"http://127.0.0.1:{port}/json/version"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=min(timeout, 10)) as response:
        meta = json.loads(response.read().decode("utf-8", "replace"))
    ws_url = meta.get("webSocketDebuggerUrl")
    if not ws_url:
        raise CookieBridgeError("调试端口没有返回 websocket 地址")
    conn = _CdpConnection(ws_url, timeout=timeout)
    try:
        targets: list[dict] = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            reply = conn.call("Target.getTargets", timeout=timeout)
            targets = (reply.get("result") or {}).get("targetInfos") or []
            if any(t.get("type") == "page" for t in targets):
                break
            time.sleep(0.3)
        page = next((t for t in targets if t.get("type") == "page"), None)
        if page is None:
            raise CookieBridgeError("浏览器没有可用的页面目标")
        attached = conn.call(
            "Target.attachToTarget",
            {"targetId": page.get("targetId"), "flatten": True}, timeout=timeout)
        session = (attached.get("result") or {}).get("sessionId")
        if not session:
            raise CookieBridgeError("无法附加到浏览器页面")
        reply = conn.call("Network.getAllCookies", {}, session=session, timeout=timeout)
        error = reply.get("error")
        if error:
            reason = error.get("message") if isinstance(error, dict) else error
            raise CookieBridgeError(f"读取 Cookie 失败:{reason}")
        return ((reply.get("result") or {}).get("cookies")) or []
    finally:
        conn.close()


def _locked(path: Path, wait: float = 0.0) -> bool:
    """Cookie 库当前是否被别的进程独占(Windows 上 chrome 运行时连共享读都不给)。

    直接尝试复制一份到临时文件:能复制 = 没被独占。比查 SingletonLock 可靠 ——
    那个锁文件在部分分支/异常退出后不会清理,还会把"另一个 profile 在跑"误判成
    "这个 profile 被占用"(多 profile 用户最常见的假失败来源)。
    """
    deadline = time.monotonic() + max(0.0, wait)
    tmpdir = Path(tempfile.mkdtemp(prefix="bili-probe-"))
    try:
        target = tmpdir / "probe.db"
        while True:
            try:
                shutil.copy2(path, target)
                return False
            except PermissionError:
                if time.monotonic() >= deadline:
                    return True
                time.sleep(0.3)
            except OSError:
                return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def browser_running(browser: str) -> bool:
    """该浏览器的 Cookie 库是否被占用(= 对应的浏览器实例在运行)。"""
    found = newest_profile(browser)
    if found is None:
        return False
    _profile, db = found
    return _locked(db)


def _drive(exe: Path, user_data_dir: Path, timeout: float,
           profile_dir: str = "") -> list[dict]:
    """起一个独立实例读 Cookie(调用方负责关进程与清理)。"""
    proc = None
    port = None
    try:
        for flag in ("--headless=new", "--headless"):
            proc = _launch(exe, user_data_dir, flag, profile_dir)
            port = _wait_devtools_port(user_data_dir, timeout=timeout)
            if port:
                break
            _kill(proc)
            proc = None
        if not port:
            raise CookieBridgeError("浏览器没能在限定时间内启动")
        return _read_all_cookies(port, timeout)
    finally:
        if proc is not None:
            _kill(proc)


def read_cookies_in_place(browser: str, timeout: float = 30.0) -> list[dict]:
    """在**原始 profile 路径**上解一次(Chrome 127+ 的 v20 只能这样读)。

    App-Bound 密钥绑定 profile 路径与浏览器签名,复制到别处浏览器就不认,
    因此这条要求浏览器当前没在运行(否则 profile 被独占,起不来第二个实例)。
    """
    browser = require_supported(browser)
    exe = find_executable(browser)
    if exe is None:
        raise CookieBridgeError(f"没找到 {browser} 的可执行文件(可能没安装),无法直读 Cookie")
    found = newest_profile(browser)
    if found is None:
        raise CookieBridgeError(f"没找到 {browser} 的 Cookie 库(可能没安装或从未登录过)")
    profile, _db = found
    root, rel = profile_root(profile)
    if browser_running(browser):
        raise CookieBridgeError(
            "浏览器正在运行:Cookie 用的是应用绑定加密,必须在原路径由浏览器自己解密 ——"
            "先关闭浏览器,再点一次测试/导出,或直接点「从浏览器导出 cookies.txt」"
        )
    # rel 是 "<Profile 名>" 或 "."(Opera 根目录即 profile)
    profile_dir = "" if rel == Path(".") else rel.as_posix()
    return _drive(exe, root, timeout, profile_dir)


def read_cookies_via_copy(browser: str, timeout: float = 25.0,
                          copy_wait: float = 6.0) -> list[dict]:
    """复制最小 profile 到临时目录再解(只对 v10 有效,v20 会是空列表)。

    copy_wait 是复制阶段的重试上限:已经确知库被独占时传小值快速失败,
    不要为了让文案更"努力"而白等。
    """
    browser = require_supported(browser)
    exe = find_executable(browser)
    if exe is None:
        raise CookieBridgeError(f"没找到 {browser} 的可执行文件(可能没安装),无法直读 Cookie")
    found = newest_profile(browser)
    if found is None:
        raise CookieBridgeError(f"没找到 {browser} 的 Cookie 库(可能没安装或从未登录过)")
    profile, cookie_db = found
    tmpdir = Path(tempfile.mkdtemp(prefix="bili-cookie-bridge-"))
    try:
        copy_min_profile(profile, cookie_db, tmpdir, wait=copy_wait)
        return _drive(exe, tmpdir, timeout)
    except CookieBridgeError:
        raise
    except Exception as exc:  # noqa: BLE001 — 统一转成可展示文案
        raise CookieBridgeError(f"直读 Cookie 失败:{exc}") from exc
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


SUPPORTED_BROWSERS: tuple[str, ...] = (
    "chrome", "edge", "brave", "vivaldi", "chromium", "whale", "firefox", "opera",
)


def require_supported(browser: str) -> str:
    """校验并返回规范化后的浏览器 key。

    不区分「不支持这个浏览器」和「没装这个浏览器」时,AI 拿到"没找到 Cookie 库"
    只能乱猜;支持列表一起给出,它才知道下一步该改设置还是装浏览器。
    """
    key = normalize_browser(browser)
    if not key:
        raise CookieBridgeError("没指定浏览器:请先在设置里选一个(cookies_from_browser)")
    if key not in SUPPORTED_BROWSERS:
        raise CookieBridgeError(
            f"不支持 {browser!r} 的 Cookie 读取;支持:" + "、".join(SUPPORTED_BROWSERS)
        )
    return key


def read_cookies(browser: str, timeout: float = 30.0) -> list[dict]:
    """读出全部 Cookie(明文),自动在「原路径」与「临时副本」之间选路。

    - 浏览器没在跑 → 走原路径(能解 v20,最稳,也是唯一能解 v20 的路)
    - 库被独占 → 直接给「关掉浏览器」的提示,不再白等重试。
      Chromium 系运行时连共享读都不给(实测 30s/60 次全失败),重试纯属浪费;
      Firefox 在 Windows 上允许共享读,所以它仍会走临时副本。
    """
    browser = require_supported(browser)
    found = newest_profile(browser)
    if found is None:
        raise CookieBridgeError(f"没找到 {browser} 的 Cookie 库(可能没安装或从未登录过)")
    if not browser_running(browser):
        return read_cookies_in_place(browser, timeout)
    # 库被占用:Firefox 仍可复制,Chromium 会立刻失败(不要再等 6 秒)
    try:
        cookies = read_cookies_via_copy(browser, timeout, copy_wait=0.4)
    except CookieBridgeError:
        cookies = []
    if cookies:
        return cookies
    raise CookieBridgeError(
        "浏览器正在运行:Cookie 用的是应用绑定加密(v20),第三方解不开 ——"
        "关闭浏览器后点一次「测试连接」或「导出」即可,或改用 cookies.txt"
    )


def encrypted_kind(browser: str) -> str:
    """当前 profile 的密文类型(v20 / v10 / 空)。"""
    found = newest_profile(browser)
    if found is None:
        return ""
    prefixes = encryption_prefixes(found[1])
    return describe_prefixes(prefixes)


# ---------------------------------------------------------------- Netscape 输出


def _expires_for_file(cookie: dict) -> int:
    raw = cookie.get("expires")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 2147483647
    if value <= 0:
        return 2147483647  # 会话级:Cookie 文件里用一个远期值占位
    return int(value)


def write_netscape(cookies: list[dict], dest: Path) -> Path:
    """把 CDP Cookie 列表写成 yt-dlp 能直接读的 Netscape 文件。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Netscape HTTP Cookie File",
        "# 由 bili-transcriber 从浏览器导出,内含登录凭据,请勿分享",
    ]
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "")
        if not name or not domain:
            continue
        prefix = "#HttpOnly_" if cookie.get("httpOnly") else ""
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(cookie.get("path") or "/")
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        lines.append(
            f"{prefix}{domain}\t{flag}\t{path}\t{secure}\t"
            f"{_expires_for_file(cookie)}\t{name}\t{value}"
        )
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def has_login_cookie(cookies: list[dict], names: set[str]) -> bool:
    """列表里是否含给定的登录标志 Cookie(用于「导出了但没登录」的提示)。"""
    return any(str(c.get("name") or "") in names for c in cookies)
