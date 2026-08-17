"""NiceGUI 桌面入口:任务队列 / 历史 / 设置 三个页签。"""

from __future__ import annotations

import os
import subprocess
from collections import deque
from pathlib import Path

from nicegui import ui

from app import converter
from app.downloader import Downloader
from app.pipeline import Pipeline
from app.store import DEFAULT_SETTINGS, Job, Settings, Store, load_settings, save_settings

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

STATUS_LABEL = {
    "queued": "排队中", "downloading": "下载中", "converting": "转码中",
    "transcribing": "转写中", "saving": "保存中", "done": "已完成", "failed": "失败",
}
ACTIVE_STATUSES = {"queued", "downloading", "converting", "transcribing", "saving"}
MODEL_OPTIONS = ["large-v3-turbo", "large-v3", "medium", "small", "base", "tiny"]

store = Store(DATA_DIR / "history.db")
settings = load_settings(DATA_DIR / "settings.json")
events: deque[str] = deque(maxlen=200)
pipeline = Pipeline(
    downloader=Downloader(), store=store, settings=settings,
    base_dir=BASE_DIR, on_event=events.append,
)


def _open_path(path: str) -> None:
    """在资源管理器中打开文件所在目录并选中文件。"""
    if not path or not Path(path).exists():
        ui.notify("文件不存在", type="warning")
        return
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
        else:
            os.startfile(path) if hasattr(os, "startfile") else subprocess.Popen(["xdg-open", path])  # type: ignore[misc]
    except OSError as exc:
        ui.notify(f"打开失败:{exc}", type="negative")


def _job_row(job: Job) -> dict:
    pct = int(job.progress * 100) if job.status in ACTIVE_STATUSES or job.status == "done" else 0
    return {
        "media_id": job.media_id,
        "title": job.title + (f"(P{job.part}/{job.total_parts})" if job.total_parts > 1 else ""),
        "uploader": job.uploader,
        "duration": _fmt_dur(job.duration),
        "status": STATUS_LABEL.get(job.status, job.status),
        "raw_status": job.status,
        "progress": pct,
        "error": job.error,
        "md_path": job.md_path,
    }


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


@ui.page("/")
def main_page() -> None:
    ui.colors(primary="#fb7299")  # B 站粉

    with ui.header().classes("items-center justify-between"):
        ui.label("B站音频本地转写").classes("text-lg font-bold")
        queue_label = ui.label("待处理:0")

    if not converter.ffmpeg_available():
        with ui.banner().props("rounded").classes("bg-orange-100 text-orange-800 w-full"):
            ui.label("未检测到 FFmpeg,请安装后加入 PATH,否则无法转码。")

    with ui.tabs().classes("w-full") as tabs:
        tab_tasks = ui.tab("任务")
        tab_history = ui.tab("历史")
        tab_settings = ui.tab("设置")

    with ui.tab_panels(tabs, value=tab_tasks).classes("w-full"):
        # ---------- 任务页 ----------
        with ui.tab_panel(tab_tasks):
            with ui.row().classes("w-full items-center gap-2"):
                url_input = ui.input(placeholder="粘贴 B 站视频/合集链接…").classes("flex-grow")

                def submit() -> None:
                    url = (url_input.value or "").strip()
                    if not url:
                        ui.notify("请输入链接", type="warning")
                        return
                    pipeline.submit(url)
                    url_input.set_value("")
                    ui.notify("已加入队列,正在解析…")

                ui.button("添加任务", icon="add", on_click=submit)

            log = ui.log(max_lines=100).classes("w-full h-40")

            ui.label("进行中的任务").classes("font-medium mt-2")
            active_table = ui.table(
                columns=[
                    {"name": "title", "label": "标题", "field": "title", "align": "left"},
                    {"name": "uploader", "label": "UP 主", "field": "uploader", "align": "left"},
                    {"name": "duration", "label": "时长", "field": "duration"},
                    {"name": "status", "label": "状态", "field": "status"},
                    {"name": "progress", "label": "进度", "field": "progress"},
                ],
                rows=[],
            ).classes("w-full")

        # ---------- 历史页 ----------
        with ui.tab_panel(tab_history):
            search = ui.input(placeholder="搜索标题 / UP 主 / BV 号").classes("w-80")

            ui.label("全部记录").classes("font-medium mt-2")
            history_table = ui.table(
                columns=[
                    {"name": "title", "label": "标题", "field": "title", "align": "left"},
                    {"name": "uploader", "label": "UP 主", "field": "uploader", "align": "left"},
                    {"name": "duration", "label": "时长", "field": "duration"},
                    {"name": "status", "label": "状态", "field": "status"},
                    {"name": "error", "label": "错误", "field": "error", "align": "left"},
                    {"name": "actions", "label": "操作", "field": "actions"},
                ],
                rows=[],
            ).classes("w-full")

            with history_table.add_slot("body-cell-actions"):
                with ui.button(icon="folder_open", color="primary").props(
                        "flat dense size=sm").on("click", lambda e: _open_path(e.args[1]["md_path"])) as btn1:
                    ui.tooltip("打开所在文件夹")
                with ui.button(icon="replay", color="primary").props(
                        "flat dense").on("click", lambda e: _rerun(e.args[1]["media_id"])) as btn2:
                    ui.tooltip("重新转写")

            def refresh_history() -> None:
                history_table.rows = [_job_row(j) for j in store.list_jobs(search.value or "")]
                history_table.update()

        # ---------- 设置页 ----------
        with ui.tab_panel(tab_settings):
            with ui.column().classes("max-w-lg gap-2"):
                out_input = ui.input("输出目录(相对项目根或绝对路径)").bind_value(
                    settings, "output_dir").classes("w-full")
                with ui.row().classes("items-center gap-4"):
                    ui.select(MODEL_OPTIONS, label="转写模型", value=settings.model_size).bind_value(
                        settings, "model_size")
                    ui.select({"auto": "自动", "cuda": "GPU", "cpu": "CPU"}, label="设备",
                              value=settings.device).bind_value(settings, "device")
                with ui.row().classes("items-center gap-4"):
                    ui.select(["auto", "float16", "int8_float16", "int8"], label="计算精度",
                              value=settings.compute_type).bind_value(settings, "compute_type")
                    ui.select({"": "自动检测", "zh": "中文", "en": "English"}, label="语言",
                              value=settings.language).bind_value(settings, "language")
                ui.switch("保留原始音频", value=settings.keep_audio).bind_value(settings, "keep_audio")
                ui.switch("VAD 人声过滤(静音不转写)", value=settings.vad).bind_value(settings, "vad")

                def save() -> None:
                    save_settings(settings, DATA_DIR / "settings.json")
                    pipeline.apply_settings(settings)
                    ui.notify("设置已保存(模型变更在下个任务生效)", type="positive")

                ui.button("保存设置", icon="save", on_click=save).classes("mt-2")
                ui.button("恢复默认", on_click=lambda: _reset_settings(out_input)).props("flat")

    # ---------- 定时刷新(工作线程只写 Store/deque,UI 线程轮询) ----------
    def refresh() -> None:
        while events:
            log.push(events.popleft())
        queue_label.set_text(f"待处理:{pipeline.pending_count}")
        rows = [_job_row(j) for j in store.list_jobs()]
        active_table.rows = [r for r in rows if r["raw_status"] in ACTIVE_STATUSES]
        active_table.update()
        history_table.rows = [r for r in rows if r["raw_status"] not in ACTIVE_STATUSES]
        history_table.update()

    ui.timer(0.6, refresh)


def _rerun(media_id: str) -> None:
    pipeline.rerun(media_id)
    ui.notify("已重新入队")


def _reset_settings(out_input) -> None:
    for k, v in DEFAULT_SETTINGS.items():
        setattr(settings, k, v)
    out_input.set_value(settings.output_dir)
    ui.notify("已恢复默认值,记得点保存")


def main() -> None:
    ui.run(
        title="B站音频本地转写",
        native=True,
        window_size=(980, 720),
        port=8765,
        reload=False,
        show=False,
        favicon="🅑",
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
