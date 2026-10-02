"""main.py 事件/打开文件夹辅助函数测试。

覆盖本轮修复:
- NiceGUI 自定义 $emit 事件参数以单元素列表到达服务器(e.args=[value]),
  _first_arg 统一解包,防止 Path([path]) 抛 TypeError("打开文件夹点了没反应");
- 「打开所在位置」优先走 Windows Shell API(SHOpenFolderAndSelectItems)定位文件,
  规避 subprocess 列表参数转义引号导致 explorer 打开桌面;API 失败才降级
  explorer /select,"path" 字符串命令;路径含引号字符时降级打开所在目录。
"""

from unittest.mock import patch

import pytest
from pathlib import Path

from app.main import _first_arg, _job_reveal_path, _launch_explorer, _open_path, _rerun
from app.store import Job


# ---------- _job_reveal_path(打开所在位置的目标路径解析) ----------

def _mk(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    return p


def test_job_reveal_prefers_source_video_over_md(tmp_path):
    """未完成本地任务:源视频(uploads)优先于输出 md——定位到源文件所在目录。"""
    src = _mk(tmp_path / "uploads" / "demo.mp4")
    md = _mk(tmp_path / "output" / "demo.md")
    job = Job(
        media_id="local_1", bv="", title="demo", uploader="本地文件",
        duration=10.0, url="", source_type="local", status="queued",
        audio_path=str(src), md_path=str(md),
    )
    assert _job_reveal_path(job) == str(src.resolve())


def test_job_reveal_done_prefers_md_over_source(tmp_path):
    """已完成任务:输出笔记(.md)优先于源视频——定位到笔记所在目录。"""
    src = _mk(tmp_path / "uploads" / "demo.mp4")
    md = _mk(tmp_path / "output" / "demo.md")
    job = Job(
        media_id="local_1", bv="", title="demo", uploader="本地文件",
        duration=10.0, url="", source_type="local", status="done",
        audio_path=str(src), md_path=str(md),
    )
    assert _job_reveal_path(job) == str(md.resolve())


def test_job_reveal_falls_back_to_existing_product_when_source_gone(tmp_path):
    """源文件被删时,回退到仍存在的输出产物(md/srt/txt 中最先存在者)。"""
    gone = tmp_path / "uploads" / "demo.mp4"          # 不落盘
    md = _mk(tmp_path / "output" / "demo.md")
    job = Job(
        media_id="local_1", bv="", title="demo", uploader="本地文件",
        duration=10.0, url="", source_type="local", status="queued",
        audio_path=str(gone), md_path=str(md),
    )
    assert _job_reveal_path(job) == str(md.resolve())


def test_job_reveal_searches_actual_file_when_paths_stale(tmp_path, monkeypatch):
    """存储路径全部失效时,按文件名在输出目录搜索到实际产物(.md)。"""
    # 模拟 output_dir 变更:实际产物在 tmp_path/out 下,而任务记录指向旧目录
    actual = _mk(tmp_path / "out" / "BV1X_标题" / "标题.md")
    job = Job(
        media_id="BV1X", bv="BV1X", title="标题", uploader="u",
        duration=10.0, url="https://x", source_type="url", status="done",
        audio_path=str(tmp_path / "old" / "BV1X_标题" / "标题.m4a"),
        md_path=str(tmp_path / "old" / "BV1X_标题" / "标题.md"),
    )
    monkeypatch.setattr("app.main.settings", type("S", (), {
        "resolve_output_dir": lambda self, base: tmp_path / "out",
    })())
    assert _job_reveal_path(job) == str(actual.resolve())


def test_job_reveal_empty_when_no_path(tmp_path):
    """无任何源/产物(如早期取消的 URL 任务)时不抛错,返回空串。"""
    job = Job(
        media_id="u1", bv="bv1", title="t", uploader="u",
        duration=0.0, url="https://x", source_type="url",
        audio_path="", md_path="", srt_path="", txt_path="",
    )
    assert _job_reveal_path(job) == ""


def test_job_reveal_failed_url_task_has_no_path(tmp_path):
    """aired:解析/下载失败的 URL 任务无源文件无产物 -> 空(按钮禁用,不乱开)。"""
    job = Job(
        media_id="u2", bv="bv2", title="t", uploader="u",
        duration=0.0, url="https://x", source_type="url",
        status="failed", audio_path="", md_path="", srt_path="", txt_path="",
    )
    # 不指向不存在到路径,避免 _open_path 乱弹;返回空由 UI 禁用按钮
    assert _job_reveal_path(job) == ""


# ---------- _first_arg(事件参数解包) ----------

def test_first_arg_unwraps_single_element_list():
    assert _first_arg(["D:\\x.md"]) == "D:\\x.md"
    assert _first_arg(("D:\\x.md",)) == "D:\\x.md"


def test_first_arg_passthrough():
    assert _first_arg("D:\\x.md") == "D:\\x.md"
    assert _first_arg(None) is None
    assert _first_arg([1, 2]) == [1, 2]   # 多元素列表不解包
    assert _first_arg([]) == []


# ---------- _open_path(文件打开,含列表参数防御) ----------

def test_open_path_with_list_arg_selects(tmp_path, monkeypatch):
    f = tmp_path / "a.md"
    f.write_text("x")
    calls = []
    monkeypatch.setattr("app.main._launch_explorer", lambda t, select=False: calls.append((str(t), select)))
    _open_path([str(f)])   # 模拟 $emit 到达服务器的 e.args=[path]
    assert calls == [(str(f), True)]


def test_open_path_missing_falls_back_to_parent_dir(tmp_path, monkeypatch):
    missing = tmp_path / "gone.md"
    calls = []
    monkeypatch.setattr("app.main._launch_explorer", lambda t, select=False: calls.append((str(t), select)))
    _open_path(str(missing))
    assert calls and calls[0][0] == str(tmp_path) and calls[0][1] is False


def test_open_path_empty_does_not_crash(monkeypatch):
    calls = []
    monkeypatch.setattr("app.main._launch_explorer", lambda t, select=False: calls.append((str(t), select)))
    _open_path([""])   # md_path 为空(任务未完成)不应抛异常
    # 空路径:文件/目录都不存在 → 尝试兜底输出目录;不崩即可
    assert isinstance(calls, list)


# ---------- _launch_explorer(资源管理器定位) ----------

def test_launch_explorer_uses_shell_api_first(tmp_path, monkeypatch):
    """「打开所在位置」优先走 Shell API 定位:路径不经命令行转义,
    直接以原生字符串传给 SHOpenFolderAndSelectItems,不弹桌面。"""
    f = tmp_path / "普通文件.md"
    f.write_text("x")
    select_calls = []
    monkeypatch.setattr("app.main._shell_select", lambda p: select_calls.append(p) or True)
    popen_calls = []
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **kw: popen_calls.append(cmd))
    _launch_explorer(f, select=True)
    assert select_calls == [str(f.resolve())]
    assert popen_calls == []   # API 成功,无需降级 explorer 命令


def test_launch_explorer_space_path_quoted(tmp_path, monkeypatch):
    """Shell API 失败降级 explorer /select,时,含空格路径必须整体引号包裹,
    且以字符串命令传递(列表参数会把引号转义成 \",导致 explorer 打开桌面)。"""
    f = tmp_path / "含 空格 文件.md"
    f.write_text("x")
    popen_calls = []
    monkeypatch.setattr("app.main._shell_select", lambda p: False)
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **kw: popen_calls.append(cmd))
    _launch_explorer(f, select=True)
    assert len(popen_calls) == 1
    assert popen_calls[0] == f'explorer /select,"{f.resolve()}"'


def test_launch_explorer_quote_char_falls_back_to_parent(monkeypatch):
    """路径字符串含 ASCII 双引号(仅防御分支;合法 NTFS 路径不可能含半角引号)
    时 Shell API 无法定位,应降级打开所在目录,不抛异常。"""
    f = Path(r'C:\tmp\含"引号"文件.md')   # 直接构造,不落盘
    startfile_calls = []
    monkeypatch.setattr("os.startfile", lambda p: startfile_calls.append(p))
    _launch_explorer(f, select=True)
    assert len(startfile_calls) == 1
    assert startfile_calls[0] == str(Path(r'C:\tmp').resolve())


# ---------- _rerun(列表参数防御) ----------

def test_rerun_unwraps_list(monkeypatch):
    rerun_calls = []
    monkeypatch.setattr("app.main.pipeline", type("P", (), {"rerun": lambda self, mid: rerun_calls.append(mid)})())
    _rerun(["BV12345"])
    assert rerun_calls == ["BV12345"]


# ---------- 版本一致性(APP_VERSION 是设置页"关于与帮助"展示的唯一版本来源) ----------

def test_app_version_matches_pyproject():
    import re

    from app import APP_VERSION

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    m = re.search(r'^version\s*=\s*"(\d+\.\d+\.\d+)"',
                  pyproject.read_text(encoding="utf-8"), re.M)
    assert m, "pyproject.toml 缺 version"
    assert APP_VERSION == m.group(1), (
        f"APP_VERSION={APP_VERSION} 与 pyproject version={m.group(1)} 不一致,"
        "请通过 scripts/release.py 的 bump 流程或手动同步 app/__init__.py"
    )
