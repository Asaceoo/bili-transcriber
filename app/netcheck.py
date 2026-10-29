"""网络与 Cookie 自动检测(设置页「一键检测」/「测试连接」的后端)。

三条检测线,全部只读(不改设置、不写浏览器数据):

1. **Cookie 来源** —— 已装浏览器(Chromium 系 + Firefox)里各平台的登录态 Cookie,
   以及本机散落的 cookies.txt(Netscape 格式)。只看 Cookie 的 域名/名字/有效期,
   不读取也不解密 Cookie 值 —— 判断"是否登录某平台"只需要这些元信息。
2. **网络** —— 直连各平台的可达性与延迟;系统代理、环境变量代理、本机常见代理
   端口(Clash / v2rayN / Privoxy …),并验证哪个代理真能访问 YouTube。
3. **建议** —— 按「覆盖平台数」挑最优来源,产出可直接写回设置的三元组:
   (浏览器, cookies.txt, 代理)。

「测试连接」才会联网并读取 Cookie 值(要么解析 cookies.txt,要么经 yt-dlp 解密
浏览器 Cookie),并用 B 站 nav 接口做一次真实登录校验 —— 这一步才可能碰到
"浏览器正在运行导致 Cookie 库被独占锁定"的情况,文案会直接告诉用户怎么办。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from app.downloader import PROBE_HOSTS

logger = logging.getLogger(__name__)

# 平台顺序 = UI 展示顺序(国内平台在前,境外平台最后)
PLATFORMS: tuple[str, ...] = ("bilibili", "douyin", "xiaohongshu", "kuaishou", "youtube")
PLATFORM_LABEL = {
    "bilibili": "B 站",
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "kuaishou": "快手",
    "youtube": "YouTube",
}

# 各平台的「已登录」标志 Cookie:存在即认为浏览器/文件里有该平台账号
LOGIN_COOKIES: dict[str, tuple[str, ...]] = {
    "bilibili": ("SESSDATA", "DedeUserID"),
    "douyin": ("sessionid", "sessionid_ss", "sid_guard", "sid_tt"),
    "xiaohongshu": ("web_session", "customerClientId"),
    "kuaishou": ("kuaishou.server.web_st", "kuaishou.server.web_ph", "passToken", "userId"),
    "youtube": ("SID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO"),
}

# Cookie 所属域名后缀(抖音/小红书/快手都有多个投放域名)
_DOMAIN_SUFFIX: dict[str, tuple[str, ...]] = {
    "bilibili": ("bilibili.com", "b23.tv"),
    "douyin": ("douyin.com", "iesdouyin.com"),
    "xiaohongshu": ("xiaohongshu.com", "xhslink.com"),
    "kuaishou": ("kuaishou.com", "gifshow.com"),
    "youtube": ("youtube.com", "google.com", "youtu.be"),
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

# 常见本机代理端口(Clash / Clash Verge / v2rayN / Privoxy / 通用 HTTP 代理)
COMMON_PROXY_PORTS: tuple[tuple[int, str], ...] = (
    (7890, "http"), (7891, "http"), (7897, "http"), (10809, "http"),
    (1087, "http"), (8118, "http"), (2080, "http"), (8889, "http"),
    (9910, "http"), (1080, "socks5"), (10808, "socks5"),
)

# cookies.txt 扫描目录里只认名字带 cookie 的 txt(避免误扫大目录里的无关文本)
_MAX_COOKIE_FILE_BYTES = 8 * 1024 * 1024

_EPOCH_1601 = datetime(1601, 1, 1, tzinfo=timezone.utc)


class NetcheckError(RuntimeError):
    """面向用户的检测/导出失败(文案已可直接展示)。"""


# ---------------------------------------------------------------- 数据结构


@dataclass
class PlatformCookie:
    """某平台在一个 Cookie 来源里的登录态。"""

    platform: str
    name: str = ""          # 命中的登录标志 Cookie 名
    host: str = ""
    expires_at: str = ""    # YYYY-MM-DD;空 = 会话级
    expired: bool = False

    @property
    def present(self) -> bool:
        return bool(self.name)


@dataclass
class BrowserInfo:
    key: str                        # chrome / edge / firefox …
    label: str                      # 展示名
    profile: str = ""               # Default / Profile 1 / xxx.default
    cookie_db: Path | None = None
    running: bool = False           # Cookie 库被占用 = 浏览器正在运行
    error: str = ""
    cookie_count: int = 0
    platforms: dict[str, PlatformCookie] = field(default_factory=dict)

    @property
    def readable(self) -> bool:
        return self.cookie_db is not None and not self.error and not self.running

    @property
    def login_platforms(self) -> list[str]:
        return [p for p in PLATFORMS
                if (c := self.platforms.get(p)) and c.present and not c.expired]

    @property
    def coverage(self) -> int:
        return len(self.login_platforms)

    def describe(self) -> str:
        if self.running:
            return f"{self.label} 正在运行,Cookie 库被占用"
        if self.error:
            return f"{self.label}:{self.error}"
        if not self.coverage:
            return f"{self.label}:未找到平台登录态"
        names = "、".join(PLATFORM_LABEL[p] for p in self.login_platforms)
        return f"{self.label}:{names}"


@dataclass
class CookieFileInfo:
    path: Path
    cookie_count: int = 0
    platforms: dict[str, PlatformCookie] = field(default_factory=dict)
    error: str = ""

    @property
    def login_platforms(self) -> list[str]:
        return [p for p in PLATFORMS
                if (c := self.platforms.get(p)) and c.present and not c.expired]

    @property
    def coverage(self) -> int:
        return len(self.login_platforms)


@dataclass
class PlatformProbe:
    platform: str
    ok: bool = False
    ms: int = 0
    error: str = ""


@dataclass
class ProxyCandidate:
    url: str
    source: str = ""        # 系统代理 / 环境变量 / 本机端口
    listening: bool = False  # 端口在监听
    verified: bool = False   # 经它访问 YouTube 成功
    ms: int = 0
    error: str = ""


@dataclass
class NetworkReport:
    direct: dict[str, PlatformProbe] = field(default_factory=dict)
    proxies: list[ProxyCandidate] = field(default_factory=list)
    working_proxy: str = ""
    system_proxy: str = ""

    @property
    def youtube_direct_ok(self) -> bool:
        probe = self.direct.get("youtube")
        return bool(probe and probe.ok)


@dataclass
class DetectionReport:
    browsers: list[BrowserInfo] = field(default_factory=list)
    cookie_files: list[CookieFileInfo] = field(default_factory=list)
    network: NetworkReport = field(default_factory=NetworkReport)
    suggested_browser: str = ""
    suggested_cookie_file: str = ""
    suggested_proxy: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def platform_states(self) -> dict[str, PlatformCookie]:
        """各平台"最好的那个来源"的登录态(浏览器与 cookies.txt 合并取优)。"""
        best: dict[str, PlatformCookie] = {}
        for info in self.cookie_files:
            for plat, cookie in info.platforms.items():
                if cookie.present and not cookie.expired:
                    best.setdefault(plat, cookie)
        for browser in self.browsers:
            for plat, cookie in browser.platforms.items():
                if cookie.present and not cookie.expired:
                    best.setdefault(plat, cookie)
        return best


@dataclass
class PlatformTest:
    platform: str
    network_ok: bool = False
    ms: int = 0
    cookie_state: str = "missing"  # verified / present / expired / missing / unreadable / unknown
    detail: str = ""


@dataclass
class TestReport:
    items: list[PlatformTest] = field(default_factory=list)
    source: str = ""
    summary: str = ""
    warnings: list[str] = field(default_factory=list)
    ok: bool = False

    def by_platform(self, platform: str) -> PlatformTest | None:
        return next((i for i in self.items if i.platform == platform), None)


# ---------------------------------------------------------------- 时间/工具


def _expiry_from_chrome(value) -> datetime | None:
    """Chrome/Edge 的 expires_utc:1601-01-01 起的微秒数,0 = 会话级。"""
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    try:
        return _EPOCH_1601 + timedelta(microseconds=raw)
    except OverflowError:
        return None


def _expiry_from_unix(value) -> datetime | None:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _fmt_expiry(exp: datetime | None) -> str:
    return exp.strftime("%Y-%m-%d") if exp else ""


def _platform_of(host: str, name: str) -> str | None:
    """按 Cookie 域名 + 名字判断它属于哪个平台的登录态;无关 Cookie 返回 None。"""
    host = (host or "").lstrip(".").lower()
    if not host or not name:
        return None
    for platform, suffixes in _DOMAIN_SUFFIX.items():
        if name not in LOGIN_COOKIES[platform]:
            continue
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return platform
    return None


def _match_cookies(rows: Iterable[tuple[str, str, datetime | None]]) -> dict[str, PlatformCookie]:
    """从 (域名, 名字, 过期时间) 三元组里挑出各平台的登录标志 Cookie。

    同一平台可能有多个候选(如抖音 sessionid / sessionid_ss),优先未过期的那个;
    都过期时保留一个用于提示"Cookie 已过期"。
    """
    hits: dict[str, PlatformCookie] = {}
    for host, name, exp in rows:
        platform = _platform_of(host, name)
        if not platform:
            continue
        expired = bool(exp and exp <= datetime.now(timezone.utc))
        candidate = PlatformCookie(platform, name, host, _fmt_expiry(exp), expired)
        prev = hits.get(platform)
        if prev is None or (prev.expired and not candidate.expired):
            hits[platform] = candidate
    return hits


def _read_sqlite_cookies(db: Path, query: str) -> tuple[list[tuple], str, bool]:
    """把 Cookie 库复制到临时目录再读,返回 (行, 错误文案, 是否被占用)。

    必须复制:浏览器运行时原库被独占锁定(Windows 上直接 PermissionError),
    复制失败就说明"浏览器正在运行",这不算错误,交给上层转成友好提示。
    """
    try:
        tmpdir = Path(tempfile.mkdtemp(prefix="bili-cookie-"))
    except OSError as exc:
        return [], f"临时目录创建失败:{exc.strerror or exc}", False
    try:
        target = tmpdir / db.name
        try:
            shutil.copy2(db, target)
        except PermissionError:
            return [], "", True
        except OSError as exc:
            return [], f"读取失败:{exc.strerror or exc}", False
        # 顺带复制 WAL/回滚日志,避免读到半截事务
        for suffix in ("-wal", "-shm", "-journal"):
            side = db.with_name(db.name + suffix)
            if side.exists():
                try:
                    shutil.copy2(side, target.with_name(target.name + suffix))
                except OSError:
                    pass
        try:
            conn = sqlite3.connect(str(target))
            try:
                rows = conn.execute(query).fetchall()
            finally:
                conn.close()
            return rows, "", False
        except sqlite3.DatabaseError as exc:
            return [], f"数据库格式异常:{exc}", False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _read_chromium(db: Path) -> tuple[list[tuple[str, str, datetime | None]], str, bool]:
    rows, error, locked = _read_sqlite_cookies(db, "SELECT host_key, name, expires_utc FROM cookies")
    if error or locked:
        return [], error, locked
    out = [(str(r[0] or ""), str(r[1] or ""), _expiry_from_chrome(r[2])) for r in rows]
    return out, "", False


def _read_firefox(db: Path) -> tuple[list[tuple[str, str, datetime | None]], str, bool]:
    rows, error, locked = _read_sqlite_cookies(db, "SELECT host, name, expiryDate FROM moz_cookies")
    if error or locked:
        return [], error, locked
    out = [(str(r[0] or ""), str(r[1] or ""), _expiry_from_unix(r[2])) for r in rows]
    return out, "", False


# ---------------------------------------------------------------- 浏览器发现


def _browser_roots() -> list[tuple[str, str, Path, bool]]:
    """(key, 展示名, 配置根目录, 是否 Firefox),只返回真实存在的目录。"""
    home = Path.home()
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        roam = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        roots = [
            ("chrome", "Chrome", local / "Google" / "Chrome" / "User Data", False),
            ("edge", "Edge", local / "Microsoft" / "Edge" / "User Data", False),
            ("brave", "Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data", False),
            ("vivaldi", "Vivaldi", local / "Vivaldi" / "User Data", False),
            ("chromium", "Chromium", local / "Chromium" / "User Data", False),
            ("whale", "Whale", local / "Naver" / "Naver Whale" / "User Data", False),
            ("opera", "Opera", roam / "Opera Software" / "Opera Stable", False),
            ("firefox", "Firefox", roam / "Mozilla" / "Firefox", True),
        ]
    elif sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        roots = [
            ("chrome", "Chrome", support / "Google" / "Chrome", False),
            ("edge", "Edge", support / "Microsoft Edge", False),
            ("brave", "Brave", support / "BraveSoftware" / "Brave-Browser", False),
            ("vivaldi", "Vivaldi", support / "Vivaldi", False),
            ("chromium", "Chromium", support / "Chromium", False),
            ("firefox", "Firefox", support / "Firefox", True),
        ]
    else:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        roots = [
            ("chrome", "Chrome", cfg / "google-chrome", False),
            ("chromium", "Chromium", cfg / "chromium", False),
            ("edge", "Edge", cfg / "microsoft-edge", False),
            ("brave", "Brave", cfg / "BraveSoftware" / "Brave-Browser", False),
            ("vivaldi", "Vivaldi", cfg / "vivaldi", False),
            ("firefox", "Firefox", home / ".mozilla" / "firefox", True),
        ]
    return [r for r in roots if r[2].is_dir()]


def _cookie_db(profile: Path) -> Path | None:
    """Chromium 新版本放在 Network/Cookies,旧版本/部分分支直接放 Cookies。"""
    for rel in (("Network", "Cookies"), ("Cookies",)):
        candidate = profile.joinpath(*rel)
        if candidate.is_file():
            return candidate
    return None


def _chromium_profiles(root: Path) -> list[Path]:
    if _cookie_db(root) is not None:  # Opera:根目录本身就是 profile
        return [root]
    found: list[Path] = []
    try:
        for entry in root.iterdir():
            if entry.is_dir() and _cookie_db(entry) is not None:
                found.append(entry)
    except OSError:
        return []
    return sorted(found, key=lambda p: _mtime(_cookie_db(p)), reverse=True)[:2]


def _firefox_profiles(root: Path) -> list[Path]:
    base = root / "Profiles"
    if not base.is_dir():
        return []
    try:
        found = [d for d in base.iterdir() if (d / "cookies.sqlite").is_file()]
    except OSError:
        return []
    return sorted(found, key=lambda p: _mtime(p / "cookies.sqlite"), reverse=True)[:2]


def _mtime(path: Path | None) -> float:
    if path is None:
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def detect_browsers() -> list[BrowserInfo]:
    """扫描本机已装浏览器的 Cookie 库,识别各平台登录态(只读元信息)。"""
    out: list[BrowserInfo] = []
    for key, label, root, is_firefox in _browser_roots():
        profiles = _firefox_profiles(root) if is_firefox else _chromium_profiles(root)
        for profile in profiles:
            db = (profile / "cookies.sqlite") if is_firefox else _cookie_db(profile)
            if db is None:
                continue
            rows, error, locked = (_read_firefox if is_firefox else _read_chromium)(db)
            out.append(BrowserInfo(
                key=key, label=label, profile=profile.name, cookie_db=db,
                running=locked, error=error, cookie_count=len(rows),
                platforms=_match_cookies(rows),
            ))
    out.sort(key=lambda b: (0 if b.readable else 1 if b.running else 2,
                            -b.coverage, b.label))
    return out


# ---------------------------------------------------------------- cookies.txt


def parse_cookie_file(path: Path) -> tuple[int, dict[str, PlatformCookie],
                                           dict[str, dict[str, str]], str]:
    """解析 Netscape 格式 cookies.txt。

    返回 (Cookie 条数, 各平台登录态, 各平台 Cookie 键值, 错误文案)。
    键值只在「测试连接」里用于 B 站在线校验,浏览/检测阶段不需要。
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return 0, {}, {}, f"读取失败:{exc.strerror or exc}"
    rows: list[tuple[str, str, datetime | None]] = []
    values: dict[str, dict[str, str]] = {}
    total = 0
    for raw in text.splitlines():
        line = raw.replace("\ufeff", "").strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, _path, _secure, expires, name, value = parts[:7]
        total += 1
        exp = _expiry_from_unix(expires)
        rows.append((domain, name, exp))
        platform = _platform_of(domain, name)
        if platform:
            values.setdefault(platform, {})[name] = value
    return total, _match_cookies(rows), values, ""


def _inspect_cookie_file(path: Path) -> CookieFileInfo | None:
    try:
        if path.stat().st_size > _MAX_COOKIE_FILE_BYTES:
            return None
    except OSError:
        return None
    total, platforms, _values, error = parse_cookie_file(path)
    if error:
        return CookieFileInfo(path, 0, {}, error)
    if not total:
        return None  # 不是 Netscape cookie 文件,静默跳过
    return CookieFileInfo(path, total, platforms)


def detect_cookie_files(dirs: Iterable[Path | str],
                        extra_files: Iterable[Path | str] = ()) -> list[CookieFileInfo]:
    """在指定目录里找 cookies.txt;extra_files 里的路径(如当前设置)也会被检查。"""
    candidates: list[Path] = []
    seen: set[str] = set()
    for raw in dirs:
        directory = Path(raw)
        if not directory.is_dir():
            continue
        try:
            for entry in directory.glob("*.txt"):
                if "cookie" in entry.name.lower():
                    candidates.append(entry)
        except OSError:
            continue
    for raw in extra_files:
        entry = Path(raw)
        if entry.is_file():
            candidates.append(entry)

    out: list[CookieFileInfo] = []
    for entry in candidates:
        try:
            key = str(entry.resolve()).lower()
        except OSError:
            key = str(entry).lower()
        if key in seen:
            continue
        seen.add(key)
        info = _inspect_cookie_file(entry)
        if info is not None:
            out.append(info)
    out.sort(key=lambda c: (-c.coverage, c.path.name.lower()))
    return out


def default_cookie_dirs(base_dir: Path, data_dir: Path) -> list[Path]:
    """cookies.txt 的常规落点:项目目录、data/、下载、桌面、文档。"""
    home = Path.home()
    return [base_dir, data_dir, home / "Downloads", home / "Desktop", home / "Documents"]


# ---------------------------------------------------------------- 代理与网络


def _normalize_proxy(raw: str) -> str:
    """把注册表/环境变量里的代理写法统一成 URL。

    Windows 的 ProxyServer 可能是 `127.0.0.1:7890` 或
    `http=127.0.0.1:7890;https=127.0.0.1:7890`,取 https(或 http)那一项补上协议。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if "=" in text:
        table = {}
        for part in text.split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                table[key.strip().lower()] = value.strip()
        text = table.get("https") or table.get("http") or next(iter(table.values()), "")
        if not text:
            return ""
    text = text.split(" ")[0].strip()
    if "://" not in text:
        text = "http://" + text
    return text


def _system_proxy() -> str:
    if os.name != "nt":
        for var in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            value = os.environ.get(var)
            if value:
                return _normalize_proxy(value)
        return ""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
    except Exception:  # noqa: BLE001 — 注册表键缺失/权限异常都视为"没有系统代理"
        return ""
    return _normalize_proxy(server) if enabled else ""


def _env_proxies() -> list[str]:
    found: list[str] = []
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(var)
        if value:
            url = _normalize_proxy(value)
            if url and url not in found:
                found.append(url)
    return found


def _tcp_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _proxy_port_open(url: str) -> bool:
    parts = urllib.parse.urlparse(url)
    if not parts.hostname or not parts.port:
        return False
    return _tcp_open(parts.hostname, parts.port)


def detect_proxies() -> list[ProxyCandidate]:
    """收集系统代理 / 环境变量代理 / 本机常见代理端口,并标注端口是否在监听。"""
    found: list[ProxyCandidate] = []
    seen: set[str] = set()

    system = _system_proxy()
    if system:
        found.append(ProxyCandidate(system, "系统代理"))
        seen.add(system)
    for url in _env_proxies():
        if url not in seen:
            found.append(ProxyCandidate(url, "环境变量"))
            seen.add(url)

    def probe_port(item: tuple[int, str]) -> ProxyCandidate | None:
        port, scheme = item
        if not _tcp_open("127.0.0.1", port):
            return None
        url = f"{scheme}://127.0.0.1:{port}"
        if url in seen:
            return None
        return ProxyCandidate(url, "本机端口", listening=True)

    with ThreadPoolExecutor(max_workers=len(COMMON_PROXY_PORTS)) as pool:
        for candidate in pool.map(probe_port, COMMON_PROXY_PORTS):
            if candidate and candidate.url not in seen:
                seen.add(candidate.url)
                found.append(candidate)
    return found


def _socks5_ready(proxy_url: str, target_host: str, target_port: int, timeout: float) -> bool:
    """SOCKS5 握手 + CONNECT:能建到目标即视为链路可用(不做 TLS,省时且足够)。"""
    parts = urllib.parse.urlparse(proxy_url)
    host, port = parts.hostname, parts.port or 1080
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"\x05\x01\x00")
            if sock.recv(2) != b"\x05\x00":
                return False
            host_bytes = target_host.encode("idna")
            request = (b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes
                       + target_port.to_bytes(2, "big"))
            sock.sendall(request)
            reply = sock.recv(4)
            return len(reply) >= 2 and reply[1] == 0
    except OSError:
        return False


def _dns_ok(host: str, timeout: float) -> bool:
    """带超时的域名解析。

    urllib 的 timeout 只覆盖 socket 操作,**不覆盖 DNS 解析**:被污染的域名
    解析会卡十几秒,表现为"点了检测半天没反应"。这里先给 DNS 一个硬上限,
    解析不出来就直接判不可达(被墙域名解析本来就该判不可达)。
    """
    if not host or _is_ip(host):
        return True
    result: dict[str, bool] = {}

    def run() -> None:
        try:
            socket.getaddrinfo(host, 443)
            result["ok"] = True
        except OSError:
            result["ok"] = False

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    return result.get("ok", False)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _probe(url: str, proxy: str = "", timeout: float = 4.0) -> tuple[bool, int, str]:
    """探测 URL 是否可达,返回 (可达, 耗时毫秒, 错误文案)。

    HTTP 4xx/5xx 也算可达(服务器有响应 = 网络通);只有连接层失败才判不可达。
    必须显式传空 ProxyHandler:默认 opener 会读取系统/环境变量代理,
    那样"直连探测"其实是走代理,结果会撒谎。
    """
    started = time.perf_counter()
    target = urllib.parse.urlparse(url)
    host = target.hostname or ""
    scheme = (proxy or "").lower()
    if scheme.startswith("socks"):
        # socks5h 由代理解析域名,本地不必也不能先解析
        ok = _socks5_ready(proxy, host, target.port or 443, timeout)
        elapsed = int((time.perf_counter() - started) * 1000)
        return ok, elapsed, "" if ok else "经 socks 代理连接失败"
    # 走代理时由代理解析域名:本地 DNS 被污染/超时并不代表代理不可用,
    # 所以这步只在直连探测时做,避免把能用的代理误判成不可用。
    if not proxy and not _dns_ok(host, min(2.0, timeout)):
        elapsed = int((time.perf_counter() - started) * 1000)
        return False, elapsed, "域名解析超时"
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with opener.open(request, timeout=timeout):
            return True, int((time.perf_counter() - started) * 1000), ""
    except urllib.error.HTTPError:
        return True, int((time.perf_counter() - started) * 1000), ""
    except Exception as exc:  # noqa: BLE001 — 连接层任何异常都视为不可达
        return False, int((time.perf_counter() - started) * 1000), str(exc)[:120]


def _result_or_timeout(future, limit: float) -> tuple[bool, int, str]:
    """取探测结果,超过硬上限就判超时(不等底层 socket 逐个地址重试)。

    urllib/socket 的 timeout 是"每个地址每次 socket 操作"的上限:被墙域名解析出
    多个 IP 时会连续等好几个 timeout(实测 YouTube 3.5s 的配置要 7.2s)。
    这里用 future 的硬截止时间把"探测耗时"封住,超时的线程留给后台自己结束。
    """
    try:
        return future.result(timeout=limit)
    except FutureTimeoutError:
        return False, int(limit * 1000), "探测超时(网络被阻断或代理不可用)"


def probe_network(timeout: float = 4.0) -> NetworkReport:
    """并行探测各平台直连情况,并验证候选代理能否访问 YouTube。

    直连探测先提交,代理扫描/验证随后并发跑:两块互不依赖,串行做会让
    "一键检测"从 4 秒变成 9 秒(用户能明显感到卡)。
    """
    report = NetworkReport(system_proxy=_system_proxy())
    youtube_timeout = min(timeout, 3.5)
    pool = ThreadPoolExecutor(max_workers=len(PLATFORMS) + 8)
    try:
        direct_futures = {
            p: pool.submit(_probe, PROBE_HOSTS[p], "",
                           youtube_timeout if p == "youtube" else timeout)
            for p in PLATFORMS
        }
        report.proxies = detect_proxies()
        verifiable = [c for c in report.proxies if c.listening or c.source != "本机端口"]
        verify_futures = {
            id(c): pool.submit(_probe, PROBE_HOSTS["youtube"], c.url, timeout)
            for c in verifiable
        }
        for platform, future in direct_futures.items():
            limit = youtube_timeout if platform == "youtube" else timeout
            ok, ms, error = _result_or_timeout(future, limit + 0.3)
            report.direct[platform] = PlatformProbe(platform, ok, ms, error)
        for candidate in verifiable:
            candidate.verified, candidate.ms, candidate.error = \
                _result_or_timeout(verify_futures[id(candidate)], timeout + 0.3)
    finally:
        pool.shutdown(wait=False)
    report.working_proxy = next((c.url for c in report.proxies if c.verified), "")
    return report


def find_working_proxy(timeout: float = 4.0) -> ProxyCandidate | None:
    """只探测代理(设置页「探测代理」按钮用),返回第一个能访问 YouTube 的。"""
    proxies = detect_proxies()
    verifiable = [c for c in proxies if c.listening or c.source != "本机端口"]
    if not verifiable:
        return None
    pool = ThreadPoolExecutor(max_workers=min(6, len(verifiable)))
    try:
        futures = {id(c): pool.submit(_probe, PROBE_HOSTS["youtube"], c.url, timeout)
                   for c in verifiable}
        for candidate in verifiable:
            candidate.verified, candidate.ms, candidate.error = \
                _result_or_timeout(futures[id(candidate)], timeout + 0.3)
    finally:
        pool.shutdown(wait=False)
    return next((c for c in verifiable if c.verified), None)


# ---------------------------------------------------------------- 建议


def build_report(browsers: list[BrowserInfo], cookie_files: list[CookieFileInfo],
                 network: NetworkReport, *, current_browser: str = "",
                 current_cookie_file: str = "", current_proxy: str = "") -> DetectionReport:
    """把三条检测线的原始结果收敛成"填哪三个值 + 为什么"的建议。

    选源原则:先比覆盖平台数;数量相同优先 cookies.txt(不受浏览器开关/锁定影响),
    其次是可读的浏览器。什么都没识别到就保留用户当前配置,绝不清空。
    """
    report = DetectionReport(browsers=browsers, cookie_files=cookie_files, network=network)
    notes = report.notes

    readable = [b for b in browsers if b.readable and b.coverage]
    locked = [b for b in browsers if b.running]
    best_browser = max(readable, key=lambda b: b.coverage) if readable else None
    best_file = max(cookie_files, key=lambda f: f.coverage) if cookie_files else None
    browser_cov = best_browser.coverage if best_browser else 0
    file_cov = best_file.coverage if best_file else 0

    if best_file and file_cov > 0 and file_cov >= browser_cov:
        report.suggested_cookie_file = str(best_file.path)
        report.suggested_browser = best_browser.key if best_browser else current_browser
        notes.append(
            f"cookies.txt:{best_file.path}(覆盖 {file_cov} 个平台,优先于浏览器 Cookie)")
    elif best_browser:
        report.suggested_browser = best_browser.key
        report.suggested_cookie_file = ""
        notes.append(f"{best_browser.label} 读到 {browser_cov} 个平台的登录态")
    else:
        report.suggested_browser = current_browser
        report.suggested_cookie_file = current_cookie_file
        if locked:
            names = "、".join(b.label for b in locked)
            notes.append(f"{names} 正在运行,Cookie 库被独占锁定:关闭浏览器后重新检测更准,"
                         "或改用 cookies.txt")
        elif browsers:
            notes.append("浏览器里没找到平台登录态:先在浏览器登录对应平台,或导出 cookies.txt")
        else:
            notes.append("未发现已安装的浏览器:可先用浏览器登录并导出 cookies.txt")

    if network.working_proxy:
        report.suggested_proxy = network.working_proxy
        if network.youtube_direct_ok:
            notes.append(f"已发现可用代理 {network.working_proxy}(YouTube 直连也可用)")
        else:
            notes.append(f"已找到可用代理 {network.working_proxy}:YouTube 走它才能下载")
    elif network.youtube_direct_ok:
        report.suggested_proxy = ""
        notes.append("YouTube 可直连,无需代理")
    elif network.system_proxy:
        report.suggested_proxy = network.system_proxy
        notes.append(f"系统代理 {network.system_proxy} 未通过 YouTube 探测,已按系统设置填入")
    else:
        report.suggested_proxy = current_proxy
        notes.append("YouTube 直连不通,也未找到本机代理:境外视频需要手动填写代理")
    return report


# ---------------------------------------------------------------- 测试连接


def _friendly_browser_error(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    if ("could not copy" in low or "cookie database" in low or "being used" in low
            or "permission" in low):
        return "浏览器正在运行,Cookie 数据库被占用(关闭浏览器后重试)"
    if "could not find" in low or "no such" in low or "not found" in low:
        return "未找到该浏览器的 Cookie 数据库"
    if "decrypt" in low:
        return "Cookie 解密失败(浏览器版本较新或启用了加密保护)"
    first = text.splitlines()[0].strip() if text.strip() else ""
    return first[:120] or exc.__class__.__name__


class _QuietYdlLogger:
    """yt-dlp 的 Cookie 提取日志转进本模块 logger,不污染 stdout(MCP 协议端口)。"""

    def debug(self, msg):  # noqa: D102 - 协议要求的方法名
        logger.debug("yt-dlp: %s", msg)

    def info(self, msg):  # noqa: D102
        logger.debug("yt-dlp: %s", msg)

    def warning(self, msg):  # noqa: D102
        logger.debug("yt-dlp 警告: %s", msg)

    def error(self, msg):  # noqa: D102
        logger.debug("yt-dlp 错误: %s", msg)


def _browser_cookie_values(browser: str) -> tuple[dict[str, dict[str, str]],
                                                  dict[str, PlatformCookie], str]:
    """经 yt-dlp 解密浏览器 Cookie(这一步才需要读 Cookie 值)。"""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except Exception as exc:  # noqa: BLE001 — 缺依赖也要给可读文案
        return {}, {}, f"当前版本缺少 yt-dlp:{exc}"
    try:
        jar = extract_cookies_from_browser(browser, logger=_QuietYdlLogger())
    except Exception as exc:  # noqa: BLE001 — 锁定/解密失败都转成用户可读提示
        return {}, {}, _friendly_browser_error(exc)

    rows: list[tuple[str, str, datetime | None]] = []
    values: dict[str, dict[str, str]] = {}
    for cookie in jar:
        host = (cookie.domain or "").lstrip(".").lower()
        expires = None
        if cookie.expires:
            try:
                expires = datetime.fromtimestamp(cookie.expires, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                expires = None
        rows.append((host, cookie.name, expires))
        platform = _platform_of(host, cookie.name)
        if platform:
            values.setdefault(platform, {})[cookie.name] = cookie.value
    return values, _match_cookies(rows), ""


def _bili_login(values: dict[str, str], proxy: str, timeout: float) -> tuple[str, str]:
    """用 B 站 nav 接口做一次真实登录校验(SESSDATA → isLogin / uname)。"""
    if not values.get("SESSDATA"):
        return "missing", "未找到 SESSDATA"
    if (proxy or "").lower().startswith("socks"):
        return "present", "找到 SESSDATA(socks 代理下跳过在线校验)"
    cookie_header = "; ".join(f"{k}={v}" for k, v in values.items() if v)
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(_NAV_URL, headers={
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/",
        "Cookie": cookie_header,
    })
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read(65536).decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 — 校验失败不等于 Cookie 无效
        return "present", f"找到 SESSDATA(在线校验未完成:{str(exc)[:60]})"
    data = payload.get("data") or {}
    if payload.get("code") == 0 and data.get("isLogin"):
        uname = data.get("uname") or "已登录"
        return "verified", f"在线校验通过:{uname}"
    return "expired", "Cookie 已失效(接口返回未登录)"


def test_configuration(browser: str = "", cookie_file: str = "", proxy: str = "",
                       timeout: float = 4.0) -> TestReport:
    """用当前填写的三项配置做真实测试:网络可达性 + Cookie 登录态。

    与「一键检测」的区别:检测只读元信息、给建议;测试会真的联网并读取 Cookie 值,
    因此能回答"这套配置现在能不能用"。
    """
    proxy = (proxy or "").strip()
    pool = ThreadPoolExecutor(max_workers=len(PLATFORMS))
    try:
        futures = {p: pool.submit(_probe, PROBE_HOSTS[p], proxy, timeout) for p in PLATFORMS}
        net = {p: _result_or_timeout(f, timeout + 0.3) for p, f in futures.items()}
    finally:
        pool.shutdown(wait=False)

    values: dict[str, dict[str, str]] = {}
    states: dict[str, PlatformCookie] = {}
    source = ""
    source_error = ""
    file_path = (cookie_file or "").strip()
    if file_path:
        if Path(file_path).is_file():
            total, states, values, error = parse_cookie_file(Path(file_path))
            source = f"cookies.txt(共 {total} 条)"
            source_error = error
        else:
            source = "cookies.txt(路径不存在)"
            source_error = f"文件不存在:{file_path}"
    elif browser:
        values, states, source_error = _browser_cookie_values(browser)
        source = f"{browser} 浏览器 Cookie"
    else:
        source = "未配置 Cookie(仅能下载公开内容)"
        source_error = ""  # 未配置不算"读取失败",各平台按 missing 展示

    report = TestReport(source=source)
    for platform in PLATFORMS:
        ok, ms, _error = net[platform]
        cookie = states.get(platform)
        state = "missing"
        detail = "未找到登录 Cookie,仅能下载公开内容"
        if platform == "bilibili" and cookie and cookie.present and not cookie.expired:
            state, detail = _bili_login(values.get("bilibili", {}), proxy, timeout)
        elif cookie and cookie.present and cookie.expired:
            state = "expired"
            detail = f"{cookie.name} 已过期({cookie.expires_at})"
        elif cookie and cookie.present:
            state = "present"
            detail = f"找到 {cookie.name}" + (
                f",有效期至 {cookie.expires_at}" if cookie.expires_at else ",会话级")
        elif source_error:
            state = "unreadable"
            detail = source_error
        report.items.append(PlatformTest(platform, ok, ms, state, detail))

    youtube = report.by_platform("youtube")
    if youtube and not youtube.network_ok:
        report.warnings.append("YouTube 不可达:境外视频需要配置代理(其它平台不受影响)")
    if source_error and (file_path or browser):
        report.warnings.append(f"Cookie 读取失败:{source_error}")
    domestic = [i for i in report.items if i.platform != "youtube"]
    net_ok = all(i.network_ok for i in domestic)
    cookie_ok = any(i.cookie_state in ("verified", "present") for i in report.items)
    report.ok = net_ok and cookie_ok
    if report.ok:
        report.summary = "配置可用:国内平台网络正常,已识别到登录 Cookie"
    elif not net_ok:
        report.summary = "网络异常:有平台无法连接,请检查网络或代理"
    elif not cookie_ok:
        report.summary = "网络正常,但没读到登录 Cookie:高清/会员/风控内容可能下载失败"
    else:
        report.summary = "配置基本可用,请留意下方提示"
    return report


# ---------------------------------------------------------------- 导出


def export_browser_cookies(browser: str, dest: Path) -> Path:
    """把浏览器 Cookie 导出成 Netscape cookies.txt(需关闭浏览器)。

    导出后 yt-dlp 直接读文件,不再依赖浏览器进程与解密,稳定性最好;
    但文件内含登录凭据,只在用户明确点按钮时生成。
    """
    if not browser:
        raise NetcheckError("请先选择浏览器")
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except Exception as exc:  # noqa: BLE001
        raise NetcheckError(f"当前版本缺少 yt-dlp:{exc}") from exc
    try:
        jar = extract_cookies_from_browser(browser, logger=_QuietYdlLogger())
    except Exception as exc:  # noqa: BLE001
        raise NetcheckError(_friendly_browser_error(exc)) from exc
    if not len(jar):
        raise NetcheckError("浏览器里没有可导出的 Cookie,请先在浏览器登录对应平台")
    dest = Path(dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        jar.save(str(dest), ignore_discard=True, ignore_expires=True)
    except OSError as exc:
        raise NetcheckError(f"写入失败:{exc.strerror or exc}") from exc
    return dest
