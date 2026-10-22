"""UI 动作辅助函数:打开文件夹/定位产物/任务操作/删除/上传落盘/设置重置。

纯逻辑与系统集成,不含页面布局;UI 名字(如 _launch_explorer)可被测试
monkeypatch(app.ui.helpers.*)替身替换。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from nicegui import ui

from app.store import DEFAULT_SETTINGS, Job
from app.ui.state import BASE_DIR, pipeline, settings

logger = logging.getLogger(__name__)


def _notify(message: str, **kwargs) -> None:
    """安全通知:无 NiceGUI slot 上下文(托盘线程/纯逻辑测试)时降级为日志。

    ui.notify 要求当前任务存在 UI slot;从非 UI 上下文调用会抛
    RuntimeError("slot stack ... is empty")。本函数兜底保证动作函数
    (_open_path/_rerun/删除确认等)在任何线程都不因通知失败而中断。
    """
    try:
        ui.notify(message, **kwargs)
    except Exception:  # noqa: BLE001 — 通知失败只影响提示,不影响动作本身
        logger.info("notify(无UI上下文): %s", message)


def _shell_select(path: str) -> bool:
    """用 Windows Shell API(SHOpenFolderAndSelectItems)在资源管理器中定位选中文件。

    必须用它而不是 `explorer /select,"path"`:subprocess 列表参数会把参数内的
    引号按 MSVCRT 规则转义成 \",explorer 解析不出路径,转而打开桌面(默认视图)
    ——这正是历史任务「打开所在位置」总跳到桌面的根因。
    """
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD,
        ]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

        ole32.CoInitialize(None)  # 已初始化的线程返回 S_FALSE,无害
        pidl = shell32.ILCreateFromPathW(str(path))
        if not pidl:
            return False
        try:
            hr = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
            return hr == 0
        finally:
            ole32.CoTaskMemFree(pidl)
    except Exception:
        return False


def _launch_explorer(target: Path, select: bool = False) -> None:
    """在资源管理器中打开 target(文件时 select=True 表示定位选中)。"""
    try:
        if os.name == "nt":
            if select:
                # 路径含双引号 " 时 Shell API 无法定位(NTFS 允许引号字符),降级打开所在目录。
                p = str(target.resolve())
                if '"' in p:
                    os.startfile(str(target.parent.resolve()))
                elif not _shell_select(p):
                    # API 失败(极少见)时降级:字符串命令不经 list2cmdline 转义,
                    # explorer 仍能按原生语法解析 /select,"path"。
                    subprocess.Popen(f'explorer /select,"{p}"')
            else:
                os.startfile(str(target.resolve()))
        elif sys.platform == "darwin":
            cmd = ["open"]
            if select:
                cmd.append("-R")
            cmd.append(str(target))
            subprocess.Popen(cmd)
        else:
            subprocess.Popen(["xdg-open", str(target.parent if select else target)])
    except Exception as exc:
        _notify(f"打开失败:{exc}", type="negative")


def _first_arg(arg):
    """NiceGUI 自定义 $emit 事件参数以数组形式到达服务器(e.args=[value])。

    QTable 插槽里 `$emit('open-path', path)` 经默认桥接后,Python 侧收到的
    `e.args` 是单元素列表 [path] 而非 path 本身;直接传给 _open_path 会让
    Path([path]) 抛 TypeError,表现为"点了没反应"。这里统一解包。
    """
    if isinstance(arg, (list, tuple)) and len(arg) == 1:
        return arg[0]
    return arg


def _open_path(path: str) -> None:
    """打开文件所在目录并选中文件;文件缺失时降级打开其目录或输出目录。

    历史任务的 md_path 可能因 output_dir 变更/文件被移走而失效,
    此时不能只提示"文件不存在"——应尽量打开还能访问的目录。
    """
    path = _first_arg(path)  # 防御:事件参数可能以单元素列表到达
    target = Path(path) if path else None
    if target is not None and target.exists():
        _launch_explorer(target, select=True)
        return
    # 文件不存在:尝试打开其所在目录(可能还有 srt/txt/音频等产物)
    parent = target.parent if target is not None else None
    if parent is not None and parent.is_dir():
        _notify("原文件已不存在,已打开其所在目录", type="warning")
        _launch_explorer(parent, select=False)
        return
    _notify("文件及其目录均不存在", type="warning")
    # 最后兜底:打开输出目录
    out = settings.resolve_output_dir(BASE_DIR)
    if out.is_dir():
        _launch_explorer(out, select=False)


def _stage_text(status: str) -> str:
    return {
        "queued": "排队等待中…",
        "downloading": "正在下载音频…",
        "converting": "FFmpeg 转码中…",
        "transcribing": "AI 转写中,请稍候…",
        "extracting": "正在提取关键帧…",
        "saving": "正在保存字幕…",
        "done": "已完成",
        "failed": "失败",
        "paused": "已暂停",
        "cancelled": "已取消",
    }.get(status, status)


def _job_reveal_path(job: Job) -> str:
    """“打开所在位置”智能定位:
    - 已完成任务:优先输出产物(.md/.srt/.txt),定位到笔记所在目录;
    - 未完成/失败任务:优先源文件(本地上传视频/下载音频);
    - 存储路径全部失效时,按文件名在输出/上传目录搜索实际文件。
    """
    if job.status == "done":
        candidates = (job.md_path, job.srt_path, job.txt_path, job.audio_path)
    else:
        candidates = (job.audio_path, job.md_path, job.srt_path, job.txt_path)
    for cand in candidates:
        if cand and Path(cand).exists():
            return str(Path(cand).resolve())
    found = _search_job_file(job)
    if found is not None:
        return str(found.resolve())
    return job.audio_path or job.md_path or ""  # 兜底取值,交给打开逻辑做目录回退


def _search_job_file(job: Job) -> Path | None:
    """存储路径失效时,按文件名在已知目录(输出/上传/原路径父目录)搜索实际文件。

    返回找到的第一个文件;找不到返回 None。仅作为“打开所在位置”的兜底,
    避免历史任务因 output_dir 变更/文件被移走而无法定位。
    """
    stems: set[str] = set()
    for p in (job.md_path, job.audio_path):
        if p:
            s = Path(p).stem
            if s:
                stems.add(s)
    if job.title:
        stems.add(job.title)
    stems = {s for s in stems if s}
    if not stems:
        return None
    dirs: list[Path] = []
    for p in (job.md_path, job.audio_path):
        if p:
            d = Path(p).parent
            if d.is_dir():
                dirs.append(d)
    out = settings.resolve_output_dir(BASE_DIR)
    if out.is_dir():
        dirs.append(out)
    uploads = BASE_DIR / "uploads"
    if uploads.is_dir():
        dirs.append(uploads)
    seen: set[str] = set()
    for d in dirs:
        key = str(d.resolve())
        if key in seen:
            continue
        seen.add(key)
        try:
            for f in d.rglob("*"):
                if not f.is_file():
                    continue
                stem = f.stem
                # 匹配规则 = 精确 stem 或 BV 号前缀。
                # 不能用"标题是文件名的子串"做模糊匹配:短标题(如 "t")会误匹配
                # 输出目录里的无关文件(Tableau2023(64bit).zip 等),导致
                # "打开所在位置"打开错误文件。
                if stem in stems or (job.bv and stem.startswith(job.bv)):
                    return f
        except OSError:
            continue
    return None


def _job_row(job: Job) -> dict:
    from app.ui.state import ACTIVE_STATUSES, STATUS_LABEL

    pct = int(job.progress * 100) if job.status in ACTIVE_STATUSES or job.status == "done" else 0
    return {
        "media_id": job.media_id,
        "title": job.title + (f"(P{job.part}/{job.total_parts})" if job.total_parts > 1 else ""),
        "uploader": job.uploader,
        "duration": _fmt_dur(job.duration),
        "status": STATUS_LABEL.get(job.status, job.status),
        "raw_status": job.status,
        "progress": pct,
        "stage_text": _stage_text(job.status),
        "error": job.error,
        "md_path": _job_reveal_path(job),
    }


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _rerun(media_id: str) -> None:
    media_id = _first_arg(media_id)  # 防御:事件参数可能以单元素列表到达
    pipeline.rerun(media_id)
    _notify("已重新入队")


def _pause(media_id: str) -> None:
    pipeline.pause(_first_arg(media_id))
    _notify("已暂停")


def _resume(media_id: str) -> None:
    pipeline.resume(_first_arg(media_id))
    _notify("已恢复")


def _cancel(media_id: str) -> None:
    pipeline.cancel(_first_arg(media_id))
    _notify("已取消")


def _delete_job_files(job: Job) -> int:
    """删除任务磁盘产物(srt/txt/md/讲义 + URL 任务的下载音频);本地任务的
    源文件是用户上传资产,永不删除。返回实际删除的文件数。"""
    paths = [job.srt_path, job.txt_path, job.md_path, job.notes_path]
    if job.source_type != "local":
        paths.append(job.audio_path)
    n = 0
    for p in paths:
        if not p:
            continue
        try:
            f = Path(p)
            if f.exists():
                f.unlink()
                n += 1
        except OSError:
            continue
    # 讲义截图目录:{标题}.notes.md 对应同级 {标题}_frames/
    if job.notes_path:
        notes = Path(job.notes_path)
        if notes.name.endswith(".notes.md"):
            frames_dir = notes.parent / f"{notes.name[: -len('.notes.md')]}_frames"
            shutil.rmtree(frames_dir, ignore_errors=True)
    return n


def _confirm_delete(media_id: str) -> None:
    """删除单条历史记录:弹确认框,可选同时删除磁盘产物。"""
    from app.ui.state import store

    media_id = _first_arg(media_id)
    job = store.get_job(media_id)
    if not job:
        store.delete_job(media_id)
        _notify("已删除")
        return
    with ui.dialog() as dialog, ui.card().classes("min-w-[400px]"):
        ui.label(f"删除记录「{job.title}」?").classes("font-medium")
        ui.label("仅删除历史记录,磁盘文件保留;删除后可重新提交链接再次转写。"
                 ).classes("text-xs text-grey-7")
        delete_files = ui.checkbox("同时删除磁盘产物(字幕/笔记/音频)", value=False)

        def _do() -> None:
            n_files = _delete_job_files(job) if delete_files.value else 0
            store.delete_job(job.media_id)
            dialog.close()
            msg = "已删除记录"
            if n_files:
                msg += f"及 {n_files} 个产物文件"
            _notify(msg, type="positive")

        with ui.row().classes("justify-end w-full"):
            ui.button("取消", on_click=dialog.close).props("flat")
            ui.button("删除", icon="delete", color="negative", on_click=_do)
    dialog.open()


def _confirm_clear_finished() -> None:
    """清空全部已结束记录(已完成/失败/已取消):弹确认框,可选删除产物。"""
    from app.ui.state import store

    with ui.dialog() as dialog, ui.card().classes("min-w-[400px]"):
        ui.label("清空全部已结束的记录?").classes("font-medium")
        ui.label("包括已完成、失败、已取消的记录;进行中/排队中的任务不受影响。"
                 ).classes("text-xs text-grey-7")
        delete_files = ui.checkbox("同时删除磁盘产物(字幕/笔记/音频)", value=False)

        def _do() -> None:
            n_files = 0
            if delete_files.value:
                for j in store.list_jobs():
                    if j.status in ("done", "failed", "cancelled"):
                        n_files += _delete_job_files(j)
            n = store.clear_finished()
            dialog.close()
            msg = f"已清空 {n} 条记录"
            msg += f"及 {n_files} 个产物文件" if n_files else "(磁盘产物保留)"
            _notify(msg, type="positive")

        with ui.row().classes("justify-end w-full"):
            ui.button("取消", on_click=dialog.close).props("flat")
            ui.button("清空", icon="delete_sweep", color="negative", on_click=_do)
    dialog.open()


async def _on_upload(e) -> None:
    """NiceGUI 3.x 上传回调。

    on_upload 的入参是 UploadEventArguments,其 .file 才是 FileUpload 对象。
    FileUpload 没有顶层 .content 属性,内容需通过 file.save(path) 异步落盘。
    每个文件单独触发一次该回调(multiple=True 时逐个调用)。"""
    f = e.file
    # 防御路径穿越:客户端文件名可能含 ../ 或绝对路径片段,只取 basename 并剥离
    # 非法字符,确保始终落在 uploads_dir 内,不会写到项目外部。
    name = Path(f.name).name or "upload"
    if name in (".", "..") or not name.strip("."):
        name = "upload"
    bad = '<>:"/\\|?*'
    name = "".join("_" if ch in bad else ch for ch in name).strip().rstrip(".")
    if not name:
        name = "upload"
    dest = pipeline.uploads_dir / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        from datetime import datetime as _dt
        dest = dest.parent / f"{dest.stem}_{_dt.now().strftime('%H%M%S')}{dest.suffix}"
    await f.save(dest)          # 异步落盘:SmallFileUpload 写内存字节,LargeFileUpload 复制临时文件
    pipeline.submit_local(dest)
    _notify(f"已上传并加入队列:{name}")


def _open_dir(path: Path) -> None:
    """在资源管理器中打开目录(不存在则先创建)。优先 os.startfile(Windows 最可靠)。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _notify(f"无法创建目录:{exc}", type="negative")
        return
    try:
        if os.name == "nt":
            os.startfile(str(path.resolve()))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        _notify(f"打开失败:{exc}", type="negative")


def _reset_settings(out_input, model_dir_input=None, refresh_model_status=None) -> None:
    for k, v in DEFAULT_SETTINGS.items():
        setattr(settings, k, v)
    out_input.set_value(settings.output_dir)
    if model_dir_input is not None:
        model_dir_input.set_value(settings.model_dir)
    if refresh_model_status is not None:
        refresh_model_status()
    _notify("已恢复默认值,记得点保存")
