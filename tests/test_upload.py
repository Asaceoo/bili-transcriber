"""上传本地文件通道的 UI 回调回归测试。

NiceGUI 3.x 的 on_upload 入参是 UploadEventArguments,其 .file 才是 FileUpload 对象,
内容需通过 file.save(path) 异步落盘(无顶层 .content 属性)。本测试锁定该契约,
防止再次出现 '上传视频不认'(取到空 content 被静默跳过) 的回退。
"""
import asyncio
from pathlib import Path

from nicegui.elements.upload import UploadEventArguments
from nicegui.elements.upload_files import SmallFileUpload


def test_upload_callback_saves_and_enqueues(tmp_path, monkeypatch):
    import app.main as m

    monkeypatch.setattr(m.ui, "notify", lambda *a, **k: None)  # 测试无 UI 上下文,屏蔽通知
    monkeypatch.setattr(m.pipeline, "uploads_dir", tmp_path)
    captured = {}

    def fake_submit_local(p):
        captured["path"] = Path(p)
        return "local:fake"

    monkeypatch.setattr(m.pipeline, "submit_local", fake_submit_local)

    data = b"X" * 2048
    fu = SmallFileUpload(name="测试视频.mp4", content_type="video/mp4", _data=data)
    ev = UploadEventArguments(sender=None, client=None, file=fu)
    asyncio.run(m._on_upload(ev))

    dest = tmp_path / "测试视频.mp4"
    assert dest.exists(), "上传文件未落盘到 uploads 目录"
    assert dest.read_bytes() == data, "落盘内容不完整"
    assert captured.get("path") == dest, "submit_local 未被调用或路径不一致"
