"""网络与 Cookie 自动检测(netcheck)的单元测试。

全部离线:浏览器库用临时 SQLite 造,网络探测一律 monkeypatch 掉,
不依赖本机装了哪些浏览器、也不真的联网。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import netcheck


# ---------------------------------------------------------------- 辅助


def _make_chromium_db(path: Path, rows: list[tuple[str, str, int]]) -> Path:
    """造一个 Chrome 形状的 Cookies 库(host_key,name,expires_utc)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, expires_utc INTEGER)")
    conn.executemany("INSERT INTO cookies VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def _make_firefox_db(path: Path, rows: list[tuple[str, str, int]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, expiryDate INTEGER)")
    conn.executemany("INSERT INTO moz_cookies VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def _future_ms(days: int = 30) -> int:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return int((dt - netcheck._EPOCH_1601).total_seconds() * 1_000_000)


def _past_ms(days: int = 30) -> int:
    return _future_ms(-days)


def _cookie_file(path: Path, rows: list[tuple[str, str, int]], header: bool = True) -> Path:
    lines = ["# Netscape HTTP Cookie File"] if header else []
    for domain, name, expires in rows:
        lines.append(f"{domain}\tTRUE\t/\tFALSE\t{expires}\t{name}\tvalue-{name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _browser(key: str, platforms: dict[str, str], *, running: bool = False,
             label: str = "") -> netcheck.BrowserInfo:
    cookies = {p: netcheck.PlatformCookie(p, name) for p, name in platforms.items()}
    return netcheck.BrowserInfo(key=key, label=label or key.title(), profile="Default",
                                cookie_db=Path("x"), running=running,
                                cookie_count=10, platforms=cookies)


def _file(path: Path, platforms: dict[str, str]) -> netcheck.CookieFileInfo:
    cookies = {p: netcheck.PlatformCookie(p, name) for p, name in platforms.items()}
    return netcheck.CookieFileInfo(path=path, cookie_count=20, platforms=cookies)


# ---------------------------------------------------------------- Cookie 匹配


def test_platform_of_matches_known_cookies():
    assert netcheck._platform_of(".bilibili.com", "SESSDATA") == "bilibili"
    assert netcheck._platform_of("www.bilibili.com", "DedeUserID") == "bilibili"
    assert netcheck._platform_of(".douyin.com", "sessionid") == "douyin"
    assert netcheck._platform_of(".xiaohongshu.com", "web_session") == "xiaohongshu"
    assert netcheck._platform_of(".kuaishou.com", "passToken") == "kuaishou"
    assert netcheck._platform_of(".youtube.com", "SAPISID") == "youtube"
    # Google 账号域下的 SID 也代表 YouTube 已登录
    assert netcheck._platform_of(".google.com", "SID") == "youtube"


def test_platform_of_ignores_unrelated_cookies():
    assert netcheck._platform_of(".bilibili.com", "buvid3") is None
    assert netcheck._platform_of(".douyin.com", "SESSDATA") is None   # 名字对、域名不对
    assert netcheck._platform_of("", "SESSDATA") is None
    assert netcheck._platform_of(".bilibili.com", "") is None


def test_match_cookies_prefers_valid_over_expired():
    rows = [
        (".bilibili.com", "SESSDATA", datetime.now(timezone.utc) - timedelta(days=1)),
        (".bilibili.com", "DedeUserID", datetime.now(timezone.utc) + timedelta(days=5)),
    ]
    hits = netcheck._match_cookies(rows)
    assert hits["bilibili"].name == "DedeUserID"
    assert hits["bilibili"].expired is False


def test_match_cookies_keeps_expired_when_it_is_the_only_one():
    rows = [(".douyin.com", "sessionid", datetime.now(timezone.utc) - timedelta(days=2))]
    hits = netcheck._match_cookies(rows)
    assert hits["douyin"].expired is True
    assert hits["douyin"].expires_at  # 有具体日期,便于提示"已过期"


def test_session_cookie_has_no_expiry_date():
    hits = netcheck._match_cookies([(".bilibili.com", "SESSDATA", None)])
    assert hits["bilibili"].expires_at == ""
    assert hits["bilibili"].expired is False


# ---------------------------------------------------------------- 解析 cookies.txt


def test_parse_cookie_file_reads_platforms_and_values(tmp_path):
    path = _cookie_file(tmp_path / "cookies.txt", [
        (".bilibili.com", "SESSDATA", 4102444800),
        (".douyin.com", "sessionid", 4102444800),
        (".example.com", "foo", 4102444800),
    ])
    total, platforms, values, error = netcheck.parse_cookie_file(path)
    assert error == ""
    assert total == 3
    assert set(platforms) == {"bilibili", "douyin"}
    assert values["bilibili"]["SESSDATA"] == "value-SESSDATA"


def test_parse_cookie_file_handles_httponly_prefix_and_comments(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        "# comment line\n"
        "#HttpOnly_.bilibili.com\tTRUE\t/\tFALSE\t4102444800\tSESSDATA\tv\n"
        "broken-line-without-tabs\n",
        encoding="utf-8")
    total, platforms, values, error = netcheck.parse_cookie_file(path)
    assert error == ""
    assert total == 1
    assert platforms["bilibili"].name == "SESSDATA"
    assert values["bilibili"]["SESSDATA"] == "v"


def test_parse_cookie_file_reports_missing_file(tmp_path):
    total, platforms, values, error = netcheck.parse_cookie_file(tmp_path / "nope.txt")
    assert total == 0 and platforms == {} and values == {}
    assert "不存在" in error or "读取失败" in error


def test_parse_cookie_file_flags_expired_cookie(tmp_path):
    path = _cookie_file(tmp_path / "cookies.txt", [(".douyin.com", "sessionid", 1000000000)])
    _total, platforms, _values, error = netcheck.parse_cookie_file(path)
    assert error == ""
    assert platforms["douyin"].expired is True


# ---------------------------------------------------------------- 发现 cookies.txt


def test_detect_cookie_files_only_picks_cookie_named_txt(tmp_path):
    _cookie_file(tmp_path / "cookies.txt", [(".bilibili.com", "SESSDATA", 4102444800)])
    _cookie_file(tmp_path / "bili-cookies.txt", [(".douyin.com", "sessionid", 4102444800)])
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "empty-cookie.txt").write_text("", encoding="utf-8")

    found = netcheck.detect_cookie_files([tmp_path])
    names = {f.path.name for f in found}
    assert names == {"cookies.txt", "bili-cookies.txt"}
    # 覆盖平台多(都只有 1 个)时按文件名排序,至少不抛错
    assert all(f.cookie_count == 1 for f in found)


def test_detect_cookie_files_includes_explicit_extra_file(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    custom = _cookie_file(outside / "my-cookies.txt", [(".bilibili.com", "SESSDATA", 4102444800)])
    found = netcheck.detect_cookie_files([tmp_path], [custom])
    assert [f.path for f in found] == [custom]


def test_detect_cookie_files_sorts_by_coverage(tmp_path):
    _cookie_file(tmp_path / "a-cookies.txt", [(".bilibili.com", "SESSDATA", 4102444800)])
    _cookie_file(tmp_path / "b-cookies.txt", [
        (".bilibili.com", "SESSDATA", 4102444800),
        (".douyin.com", "sessionid", 4102444800),
    ])
    found = netcheck.detect_cookie_files([tmp_path])
    assert found[0].path.name == "b-cookies.txt"
    assert found[0].coverage == 2


def test_detect_cookie_files_ignores_non_cookie_sqlite_dump(tmp_path):
    (tmp_path / "cookies.txt").write_text("not\t a\t cookie\n", encoding="utf-8")
    assert netcheck.detect_cookie_files([tmp_path]) == []


# ---------------------------------------------------------------- 浏览器扫描


def test_detect_browsers_reads_chromium_profile(tmp_path, monkeypatch):
    db = _make_chromium_db(tmp_path / "User Data" / "Default" / "Network" / "Cookies", [
        (".bilibili.com", "SESSDATA", _future_ms()),
        (".douyin.com", "sessionid", _future_ms()),
        (".youtube.com", "SAPISID", _future_ms()),
    ])
    monkeypatch.setattr(netcheck, "_browser_roots",
                        lambda: [("chrome", "Chrome", db.parents[2], False)])

    browsers = netcheck.detect_browsers()
    assert len(browsers) == 1
    info = browsers[0]
    assert info.key == "chrome" and info.readable
    assert info.cookie_count == 3
    assert set(info.login_platforms) == {"bilibili", "douyin", "youtube"}


def test_detect_browsers_reads_firefox_profile(tmp_path, monkeypatch):
    db = _make_firefox_db(tmp_path / "Firefox" / "Profiles" / "abc.default" / "cookies.sqlite", [
        (".bilibili.com", "SESSDATA", 4102444800),
    ])
    monkeypatch.setattr(netcheck, "_browser_roots",
                        lambda: [("firefox", "Firefox", db.parents[2], True)])

    browsers = netcheck.detect_browsers()
    assert len(browsers) == 1
    assert browsers[0].readable
    assert browsers[0].login_platforms == ["bilibili"]


def test_detect_browsers_marks_locked_database_as_running(tmp_path, monkeypatch):
    db = _make_chromium_db(tmp_path / "User Data" / "Default" / "Network" / "Cookies",
                           [(".bilibili.com", "SESSDATA", _future_ms())])
    monkeypatch.setattr(netcheck, "_browser_roots",
                        lambda: [("chrome", "Chrome", db.parents[2], False)])

    def fake_copy(src, dst, **kwargs):
        raise PermissionError(13, "另一个程序正在使用此文件")

    monkeypatch.setattr(netcheck.shutil, "copy2", fake_copy)
    browsers = netcheck.detect_browsers()
    assert browsers[0].running is True
    assert browsers[0].readable is False
    assert browsers[0].coverage == 0
    assert "正在运行" in browsers[0].describe()


def test_detect_browsers_handles_corrupt_database(tmp_path, monkeypatch):
    db = tmp_path / "User Data" / "Default" / "Network" / "Cookies"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a sqlite database")
    monkeypatch.setattr(netcheck, "_browser_roots",
                        lambda: [("chrome", "Chrome", db.parents[2], False)])

    browsers = netcheck.detect_browsers()
    assert browsers[0].readable is False
    assert "数据库" in browsers[0].error


def test_detect_browsers_sorts_readable_before_locked():
    readable = _browser("edge", {"bilibili": "SESSDATA", "douyin": "sessionid"})
    locked = _browser("chrome", {}, running=True)
    readable.cookie_db = Path("x")
    ordered = sorted([locked, readable],
                     key=lambda b: (0 if b.readable else 1 if b.running else 2,
                                    -b.coverage, b.label))
    assert ordered[0].key == "edge"


# ---------------------------------------------------------------- 代理


def test_normalize_proxy_adds_scheme_and_picks_https_entry():
    assert netcheck._normalize_proxy("127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert netcheck._normalize_proxy("http://127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert netcheck._normalize_proxy(
        "http=127.0.0.1:8080;https=127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert netcheck._normalize_proxy("") == ""
    assert netcheck._normalize_proxy("   ") == ""


def test_detect_proxies_marks_listening_ports(monkeypatch):
    monkeypatch.setattr(netcheck, "_system_proxy", lambda: "")
    monkeypatch.setattr(netcheck, "_env_proxies", lambda: [])
    monkeypatch.setattr(netcheck, "_tcp_open",
                        lambda host, port, timeout=0.35: port == 7890)
    found = netcheck.detect_proxies()
    assert [c.url for c in found] == ["http://127.0.0.1:7890"]
    assert found[0].listening is True
    assert found[0].source == "本机端口"


def test_detect_proxies_dedups_system_and_port(monkeypatch):
    monkeypatch.setattr(netcheck, "_system_proxy", lambda: "http://127.0.0.1:7890")
    monkeypatch.setattr(netcheck, "_env_proxies", lambda: ["http://127.0.0.1:7890"])
    monkeypatch.setattr(netcheck, "_tcp_open", lambda host, port, timeout=0.35: port == 7890)
    found = netcheck.detect_proxies()
    assert [c.url for c in found] == ["http://127.0.0.1:7890"]
    assert found[0].source == "系统代理"


def test_probe_network_marks_working_proxy(monkeypatch):
    def fake_probe(url, proxy="", timeout=4.0):
        if not proxy:
            return ("youtube.com" not in url), 120, "" if "youtube.com" not in url else "超时"
        return True, 90, ""

    monkeypatch.setattr(netcheck, "_probe", fake_probe)
    monkeypatch.setattr(netcheck, "detect_proxies", lambda: [
        netcheck.ProxyCandidate("http://127.0.0.1:7890", "系统代理", listening=True),
    ])
    report = netcheck.probe_network()
    assert report.direct["bilibili"].ok is True
    assert report.direct["youtube"].ok is False
    assert report.working_proxy == "http://127.0.0.1:7890"
    assert report.proxies[0].verified is True


def test_find_working_proxy_returns_none_without_candidates(monkeypatch):
    monkeypatch.setattr(netcheck, "detect_proxies", lambda: [])
    assert netcheck.find_working_proxy() is None


def test_dns_ok_short_circuits_for_ip():
    assert netcheck._dns_ok("127.0.0.1", 1.0) is True
    assert netcheck._dns_ok("", 1.0) is True


# ---------------------------------------------------------------- 建议


def test_build_report_prefers_cookie_file_when_covering_more(tmp_path):
    file_info = _file(tmp_path / "cookies.txt",
                      {"bilibili": "SESSDATA", "douyin": "sessionid",
                       "xiaohongshu": "web_session"})
    browser = _browser("chrome", {"bilibili": "SESSDATA"})
    network = netcheck.NetworkReport(
        direct={p: netcheck.PlatformProbe(p, True, 100) for p in netcheck.PLATFORMS})
    report = netcheck.build_report([browser], [file_info], network,
                                   current_browser="edge", current_proxy="")
    assert report.suggested_cookie_file == str(tmp_path / "cookies.txt")
    assert report.suggested_browser == "chrome"


def test_build_report_prefers_browser_when_it_covers_more(tmp_path):
    file_info = _file(tmp_path / "cookies.txt", {"bilibili": "SESSDATA"})
    browser = _browser("chrome", {"bilibili": "SESSDATA", "douyin": "sessionid"})
    network = netcheck.NetworkReport(
        direct={p: netcheck.PlatformProbe(p, True, 100) for p in netcheck.PLATFORMS})
    report = netcheck.build_report([browser], [file_info], network,
                                   current_browser="", current_proxy="")
    assert report.suggested_browser == "chrome"
    assert report.suggested_cookie_file == ""


def test_build_report_keeps_current_settings_when_nothing_detected(tmp_path):
    network = netcheck.NetworkReport(
        direct={p: netcheck.PlatformProbe(p, True, 100) for p in netcheck.PLATFORMS})
    report = netcheck.build_report([], [], network,
                                   current_browser="edge", current_cookie_file="keep.txt",
                                   current_proxy="http://127.0.0.1:7890")
    assert report.suggested_browser == "edge"
    assert report.suggested_cookie_file == "keep.txt"
    # YouTube 直连可用 -> 代理被清空(不强留一个可能已经失效的值)
    assert report.suggested_proxy == ""


def test_build_report_explains_locked_browser(tmp_path):
    locked = _browser("chrome", {}, running=True)
    network = netcheck.NetworkReport(
        direct={p: netcheck.PlatformProbe(p, True, 100) for p in netcheck.PLATFORMS})
    report = netcheck.build_report([locked], [], network, current_browser="")
    assert report.suggested_browser == ""
    assert any("正在运行" in note for note in report.notes)


def test_build_report_ignores_cookie_file_without_login(tmp_path):
    useless = _file(tmp_path / "cookies.txt", {})
    network = netcheck.NetworkReport(
        direct={p: netcheck.PlatformProbe(p, True, 100) for p in netcheck.PLATFORMS})
    report = netcheck.build_report([], [useless], network,
                                   current_browser="", current_cookie_file="")
    assert report.suggested_cookie_file == ""


def test_build_report_suggests_proxy_when_youtube_blocked():
    network = netcheck.NetworkReport(
        direct={p: netcheck.PlatformProbe(p, p != "youtube", 100) for p in netcheck.PLATFORMS},
        proxies=[netcheck.ProxyCandidate("http://127.0.0.1:7897", "系统代理", verified=True)],
        working_proxy="http://127.0.0.1:7897",
        system_proxy="http://127.0.0.1:7897",
    )
    report = netcheck.build_report([], [], network)
    assert report.suggested_proxy == "http://127.0.0.1:7897"
    assert any("YouTube" in note for note in report.notes)


def test_platform_states_merges_file_and_browser():
    report = netcheck.DetectionReport(
        browsers=[_browser("chrome", {"bilibili": "SESSDATA"})],
        cookie_files=[netcheck.CookieFileInfo(
            path=Path("c.txt"), cookie_count=1,
            platforms={"douyin": netcheck.PlatformCookie("douyin", "sessionid")})],
    )
    assert set(report.platform_states) == {"bilibili", "douyin"}


# ---------------------------------------------------------------- 测试连接


def test_test_configuration_verifies_bilibili_cookie(tmp_path, monkeypatch):
    path = _cookie_file(tmp_path / "cookies.txt", [
        (".bilibili.com", "SESSDATA", 4102444800),
        (".douyin.com", "sessionid", 4102444800),
    ])
    monkeypatch.setattr(netcheck, "_probe", lambda url, proxy="", timeout=4.0: (True, 80, ""))
    monkeypatch.setattr(netcheck, "_bili_login",
                        lambda values, proxy, timeout: ("verified", "在线校验通过:测试用户"))
    report = netcheck.test_configuration("", str(path), "")
    assert report.ok is True
    assert report.by_platform("bilibili").cookie_state == "verified"
    assert report.by_platform("douyin").cookie_state == "present"
    assert "cookies.txt" in report.source


def test_test_configuration_reports_locked_browser(tmp_path, monkeypatch):
    """浏览器被占用、连元信息都读不到时,五平台应全部标 unreadable。"""
    monkeypatch.setattr(netcheck, "_probe", lambda url, proxy="", timeout=4.0: (True, 80, ""))
    monkeypatch.setattr(netcheck, "_browser_cookie_values",
                        lambda browser: ({}, {}, None,
                                         "浏览器正在运行,Cookie 数据库被占用(关闭浏览器后重试)"))
    report = netcheck.test_configuration("chrome", "", "")
    assert report.ok is False
    assert report.by_platform("bilibili").cookie_state == "unreadable"
    assert any("读取失败" in w for w in report.warnings)


def test_test_configuration_value_error_keeps_metadata_states(tmp_path, monkeypatch):
    """v20 场景:值解不开,但元信息读到登录态 —— 不能谎报成"读取失败"。

    Chrome 127+ 的 Cookie 是应用绑定加密,值只能由浏览器本体在原路径解密;
    这时用户真正需要知道的是「登录态在、值取不到、怎么办」,而不是「全平台读取失败」。
    """
    metadata = {
        "bilibili": netcheck.PlatformCookie("bilibili", "SESSDATA", ".bilibili.com", "2027-01-01"),
        "douyin": netcheck.PlatformCookie("douyin", "sessionid", ".douyin.com", "2027-01-01"),
    }
    monkeypatch.setattr(netcheck, "_probe", lambda url, proxy="", timeout=4.0: (True, 80, ""))
    monkeypatch.setattr(
        netcheck, "_browser_cookie_values",
        lambda browser: ({}, {}, metadata,
                         "浏览器正在运行:Cookie 用的是应用绑定加密(v20),第三方解不开 ——"
                         "关闭浏览器后点一次「测试连接」或「导出」即可,或改用 cookies.txt"))
    report = netcheck.test_configuration("chrome", "", "")
    # 有登录态的平台不再被标成 unreadable,而是 present + 说明值的问题
    assert report.by_platform("bilibili").cookie_state == "present"
    assert report.by_platform("douyin").cookie_state == "present"
    assert "值加密读不到" in report.by_platform("bilibili").detail
    # 没有登录态的平台仍然是 missing(不是 unreadable)
    assert report.by_platform("youtube").cookie_state == "missing"
    assert any("应用绑定加密" in w for w in report.warnings)


def test_value_error_is_not_reported_as_ok(tmp_path, monkeypatch):
    """值取不到时绝不能报「配置可用」—— yt-dlp 拿到空值,下载照样失败。

    这是最糟的一类错误:绿灯让用户去下载,失败后报"需要 Cookie",无从排查。
    """
    metadata = {"bilibili": netcheck.PlatformCookie(
        "bilibili", "SESSDATA", ".bilibili.com", "2027-01-01")}
    monkeypatch.setattr(netcheck, "_probe", lambda url, proxy="", timeout=4.0: (True, 80, ""))
    monkeypatch.setattr(netcheck, "_browser_cookie_values",
                        lambda b: ({}, {}, metadata, "值加密读不到"))
    report = netcheck.test_configuration("chrome", "", "")
    assert report.ok is False
    assert "值取不到" in report.summary
    # 平台行仍然如实显示登录态存在(不谎报失败,也不谎报可用)
    assert report.by_platform("bilibili").cookie_state == "present"


def test_full_values_are_reported_ok(tmp_path, monkeypatch):
    """对照组:值真的拿到了才允许报可用。"""
    monkeypatch.setattr(netcheck, "_probe", lambda url, proxy="", timeout=4.0: (True, 80, ""))
    monkeypatch.setattr(netcheck, "_bili_login",
                        lambda values, proxy, timeout: ("verified", "在线校验通过:me"))
    monkeypatch.setattr(netcheck, "_browser_cookie_values",
                        lambda b: ({"bilibili": {"SESSDATA": "v"}},
                                   {"bilibili": netcheck.PlatformCookie(
                                       "bilibili", "SESSDATA", ".bilibili.com", "2027-01-01")},
                                   {}, ""))
    report = netcheck.test_configuration("chrome", "", "")
    assert report.ok is True
    assert "配置可用" in report.summary


def test_test_configuration_unknown_browser_metadata_is_empty(tmp_path, monkeypatch):
    """拿不到元信息时保持旧行为:unreadable + 可行动文案。"""
    monkeypatch.setattr(netcheck, "_probe", lambda url, proxy="", timeout=4.0: (True, 80, ""))
    monkeypatch.setattr(netcheck, "_browser_cookie_values",
                        lambda browser: ({}, {}, None, "没找到该浏览器的 Cookie 数据库"))
    report = netcheck.test_configuration("chrome", "", "")
    assert report.by_platform("bilibili").cookie_state == "unreadable"
    # 平台行用统一短文案(不把同一句话重复五遍),完整原因进 warnings
    assert report.by_platform("bilibili").detail == netcheck.SHORT_UNREADABLE_DETAIL
    assert any("没找到" in w for w in report.warnings)


def test_test_configuration_without_cookie_config(tmp_path, monkeypatch):
    monkeypatch.setattr(netcheck, "_probe", lambda url, proxy="", timeout=4.0: (True, 80, ""))
    report = netcheck.test_configuration("", "", "")
    assert report.ok is False
    assert all(i.cookie_state == "missing" for i in report.items)
    assert "没有登录 Cookie" in report.summary or "没读到" in report.summary


def test_test_configuration_warns_when_youtube_unreachable(tmp_path, monkeypatch):
    path = _cookie_file(tmp_path / "cookies.txt", [(".bilibili.com", "SESSDATA", 4102444800)])

    def fake_probe(url, proxy="", timeout=4.0):
        return ("youtube" not in url), 80, ""

    monkeypatch.setattr(netcheck, "_probe", fake_probe)
    monkeypatch.setattr(netcheck, "_bili_login",
                        lambda values, proxy, timeout: ("present", "找到 SESSDATA"))
    report = netcheck.test_configuration("", str(path), "")
    assert report.by_platform("youtube").network_ok is False
    assert any("YouTube 不可达" in w for w in report.warnings)
    # 国内平台 + Cookie 都正常 -> 整体仍算可用
    assert report.ok is True


def test_test_configuration_missing_cookie_file_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(netcheck, "_probe", lambda url, proxy="", timeout=4.0: (True, 80, ""))
    report = netcheck.test_configuration("chrome", str(tmp_path / "ghost.txt"), "")
    assert report.by_platform("bilibili").cookie_state == "unreadable"
    assert "不存在" in report.by_platform("bilibili").detail


# ---------------------------------------------------------------- 导出


class _FakeJar:
    def __init__(self, cookies):
        self._cookies = cookies

    def __len__(self):
        return len(self._cookies)

    def __iter__(self):
        return iter(())

    def save(self, path, ignore_discard=False, ignore_expires=False):
        Path(path).write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")


def test_export_browser_cookies_writes_file(tmp_path, monkeypatch):
    from yt_dlp import cookies as ytdlp_cookies

    monkeypatch.setattr(ytdlp_cookies, "extract_cookies_from_browser",
                        lambda name, logger=None: _FakeJar([1, 2]))
    dest = tmp_path / "out" / "cookies.txt"
    result = netcheck.export_browser_cookies("chrome", dest)
    assert result == dest
    assert dest.is_file()


def test_export_browser_cookies_requires_browser(tmp_path):
    with pytest.raises(netcheck.NetcheckError):
        netcheck.export_browser_cookies("", tmp_path / "c.txt")


def test_export_browser_cookies_raises_friendly_error(tmp_path, monkeypatch):
    from yt_dlp import cookies as ytdlp_cookies

    def boom(name, logger=None):
        raise RuntimeError("Could not copy Chrome cookie database")

    monkeypatch.setattr(ytdlp_cookies, "extract_cookies_from_browser", boom)
    with pytest.raises(netcheck.NetcheckError) as info:
        netcheck.export_browser_cookies("chrome", tmp_path / "c.txt")
    assert "正在运行" in str(info.value)


def test_export_browser_cookies_rejects_empty_jar(tmp_path, monkeypatch):
    from yt_dlp import cookies as ytdlp_cookies

    monkeypatch.setattr(ytdlp_cookies, "extract_cookies_from_browser",
                        lambda name, logger=None: _FakeJar([]))
    with pytest.raises(netcheck.NetcheckError):
        netcheck.export_browser_cookies("chrome", tmp_path / "c.txt")


# ---------------------------------------------------------------- 其它


def test_friendly_browser_error_maps_lock_message():
    assert "正在运行" in netcheck._friendly_browser_error(
        RuntimeError("Could not copy Chrome cookie database"))
    assert "未找到" in netcheck._friendly_browser_error(RuntimeError("could not find profile"))
    assert "解密" in netcheck._friendly_browser_error(RuntimeError("Failed to decrypt cookie"))


def test_expiry_helpers_handle_bad_values():
    assert netcheck._expiry_from_chrome(0) is None
    assert netcheck._expiry_from_chrome("bad") is None
    assert netcheck._expiry_from_unix(0) is None
    assert netcheck._expiry_from_unix("") is None
    assert netcheck._expiry_from_chrome(_future_ms(1)).year >= 2026


def test_default_cookie_dirs_includes_downloads(tmp_path):
    dirs = netcheck.default_cookie_dirs(tmp_path, tmp_path / "data")
    assert tmp_path in dirs
    assert any("Downloads" in str(d) for d in dirs)

# ---------------------------------------------------------------- cookiebridge

from app import cookiebridge


def test_bridge_profile_root_splits_user_data_and_profile(tmp_path):
    """Chromium 布局:<User Data>/Local State + <User Data>/Default/Network/Cookies。"""
    root = tmp_path / "User Data"
    (root / "Default" / "Network").mkdir(parents=True)
    (root / "Local State").write_text("{}", encoding="utf-8")
    db = root / "Default" / "Network" / "Cookies"
    db.write_bytes(b"")
    assert cookiebridge.profile_root(root / "Default") == (root, Path("Default"))
    # Opera 形态:根目录自身就是 profile
    assert cookiebridge.profile_root(root) == (root, Path("."))


def test_bridge_writes_netscape_that_netcheck_can_parse(tmp_path):
    """导出的 cookies.txt 必须能被项目自己的解析器读出登录态(闭环)。"""
    cookies = [
        {"name": "SESSDATA", "value": "abc", "domain": ".bilibili.com", "path": "/",
         "expires": 4102444800, "httpOnly": True, "secure": True},
        {"name": "sessionid", "value": "xyz", "domain": ".douyin.com", "path": "/",
         "expires": -1, "httpOnly": False, "secure": False},
        {"name": "", "value": "skip", "domain": ".bilibili.com", "path": "/"},
    ]
    dest = cookiebridge.write_netscape(cookies, tmp_path / "cookies.txt")
    total, platforms, values, error = netcheck.parse_cookie_file(dest)
    assert error == ""
    assert total == 2  # 空名字被丢弃
    assert set(platforms) == {"bilibili", "douyin"}
    assert not platforms["bilibili"].expired
    assert values["bilibili"]["SESSDATA"] == "abc"
    # HttpOnly 前缀要写进文件(否则 yt-dlp 解析时会被当注释吞掉)
    assert "#HttpOnly_.bilibili.com" in dest.read_text(encoding="utf-8")


def test_bridge_session_cookie_gets_placeholder_expiry(tmp_path):
    """会话级 Cookie(expires<=0)写文件时用远期值占位,不能被解析成已过期。"""
    dest = cookiebridge.write_netscape(
        [{"name": "SESSDATA", "value": "v", "domain": ".bilibili.com", "expires": 0}],
        tmp_path / "c.txt")
    _total, platforms, _values, error = netcheck.parse_cookie_file(dest)
    assert error == ""
    assert platforms["bilibili"].expired is False


def test_bridge_describe_prefixes_flags_v20():
    assert "v20" in cookiebridge.describe_prefixes({"763230": 5})
    assert "应用绑定加密" in cookiebridge.describe_prefixes({"763230": 5})
    assert cookiebridge.describe_prefixes({"763130": 5}).startswith("v10")
    assert cookiebridge.describe_prefixes({}) == ""


def test_bridge_encryption_prefixes_reads_without_browser(tmp_path):
    """只读密文前缀,不解密:用临时库验证 v20 统计。"""
    db = tmp_path / "Cookies"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, expires_utc INTEGER,"
                 " encrypted_value BLOB)")
    conn.execute("INSERT INTO cookies VALUES ('.a.com','a',0,?)",
                 (b"v20" + b"\x00" * 20,))
    conn.execute("INSERT INTO cookies VALUES ('.b.com','b',0,?)",
                 (b"v10" + b"\x00" * 20,))
    conn.commit()
    conn.close()
    prefixes = cookiebridge.encryption_prefixes(db)
    assert prefixes.get("763230") == 1
    assert prefixes.get("763130") == 1


def test_bridge_has_login_cookie_detects_marker():
    cookies = [{"name": "SESSDATA", "value": "x"}]
    assert cookiebridge.has_login_cookie(cookies, {"SESSDATA", "DedeUserID"})
    assert not cookiebridge.has_login_cookie(cookies, {"sessionid"})


# ---------------------------------------------------------------- 桥接与元信息的分工


def test_browser_cookie_values_prefers_bridge(tmp_path, monkeypatch):
    """桥接能拿到值时,不该再去碰 yt-dlp(那条路在 v20 上必失败)。"""
    def fake_read_cookies(browser):
        return [{"name": "SESSDATA", "value": "v", "domain": ".bilibili.com",
                 "expires": 4102444800}]

    def boom(*a, **k):
        raise AssertionError("不该调用 yt-dlp")

    monkeypatch.setattr(cookiebridge, "read_cookies", fake_read_cookies)
    monkeypatch.setattr(netcheck, "_browser_metadata", lambda browser: {})
    import yt_dlp.cookies as ytdlp_cookies
    monkeypatch.setattr(ytdlp_cookies, "extract_cookies_from_browser", boom)
    values, states, metadata, error = netcheck._browser_cookie_values("chrome")
    assert error == ""
    assert values["bilibili"]["SESSDATA"] == "v"
    assert states["bilibili"].name == "SESSDATA"


def test_browser_cookie_values_falls_back_to_ytdlp(tmp_path, monkeypatch):
    """桥接失败(v20/占用)时退回 yt-dlp,老 v10 库仍能读出来。"""
    class _Jar:
        def __iter__(self):
            class _C:
                domain = ".bilibili.com"
                name = "SESSDATA"
                value = "old"
                expires = 4102444800
            return iter([_C()])

    def bridge_fail(browser, timeout=30.0):
        raise cookiebridge.CookieBridgeError("浏览器正在运行")

    monkeypatch.setattr(cookiebridge, "read_cookies", bridge_fail)
    monkeypatch.setattr(netcheck, "_browser_metadata", lambda browser: {})
    import yt_dlp.cookies as ytdlp_cookies
    monkeypatch.setattr(ytdlp_cookies, "extract_cookies_from_browser",
                        lambda name, logger=None: _Jar())
    values, states, _metadata, error = netcheck._browser_cookie_values("chrome")
    assert error == ""
    assert values["bilibili"]["SESSDATA"] == "old"


def test_browser_cookie_values_both_paths_fail_reports_bridge_hint(monkeypatch):
    """两条路都失败时,必须给桥接的可行动提示(那是唯一真能通的路)。"""
    def bridge_fail(browser, timeout=30.0):
        raise cookiebridge.CookieBridgeError("关闭浏览器后重试")

    def ytdlp_fail(name, logger=None):
        raise RuntimeError("Failed to decrypt with DPAPI")

    monkeypatch.setattr(cookiebridge, "read_cookies", bridge_fail)
    monkeypatch.setattr(netcheck, "_browser_metadata", lambda browser: None)
    import yt_dlp.cookies as ytdlp_cookies
    monkeypatch.setattr(ytdlp_cookies, "extract_cookies_from_browser", ytdlp_fail)
    _values, _states, metadata, error = netcheck._browser_cookie_values("chrome")
    assert metadata is None
    assert "关闭浏览器" in error


def test_friendly_error_mentions_abe_not_upgrade():
    """v20 的报错要引导去导出/关闭浏览器,而不是含糊说"版本较新"。"""
    text = netcheck._friendly_browser_error(
        RuntimeError("Failed to decrypt with DPAPI. See https://github.com/yt-dlp/yt-dlp/issues/10927"))
    assert "cookies.txt" in text or "导出" in text
