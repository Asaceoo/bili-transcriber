"""发布脚本的文档快照逻辑(版本头部自动同步)。"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_release():
    spec = importlib.util.spec_from_file_location("_release_mod",
                                                  ROOT / "scripts" / "release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sync_header_bumps_version_and_snapshot_link(tmp_path, monkeypatch):
    rel = _load_release()
    doc = tmp_path / "user-guide-zh.md"
    doc.write_text(
        "# 手册\n\n> 本手册对应 **v0.1.38**。带版本号的发布版手册见 "
        "[user-guide-zh-0.1.38.md](user-guide-zh-0.1.38.md)。\n\n正文\n",
        encoding="utf-8")
    assert rel._sync_doc_header(doc, "0.1.39") is True
    text = doc.read_text(encoding="utf-8")
    assert "**v0.1.39**" in text
    assert "user-guide-zh-0.1.39.md" in text
    assert "v0.1.38" not in text


def test_sync_header_handles_link_on_next_line(tmp_path):
    """声明与快照链接不同行时,两处都要同步(真实手册的写法就是分两行)。"""
    rel = _load_release()
    doc = tmp_path / "user-guide-zh.md"
    doc.write_text(
        "# 手册\n\n"
        "> 本手册对应 **v0.1.38** — 版本号取自 `pyproject.toml`。\n"
        "> 带版本号的发布快照见 [user-guide-zh-0.1.38.md](user-guide-zh-0.1.38.md)。\n\n"
        "正文\n",
        encoding="utf-8")
    assert rel._sync_doc_header(doc, "0.1.39") is True
    text = doc.read_text(encoding="utf-8")
    assert "**v0.1.39**" in text
    # 链接与显示文本都要换,不能只换其中一个
    assert "[user-guide-zh-0.1.39.md](user-guide-zh-0.1.39.md)" in text
    assert "0.1.38" not in text


def test_sync_header_missing_marker_returns_false(tmp_path):
    rel = _load_release()
    doc = tmp_path / "en.md"
    doc.write_text("# Guide\n\nNo version line here.\n", encoding="utf-8")
    assert rel._sync_doc_header(doc, "9.9.9") is False


def test_sync_readme_header_touches_real_readme_contract(tmp_path, monkeypatch):
    """README 只有滚动版,发布时必须同步头部声明(否则版本号落后于源码)。"""
    rel = _load_release()
    readme = tmp_path / "README.md"
    readme.write_text(
        "# bili-transcriber\n\n"
        "> 本 README 对应 **v0.1.38** — 版本号取自 `pyproject.toml` 单一来源。\n\n"
        "正文\n",
        encoding="utf-8")
    monkeypatch.setattr(rel, "ROOT", tmp_path)
    rel.sync_readme_header("0.1.39")
    text = readme.read_text(encoding="utf-8")
    assert "**v0.1.39**" in text
    # 正文不能被改动
    assert text.rstrip().endswith("正文")


def test_snapshot_drops_self_referential_link_line(tmp_path, monkeypatch):
    """快照里不该保留「带版本号的发布快照见 [自己]」这种自引用行。"""
    rel = _load_release()
    monkeypatch.setattr(rel, "ROOT", tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "user-guide-zh.md").write_text(
        "# 手册\n\n"
        "> 本手册对应 **v0.1.38** — 版本号取自 `pyproject.toml`。\n"
        "> 带版本号的发布快照见 [user-guide-zh-0.1.38.md](user-guide-zh-0.1.38.md)。\n\n"
        "正文\n",
        encoding="utf-8")
    monkeypatch.setattr(rel, "DOC_SNAPSHOTS",
                        {"user-guide-zh.md": "user-guide-zh-{ver}.md"})
    rel.snapshot_docs("0.1.39")
    snap = (docs / "user-guide-zh-0.1.39.md").read_text(encoding="utf-8")
    assert "发布版本快照 **v0.1.39**" in snap
    assert "带版本号的发布快照见" not in snap, "快照里残留自引用行"
    assert "正文" in snap


def test_real_readme_has_syncable_header():
    """仓库里的 README 头部必须能被 _sync_doc_header 识别,否则发布时静默跳过。"""
    rel = _load_release()
    readme = ROOT / "README.md"
    assert readme.is_file()
    head = readme.read_text(encoding="utf-8").splitlines()[:20]
    hit = [ln for ln in head
           if ("对应" in ln or "matches" in ln)
           and __import__("re").search(r"\*\*v\d+\.\d+\.\d+\*\*", ln)]
    assert hit, "README 前 20 行缺少可同步的版本声明行"
