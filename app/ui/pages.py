"""NiceGUI 页面构建:任务/历史/运行日记/设置 四页签(原 main.py 的 UI 部分)。"""

from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path

from nicegui import run, ui

from app import APP_VERSION, converter, native_dialog, netcheck
from app.gpu_runtime import default_gpu_dir
from app.logging_setup import log_dir
from app.models import resolve_model_dir
from app.store import save_settings
from app.ui.helpers import (
    _cancel, _confirm_clear_finished, _confirm_delete, _first_arg, _job_row,
    _on_upload, _open_dir, _open_path, _pause, _reset_settings, _resume, _rerun,
)
from app.ui.state import (
    ACTIVE_STATUSES, BASE_DIR, DATA_DIR, MODEL_OPTIONS, diary_state, diary_lines,
    events, hw_results, pipeline, settings, store,
)

logger = logging.getLogger(__name__)

# 设置页「网络与 Cookie」的交互动效:检测结果淡入上浮、状态徽章弹入、
# 检测中按钮呼吸、等待文案闪烁。统一放这里,避免在页面函数里堆长字符串。
_NETWORK_CSS = """
<style>
@keyframes bili-fade-up { from { opacity:0; transform: translateY(6px); } to { opacity:1; transform:none; } }
@keyframes bili-pop { 0% { transform: scale(.92); opacity:0 } 70% { transform: scale(1.04) } 100% { transform: scale(1); opacity:1 } }
@keyframes bili-pulse { 0% { box-shadow:0 0 0 0 rgba(251,114,153,.45) } 70% { box-shadow:0 0 0 12px rgba(251,114,153,0) } 100% { box-shadow:0 0 0 0 rgba(251,114,153,0) } }
@keyframes bili-blink { 0%,100% { opacity:.45 } 50% { opacity:1 } }
@keyframes bili-spin { to { transform: rotate(360deg); } }
.bili-fade-up { animation: bili-fade-up .34s cubic-bezier(.22,.61,.36,1) both; }
.bili-pop { animation: bili-pop .3s cubic-bezier(.22,.61,.36,1) both; }
.bili-pulse { animation: bili-pulse 1.6s ease-out infinite; }
.bili-pulse .q-icon { animation: bili-spin 1.1s linear infinite; }
.bili-blink { animation: bili-blink 1.2s ease-in-out infinite; }
/* 未激活步骤:灰字 + 半透明,和激活态的粉色拉开距离(只调透明度时深色文字仍然显眼) */
.bili-step { color:#9aa4b2; opacity:.7;
  transition: opacity .25s ease, color .25s ease, transform .25s ease; }
.bili-step.active { opacity:1; color:#fb7299; font-weight:600; transform: translateY(-1px); }
.bili-chip { transition: transform .18s ease, box-shadow .18s ease; }
.bili-chip:hover { transform: translateY(-2px); box-shadow: 0 6px 14px rgba(0,0,0,.08); }
@media (prefers-reduced-motion: reduce) {
  .bili-fade-up, .bili-pop, .bili-pulse, .bili-blink { animation: none !important; }
}
</style>
"""

# 徽章配色:Tailwind 标准色阶(浅底 + 深字),深浅主题都不刺眼
_CHIP_KINDS = {
    "ok": ("bg-green-50", "text-green-700", "text-green-500"),
    "warn": ("bg-amber-50", "text-amber-800", "text-amber-500"),
    "bad": ("bg-red-50", "text-red-700", "text-red-400"),
    "info": ("bg-blue-50", "text-blue-700", "text-blue-500"),
    "muted": ("bg-gray-100", "text-gray-500", "text-gray-400"),
}

# 「测试连接」里 Cookie 状态 -> 徽章(种类, 图标, 文案)
_COOKIE_STATE_CHIP = {
    "verified": ("ok", "verified", "已登录(已校验)"),
    "present": ("info", "person", "已登录"),
    "expired": ("warn", "schedule", "Cookie 已过期"),
    "missing": ("muted", "remove_circle_outline", "无 Cookie"),
    "unreadable": ("bad", "lock", "读取失败"),
    "unknown": ("muted", "help_outline", "未验证"),
}


# ---------- QTable 插槽模板 ----------
_STATUS_BADGE = '''
<q-td :props="props">
  <q-badge
    :color="props.row.raw_status === 'done' ? 'positive'
           : props.row.raw_status === 'failed' ? 'negative'
           : props.row.raw_status === 'cancelled' ? 'grey-6'
           : props.row.raw_status === 'paused' ? 'amber-8'
           : props.row.raw_status === 'transcribing' ? 'purple'
           : props.row.raw_status === 'downloading' ? 'blue'
           : props.row.raw_status === 'converting' ? 'teal'
           : props.row.raw_status === 'extracting' ? 'deep-purple'
           : props.row.raw_status === 'saving' ? 'orange'
           : 'grey'"
    :label="props.value"
    outline
  />
</q-td>
'''

_PROGRESS_BAR = '''
<q-td :props="props">
  <div style="display:flex;flex-direction:column;gap:4px;min-width:160px">
    <div style="display:flex;align-items:center;gap:8px">
      <q-linear-progress
        :value="props.row.raw_status === 'failed' ? 1.0 : props.value / 100"
        size="8px"
        rounded
        :stripe="props.row.raw_status !== 'done' && props.row.raw_status !== 'failed' && props.row.raw_status !== 'paused' && props.row.raw_status !== 'cancelled'"
        :animation-speed="props.row.raw_status === 'transcribing' || props.row.raw_status === 'downloading' ? 550 : 0"
        :color="props.row.raw_status === 'failed' ? 'negative'
               : props.row.raw_status === 'cancelled' ? 'grey-6'
               : props.row.raw_status === 'paused' ? 'amber-8'
               : props.row.raw_status === 'done' ? 'positive'
               : props.row.raw_status === 'transcribing' ? 'purple'
               : props.row.raw_status === 'downloading' ? 'blue'
               : props.row.raw_status === 'converting' ? 'teal'
               : props.row.raw_status === 'extracting' ? 'deep-purple'
               : 'primary'"
        style="flex:1"
      />
      <span style="font-size:0.85em;white-space:nowrap;min-width:40px;text-align:right">
        {{ props.row.raw_status === 'failed' ? '失败' : props.row.raw_status === 'cancelled' ? '已取消' : props.value + '%' }}
      </span>
    </div>
    <span v-if="props.row.stage_text" style="font-size:0.75em;color:#888;white-space:nowrap">
      {{ props.row.stage_text }}
    </span>
  </div>
</q-td>
'''

_ACTIONS_CELL = '''
<q-td :props="props">
  <div style="display:flex;gap:4px">
    <q-btn dense flat round icon="folder_open" size="sm" color="grey-7"
           @click="$parent.$emit('open-path', props.row.md_path)"
           :disable="!props.row.md_path"
           title="打开所在文件夹" />
    <q-btn v-if="props.row.raw_status === 'failed'" dense flat round icon="replay" size="sm" color="primary"
           @click="$parent.$emit('rerun', props.row.media_id)"
           title="重新转写" />
    <q-btn v-if="props.row.raw_status === 'transcribing' || props.row.raw_status === 'downloading' || props.row.raw_status === 'converting' || props.row.raw_status === 'extracting' || props.row.raw_status === 'saving'"
           dense flat round icon="pause" size="sm" color="amber-8"
           @click="$parent.$emit('pause', props.row.media_id)"
           title="暂停" />
    <q-btn v-if="props.row.raw_status === 'paused'"
           dense flat round icon="play_arrow" size="sm" color="positive"
           @click="$parent.$emit('resume', props.row.media_id)"
           title="继续" />
    <q-btn v-if="props.row.raw_status === 'paused' || props.row.raw_status === 'queued' || props.row.raw_status === 'transcribing' || props.row.raw_status === 'downloading' || props.row.raw_status === 'converting' || props.row.raw_status === 'extracting' || props.row.raw_status === 'saving'"
           dense flat round icon="close" size="sm" color="negative"
           @click="$parent.$emit('cancel', props.row.media_id)"
           title="取消" />
    <q-btn v-if="props.row.raw_status === 'done' || props.row.raw_status === 'failed' || props.row.raw_status === 'cancelled'"
           dense flat round icon="delete" size="sm" color="grey-7"
           @click="$parent.$emit('delete', props.row.media_id)"
           title="删除记录" />
  </div>
</q-td>
'''


@ui.page("/")
def main_page() -> None:
    ui.colors(primary="#fb7299")
    ui.add_head_html(_NETWORK_CSS)

    with ui.header().classes("items-center justify-between"):
        ui.label("视频音频本地转写").classes("text-lg font-bold")
        queue_label = ui.label("待处理:0")

    if not converter.ffmpeg_available():
        with ui.card().props("flat rounded").classes("bg-orange-100 text-orange-800 w-full q-pa-md"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("warning").classes("text-orange-800")
                ui.label("未检测到可用 FFmpeg:转码与讲义仍可运行(内置引擎),仅 B 站讲义视频流可能降为低清合一格式。")

    with ui.tabs().classes("w-full") as tabs:
        tab_tasks = ui.tab("任务")
        tab_history = ui.tab("历史")
        tab_diary = ui.tab("运行日记")
        tab_settings = ui.tab("设置")

    with ui.tab_panels(tabs, value=tab_tasks).classes("w-full"):
        # ---------- 任务页 ----------
        with ui.tab_panel(tab_tasks):
            with ui.card().classes("w-full"):
                with ui.row().classes("w-full items-center gap-2"):
                    url_input = ui.input(
                        placeholder="粘贴视频链接(B站/YouTube/抖音/小红书/快手)…"
                    ).classes("flex-grow")

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

            with ui.card().classes("w-full mt-4"):
                ui.label("本地文件转写(视频/音频)").classes("font-medium")

                async def _pick_local_files() -> None:
                    """系统文件框选择本机文件并直接提交原路径(零拷贝,不经浏览器上传)。"""
                    paths = await run.io_bound(native_dialog.pick_files)
                    if not paths:
                        return
                    for p in paths:
                        pipeline.submit_local(p)
                        ui.notify(f"已提交:{p.name}", type="positive")

                with ui.row().classes("items-center gap-2"):
                    ui.button("选择文件(推荐,立即转写)…", icon="folder_open",
                              on_click=_pick_local_files).props("color=primary")
                    ui.label("大文件请用这个:弹系统文件框后直接开始,不经浏览器,秒提交。"
                             ).classes("text-xs text-grey-6")

                ui.separator().classes("w-full")
                ui.label("浏览器上传(手机/其他电脑访问网页时用;大文件较慢)"
                             ).classes("text-sm text-grey-7")
                ui.upload(
                    label="选择文件(支持 mp4/mkv/mov/avi/webm/flv/wmv/mp3/m4a/wav/ogg/opus/flac/aac)",
                    on_upload=_on_upload,
                    on_rejected=lambda: ui.notify(
                        "文件被浏览器拒绝(可能大小超限或类型不支持),请检查文件后重试。",
                        type="negative",
                    ),
                    auto_upload=True,
                    multiple=True,
                    max_file_size=10 * 1024 * 1024 * 1024,  # 10 GB 上限,覆盖默认限制
                ).classes("w-full")

            ui.label("进行中的任务").classes("font-medium mt-4")
            active_table = ui.table(
                columns=[
                    {"name": "title", "label": "标题", "field": "title", "align": "left"},
                    {"name": "uploader", "label": "UP 主", "field": "uploader", "align": "left"},
                    {"name": "duration", "label": "时长", "field": "duration"},
                    {"name": "status", "label": "状态", "field": "status"},
                    {"name": "progress", "label": "进度", "field": "progress"},
                    {"name": "actions", "label": "操作", "field": "actions"},
                ],
                rows=[],
                row_key="media_id",
            ).classes("w-full")

            with active_table.add_slot("body-cell-status", template=_STATUS_BADGE):
                pass

            with active_table.add_slot("body-cell-progress", template=_PROGRESS_BAR):
                pass

            with active_table.add_slot("body-cell-actions", template=_ACTIONS_CELL):
                pass

            active_table.on("open-path", lambda e: _open_path(_first_arg(e.args)))
            active_table.on("rerun", lambda e: _rerun(_first_arg(e.args)))
            active_table.on("pause", lambda e: _pause(_first_arg(e.args)))
            active_table.on("resume", lambda e: _resume(_first_arg(e.args)))
            active_table.on("cancel", lambda e: _cancel(_first_arg(e.args)))
            active_table.on("delete", lambda e: _confirm_delete(_first_arg(e.args)))

        # ---------- 历史页 ----------
        with ui.tab_panel(tab_history):
            with ui.row().classes("w-full items-center gap-2"):
                search = ui.input(placeholder="搜索标题 / 作者 / 视频号").classes("w-80")
                ui.button("清空已结束记录", icon="delete_sweep",
                          on_click=_confirm_clear_finished).props("flat dense color=grey-7")

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
                row_key="media_id",
            ).classes("w-full")

            with history_table.add_slot("body-cell-status", template=_STATUS_BADGE):
                pass

            with history_table.add_slot("body-cell-actions", template=_ACTIONS_CELL):
                pass

            history_table.on("open-path", lambda e: _open_path(_first_arg(e.args)))
            history_table.on("rerun", lambda e: _rerun(_first_arg(e.args)))
            history_table.on("delete", lambda e: _confirm_delete(_first_arg(e.args)))
            # 搜索框即时生效:定时器 refresh() 会带 search.value 过滤历史表
            # (0.6s 内生效);这里额外挂 on_value_change 让输入回车/清空立即刷新。
            search.on_value_change(lambda: _refresh_history())

            def _refresh_history() -> None:
                history_table.rows = [_job_row(j) for j in store.list_jobs(search.value or "")]
                history_table.update()

        # ---------- 运行日记页 ----------
        with ui.tab_panel(tab_diary):
            ui.label("运行日记:记录软件每一步操作与错误,便于排查问题。").classes("text-sm text-grey-7")

            def refresh_diary() -> None:
                lines = diary_lines()
                total = max(1, (len(lines) + diary_state["page_size"] - 1) // diary_state["page_size"])
                diary_state["total_pages"] = total
                diary_state["page"] = min(diary_state["page"], total)
                start = (diary_state["page"] - 1) * diary_state["page_size"]
                page = lines[start:start + diary_state["page_size"]]
                diary_log.clear()
                for ln in page:
                    diary_log.push(ln)
                diary_page_label.set_text(f"第 {diary_state['page']} / {total} 页")

            def _diary_page(delta: int) -> None:
                diary_state["page"] = max(1, min(diary_state["page"] + delta, diary_state["total_pages"]))
                refresh_diary()

            with ui.row().classes("items-center gap-2 w-full"):
                ui.button("刷新", icon="refresh", on_click=refresh_diary)
                ui.button("打开日志目录", icon="folder_open", on_click=lambda: _open_dir(log_dir()))
                diary_size_select = ui.select([50, 100, 200, 500], label="每页行数", value=100)
                diary_size_select.bind_value(diary_state, "page_size")
                diary_size_select.on_value_change(lambda: refresh_diary())
                ui.button(icon="chevron_left", on_click=lambda: _diary_page(-1)).props("dense round")
                diary_page_label = ui.label("第 0 / 0 页")
                ui.button(icon="chevron_right", on_click=lambda: _diary_page(1)).props("dense round")
            diary_log = ui.log(max_lines=1000).classes("w-full h-96")

            refresh_diary()

        # ---------- 设置页 ----------
        with ui.tab_panel(tab_settings):
            with ui.card().classes("w-full max-w-3xl"):
                with ui.column().classes("gap-3"):
                    out_input = ui.input("输出目录(相对项目根或绝对路径)").bind_value(
                        settings, "output_dir").classes("w-full")

                    engine_select = ui.select(
                        {"whisper": "Whisper (默认)", "qwen3-asr": "Qwen3-ASR",
                         "sensevoice": "SenseVoice"},
                        label="识别引擎", value=settings.engine,
                    ).bind_value(settings, "engine")

                    QWEN_MODEL_OPTIONS = ["Qwen3-ASR-0.6B", "Qwen3-ASR-1.7B"]
                    SENSEVOICE_MODEL_OPTIONS = ["SenseVoiceSmall"]

                    # 初始化即按引擎切换模型选项:否则用户重启后即使 engine=qwen3-asr,
                    # 下拉仍显示 whisper 列表,选不到 Qwen 模型,导致 model_size 停留在
                    # whisper 名(如 large-v3-turbo),Qwen 转写要么兜底猜模型要么直接失败。
                    if settings.engine == "qwen3-asr" and settings.model_size not in QWEN_MODEL_OPTIONS:
                        settings.model_size = QWEN_MODEL_OPTIONS[0]
                    if settings.engine == "sensevoice" and settings.model_size not in SENSEVOICE_MODEL_OPTIONS:
                        settings.model_size = SENSEVOICE_MODEL_OPTIONS[0]
                    model_select = ui.select(
                        (QWEN_MODEL_OPTIONS if settings.engine == "qwen3-asr"
                         else SENSEVOICE_MODEL_OPTIONS if settings.engine == "sensevoice"
                         else MODEL_OPTIONS),
                        label="转写模型", value=settings.model_size,
                    ).bind_value(settings, "model_size")

                    def _on_engine_change(e) -> None:
                        if e.value == "sensevoice":
                            if settings.model_size not in SENSEVOICE_MODEL_OPTIONS:
                                settings.model_size = SENSEVOICE_MODEL_OPTIONS[0]
                            model_select.set_options(SENSEVOICE_MODEL_OPTIONS, value=settings.model_size)
                        elif e.value == "qwen3-asr":
                            if settings.model_size not in QWEN_MODEL_OPTIONS:
                                settings.model_size = QWEN_MODEL_OPTIONS[0]
                            model_select.set_options(QWEN_MODEL_OPTIONS, value=settings.model_size)
                        else:  # whisper
                            if settings.model_size not in MODEL_OPTIONS:
                                settings.model_size = MODEL_OPTIONS[0]
                            model_select.set_options(MODEL_OPTIONS, value=settings.model_size)

                    engine_select.on_value_change(_on_engine_change)

                    with ui.row().classes("items-center gap-4"):
                        ui.select({"auto": "自动", "cuda": "GPU", "cpu": "CPU"}, label="设备",
                                  value=settings.device).bind_value(settings, "device")
                    with ui.row().classes("items-center gap-4"):
                        ui.select(["auto", "float16", "int8_float16", "int8"], label="计算精度",
                                  value=settings.compute_type).bind_value(settings, "compute_type")
                        ui.select({"": "自动检测", "zh": "中文", "en": "English"}, label="语言",
                                  value=settings.language).bind_value(settings, "language")
                    ui.switch("保留原始音频", value=settings.keep_audio).bind_value(settings, "keep_audio")
                    ui.switch("VAD 人声过滤(静音不转写;仅 Whisper 引擎生效)", value=settings.vad).bind_value(settings, "vad")
                    ui.switch("生成图文讲义(PPT 关键帧截图)", value=settings.notes).bind_value(settings, "notes")
                    ui.label(
                        "图文讲义:检测画面切换(如 PPT 翻页)截图,配对应时段字幕输出 {标题}.notes.md 讲义。\n"
                        "• 本地上传 / 快手:直接用原视频,无额外流量\n"
                        "• B 站链接:改为下载视频流(体积远大于纯音频;流合并由内置 ffmpeg 完成)"
                    ).classes("text-xs text-grey-6")

                    # ---------- 网络与 Cookie(多平台下载) ----------
                    ui.separator().classes("w-full")
                    ui.label("网络与 Cookie(下载视频用)").classes("font-medium")
                    ui.label(
                        "点「一键检测」自动识别浏览器登录态、cookies.txt 与本机代理,"
                        "自动填好下面的输入框;再点「测试连接」验证,最后保存即可。"
                    ).classes("text-xs text-grey-6")

                    step_labels = []
                    with ui.row().classes("items-center gap-1 text-xs"):
                        for idx, (num, text) in enumerate(
                                (("①", "检测"), ("②", "测试"), ("③", "保存"))):
                            if idx:
                                ui.icon("chevron_right").classes("text-grey-4").style("font-size:14px")
                            step_labels.append(ui.label(f"{num} {text}").classes("bili-step"))

                    def _set_step(active: int) -> None:
                        for idx, label in enumerate(step_labels, start=1):
                            if idx <= active:
                                label.classes(add="active")
                            else:
                                label.classes(remove="active")

                    def _cookie_dirs() -> list[Path]:
                        """cookies.txt 的搜索范围:项目目录/data/下载/桌面/文档 + 输出目录。"""
                        dirs = netcheck.default_cookie_dirs(BASE_DIR, DATA_DIR)
                        out_dir = settings.resolve_output_dir(BASE_DIR)
                        if out_dir not in dirs:
                            dirs.append(out_dir)
                        return dirs

                    with ui.row().classes("w-full items-center gap-2"):
                        detect_btn = ui.button(
                            "一键检测本机网络与 Cookie", icon="travel_explore",
                            on_click=lambda: _run_detect(),
                        ).props("unelevated no-caps color=primary").classes("bili-cta")
                        # 实测 5~8 秒(取决于 YouTube 直连是否要等超时),别写成更短的数字
                        detect_hint = ui.label("约 5~8 秒").classes("text-xs text-grey-5")

                    net_progress = ui.linear_progress(value=0, show_value=False) \
                        .props("rounded size=6px instant-feedback color=primary").classes("w-full")
                    net_progress.set_visibility(False)
                    net_status = ui.label("").classes("text-xs text-grey-7")

                    net_result = ui.column().classes("w-full gap-2")
                    with net_result:
                        ui.label("还没有检测。点上面的按钮,软件会自己找 Cookie 和可用代理。"
                                 ).classes("text-xs text-grey-5")

                    def _chip(kind: str, icon: str, text: str, delay: float = 0.0) -> None:
                        """状态徽章:浅底圆角 + 悬停上浮 + 依次弹入。"""
                        bg, fg, icon_fg = _CHIP_KINDS.get(kind, _CHIP_KINDS["muted"])
                        with ui.row().classes(
                                f"items-center gap-1 px-2 py-1 rounded-md {bg} bili-chip bili-pop "
                                "max-w-full").style(f"animation-delay:{delay:.2f}s"):
                            ui.icon(icon).classes(icon_fg).style("font-size:15px")
                            ui.label(text).classes(f"text-xs {fg} leading-snug")

                    def _render_detection(report) -> None:
                        """把检测结果画成一屏能看懂的状态:平台登录态 / 网络 / 代理 + 原因。"""
                        net_result.clear()
                        states = report.platform_states
                        with net_result:
                            with ui.column().classes("w-full gap-2 bili-fade-up"):
                                with ui.row().classes("items-center gap-2 flex-wrap w-full"):
                                    ui.label("平台登录态").classes("text-xs text-grey-6").style("width:64px")
                                    for idx, platform in enumerate(netcheck.PLATFORMS):
                                        name = netcheck.PLATFORM_LABEL[platform]
                                        cookie = states.get(platform)
                                        if cookie and not cookie.expired:
                                            _chip("ok", "check_circle", name, idx * 0.05)
                                        else:
                                            _chip("muted", "remove_circle_outline", name, idx * 0.05)
                                with ui.row().classes("items-center gap-2 flex-wrap w-full"):
                                    ui.label("网络").classes("text-xs text-grey-6").style("width:64px")
                                    for idx, platform in enumerate(netcheck.PLATFORMS):
                                        probe = report.network.direct.get(platform)
                                        name = netcheck.PLATFORM_LABEL[platform]
                                        if probe and probe.ok:
                                            _chip("ok", "wifi", f"{name} {probe.ms}ms", 0.15 + idx * 0.05)
                                        else:
                                            _chip("bad", "wifi_off", f"{name} 直连不通", 0.15 + idx * 0.05)
                                if report.network.working_proxy:
                                    with ui.row().classes("items-center gap-2 flex-wrap w-full"):
                                        ui.label("代理").classes("text-xs text-grey-6").style("width:64px")
                                        _chip("ok", "lan",
                                              f"{report.network.working_proxy} 可用", 0.35)
                                for line in report.notes:
                                    ui.label(f"· {line}").classes("text-xs text-grey-6")

                    def _apply_detection(report) -> None:
                        """自动填写:只在识别到更优来源时写,绝不清空用户已有配置。"""
                        changed: list[str] = []
                        if report.suggested_browser and \
                                report.suggested_browser != settings.cookies_from_browser:
                            settings.cookies_from_browser = report.suggested_browser
                            browser_select.set_value(report.suggested_browser)
                            changed.append(f"浏览器={report.suggested_browser}")
                        if report.suggested_cookie_file and \
                                report.suggested_cookie_file != settings.cookie_file:
                            settings.cookie_file = report.suggested_cookie_file
                            cookie_input.set_value(report.suggested_cookie_file)
                            changed.append(f"cookies.txt={Path(report.suggested_cookie_file).name}")
                        if report.suggested_proxy and report.suggested_proxy != settings.proxy:
                            settings.proxy = report.suggested_proxy
                            proxy_input.set_value(report.suggested_proxy)
                            changed.append(f"代理={report.suggested_proxy}")
                        if changed:
                            ui.notify("已自动填写:" + "、".join(changed), type="positive", timeout=5000)
                        else:
                            ui.notify("检测完成;当前设置已是最优,无需改动", timeout=4000)

                    async def _run_detect() -> None:
                        # 用 set_text 而不是 Quasar 的 loading 属性:后者会把按钮文案吃掉,
                        # 只剩一个空白粉块,用户看不出在干什么。
                        detect_btn.set_text("正在检测…")
                        detect_btn.props("disable")
                        detect_btn.classes(add="bili-pulse")
                        detect_hint.set_text("稍等,正在探测网络")
                        detect_hint.classes(add="bili-blink")
                        net_result.clear()
                        test_result.clear()
                        net_progress.set_visibility(True)
                        net_progress.set_value(0.06)
                        net_status.set_text("正在扫描本机浏览器与 cookies.txt…")
                        _set_step(1)
                        try:
                            browsers = await run.io_bound(netcheck.detect_browsers)
                            net_progress.set_value(0.32)
                            net_status.set_text(
                                f"已发现 {len(browsers)} 个浏览器配置,正在找 cookies.txt…")
                            files = await run.io_bound(
                                netcheck.detect_cookie_files, _cookie_dirs(), [settings.cookie_file])
                            net_progress.set_value(0.5)
                            net_status.set_text("正在探测网络与代理(YouTube 可能要等几秒)…")
                            network = await run.io_bound(netcheck.probe_network)
                            net_progress.set_value(0.9)
                            net_status.set_text("正在生成自动填写建议…")
                            report = netcheck.build_report(
                                browsers, files, network,
                                current_browser=settings.cookies_from_browser,
                                current_cookie_file=settings.cookie_file,
                                current_proxy=settings.proxy,
                            )
                            net_progress.set_value(1.0)
                            _render_detection(report)
                            _apply_detection(report)
                            _set_step(2)
                            net_status.set_text(
                                "检测完成,已按最优来源填好;下一步点「② 测试连接」验证能不能用。")
                        except Exception as exc:  # noqa: BLE001 — 检测失败不能把设置页打崩
                            logger.exception("网络与 Cookie 检测失败")
                            net_status.set_text(f"检测失败:{exc}")
                            ui.notify(f"检测失败:{exc}", type="negative")
                        finally:
                            detect_btn.set_text("一键检测本机网络与 Cookie")
                            detect_btn.props(remove="disable")
                            detect_btn.classes(remove="bili-pulse")
                            detect_hint.set_text("可重复检测")
                            detect_hint.classes(remove="bili-blink")
                            ui.timer(1.1, lambda: net_progress.set_visibility(False), once=True)

                    def _render_test(report) -> None:
                        test_result.clear()
                        with test_result:
                            with ui.column().classes("w-full gap-2 bili-fade-up"):
                                # flex-wrap + 让长结论自己换行:总结文案(30 字)加上「来源」会顶出卡片右边界
                                with ui.row().classes("items-center gap-2 w-full flex-wrap"):
                                    if report.ok:
                                        _chip("ok", "verified", report.summary)
                                    else:
                                        _chip("warn", "error_outline", report.summary)
                                    ui.label(f"来源:{report.source}").classes(
                                        "text-xs text-grey-5 flex-grow min-w-0")
                                for idx, item in enumerate(report.items):
                                    kind, icon, text = _COOKIE_STATE_CHIP.get(
                                        item.cookie_state, _COOKIE_STATE_CHIP["unknown"])
                                    with ui.row().classes(
                                            "items-center gap-2 w-full flex-wrap bili-fade-up") \
                                            .style(f"animation-delay:{idx * 0.04:.2f}s"):
                                        ui.icon("wifi" if item.network_ok else "wifi_off") \
                                            .classes("text-green-500" if item.network_ok else "text-red-400") \
                                            .style("font-size:16px")
                                        ui.label(netcheck.PLATFORM_LABEL[item.platform]) \
                                            .classes("text-xs font-medium").style("width:56px")
                                        ui.label(f"{item.ms} ms" if item.network_ok else "不可达") \
                                            .classes("text-xs text-grey-5").style("width:52px")
                                        _chip(kind, icon, text, idx * 0.04)
                                        ui.label(item.detail).classes(
                                            "text-xs text-grey-6 flex-grow min-w-0")
                                for warning in report.warnings:
                                    ui.label(f"! {warning}").classes("text-xs text-amber-700")

                    async def _run_test() -> None:
                        test_btn.set_text("测试中…")
                        test_btn.props("disable")
                        test_btn.classes(add="bili-pulse")
                        test_result.clear()
                        with test_result:
                            ui.label("正在测试:逐个平台探测网络,并校验 Cookie 登录态…"
                                     ).classes("text-xs text-grey-6 bili-blink")
                        _set_step(2)
                        try:
                            report = await run.io_bound(
                                netcheck.test_configuration,
                                settings.cookies_from_browser, settings.cookie_file, settings.proxy)
                            _render_test(report)
                            _set_step(3 if report.ok else 2)
                            ui.notify(report.summary,
                                      type="positive" if report.ok else "warning", timeout=5000)
                        except Exception as exc:  # noqa: BLE001
                            logger.exception("网络与 Cookie 测试失败")
                            ui.notify(f"测试失败:{exc}", type="negative")
                        finally:
                            test_btn.set_text("② 测试连接")
                            test_btn.props(remove="disable")
                            test_btn.classes(remove="bili-pulse")

                    async def _pick_cookie_file() -> None:
                        path = await run.io_bound(
                            native_dialog.pick_file, "选择 cookies.txt 文件",
                            native_dialog.COOKIE_FILTER)
                        if not path:
                            return
                        cookie_input.set_value(str(path))
                        total, platforms, _values, error = await run.io_bound(
                            netcheck.parse_cookie_file, path)
                        if error:
                            ui.notify(error, type="negative")
                            return
                        names = "、".join(netcheck.PLATFORM_LABEL[p] for p in platforms
                                         if platforms[p].present and not platforms[p].expired)
                        if names:
                            ui.notify(f"已载入 {total} 条 Cookie,含 {names} 登录态", type="positive")
                        else:
                            ui.notify(f"已载入 {total} 条 Cookie,但没识别到平台登录态", type="warning")

                    async def _probe_proxy() -> None:
                        probe_btn.set_text("探测中…")
                        probe_btn.props("disable")
                        probe_btn.classes(add="bili-pulse")
                        try:
                            found = await run.io_bound(netcheck.find_working_proxy)
                        finally:
                            probe_btn.set_text("探测")
                            probe_btn.props(remove="disable")
                            probe_btn.classes(remove="bili-pulse")
                        if found is None:
                            ui.notify("没找到可用代理:请先启动代理软件(Clash / v2rayN 等)",
                                      type="warning", timeout=5000)
                            return
                        proxy_input.set_value(found.url)
                        ui.notify(f"已找到可用代理:{found.url}({found.ms} ms)", type="positive")

                    async def _export_cookies() -> None:
                        browser = settings.cookies_from_browser
                        if not browser:
                            ui.notify("请先在上面选择要导出的浏览器", type="warning")
                            return
                        dest = DATA_DIR / "cookies.txt"
                        export_btn.set_text("导出中…")
                        export_btn.props("disable")
                        try:
                            path = await run.io_bound(
                                netcheck.export_browser_cookies, browser, dest)
                        except netcheck.NetcheckError as exc:
                            # 占用时给出唯一真能通的动作:关掉浏览器再点一次
                            ui.notify(str(exc), type="negative", timeout=8000)
                            return
                        finally:
                            export_btn.set_text("从浏览器导出 cookies.txt")
                            export_btn.props(remove="disable")
                        cookie_input.set_value(str(path))
                        ui.notify(f"已导出到 {path}(内含登录凭据,请勿分享);"
                                  f"下一步点「③ 保存设置」再「测试连接」即可",
                                  type="positive", timeout=6000)
                        _set_step(2)

                    browser_select = ui.select(
                        {"": "不使用", "chrome": "Chrome", "edge": "Edge", "firefox": "Firefox",
                         "brave": "Brave", "chromium": "Chromium", "opera": "Opera",
                         "vivaldi": "Vivaldi"},
                        label="浏览器 Cookie(抖音/部分 YouTube 视频需要)",
                        value=settings.cookies_from_browser,
                    ).bind_value(settings, "cookies_from_browser").classes("w-full")

                    with ui.row().classes("w-full items-center gap-1 no-wrap"):
                        cookie_input = ui.input(
                            "cookies.txt 文件路径(Netscape 格式,优先于浏览器 Cookie)"
                        ).bind_value(settings, "cookie_file").classes("flex-grow")
                        ui.button(icon="folder_open", on_click=lambda: _pick_cookie_file()) \
                            .props("flat round dense color=grey-7").tooltip("选择 cookies.txt 文件")

                    with ui.row().classes("w-full items-center gap-1 no-wrap"):
                        proxy_input = ui.input(
                            "网络代理(YouTube 等境外平台需要,如 http://127.0.0.1:7890)"
                        ).bind_value(settings, "proxy").classes("flex-grow")
                        probe_btn = ui.button("探测", icon="lan", on_click=lambda: _probe_proxy()) \
                            .props("flat dense no-caps color=grey-8").tooltip("扫描本机常见代理端口")

                    with ui.row().classes("w-full items-center gap-2"):
                        test_btn = ui.button("② 测试连接", icon="wifi_tethering",
                                             on_click=lambda: _run_test()) \
                            .props("outline no-caps color=primary")
                        ui.button("③ 保存设置", icon="save", on_click=lambda: _save_network()) \
                            .props("unelevated no-caps")
                        ui.label("保存后立即生效,不用重启").classes("text-xs text-grey-5")

                    test_result = ui.column().classes("w-full gap-2")

                    def _save_network() -> None:
                        """保存按钮:设置页所有值一起落盘,并把步骤条点亮到第 ③ 步。"""
                        save()
                        _set_step(3)

                    with ui.expansion("各平台到底需不需要 Cookie / 代理?", icon="table_chart") \
                            .classes("w-full"):
                        ui.markdown(
                            "| 平台 | 需要 Cookie? | 需要代理? | 说明 |\n"
                            "| --- | --- | --- | --- |\n"
                            "| B 站 | 多数不需要 | 不需要 | 1080P+/大会员/部分番剧要登录态 |\n"
                            "| 抖音 | **建议要** | 不需要 | 未登录常被风控,报 cookie 错误 |\n"
                            "| 小红书 | **建议要** | 不需要 | 登录后才返回完整视频地址 |\n"
                            "| 快手 | 一般不需要 | 不需要 | 走分享页解析,无需登录 |\n"
                            "| YouTube | 部分需要 | **必须** | 年龄限制/会员视频要登录;境内需代理 |\n"
                        )
                    with ui.expansion("三种 Cookie 来源怎么选?", icon="source").classes("w-full"):
                        ui.markdown(
                            "1. **浏览器 Cookie(最省事)**:软件直接读你浏览器里的登录态。\n"
                            "   缺点:浏览器**正在运行**时 Cookie 库被独占锁定,读不到也下载不了带 Cookie 的内容;"
                            "Chrome 127+ 的 Cookie 还用了应用绑定加密,只有浏览器本体能解(本程序会临时起一个"
                            "无窗口实例让浏览器自己解密,用完即删)。\n"
                            "2. **cookies.txt(最稳)**:优先于浏览器 Cookie,不受浏览器开关影响;"
                            "可用下面的「导出」按钮生成。\n"
                            "3. **都不用**:只下载公开内容,B 站普通视频足够。\n\n"
                            "> 提示:导出文件内含登录凭据,只存在本机 `data/` 目录,不要发给别人。"
                        )
                        export_btn = ui.button("从浏览器导出 cookies.txt", icon="download",
                                               on_click=lambda: _export_cookies()) \
                            .props("flat dense no-caps")
                    with ui.expansion("检测结果 / 测试结果怎么看?", icon="fact_check") \
                            .classes("w-full"):
                        ui.markdown(
                            "- **平台登录态**:绿点 = 在某个来源里找到该平台登录 Cookie;灰点 = 没找到"
                            "(不代表不能下,只是清晰度/风控可能受限)。\n"
                            "- **网络**:各平台直连延迟;红标 = 连不上,境外平台通常是没配代理。\n"
                            "- **已登录(已校验)**:用 B 站接口真实核对了账号,最可靠。\n"
                            "- **已登录**:只确认 Cookie 存在,没做在线校验(抖音/小红书接口需签名)。\n"
                            "- **读取失败(锁图标)**:浏览器正在运行,Cookie 库被占用 → 关掉浏览器再测,"
                            "或改用 cookies.txt。\n"
                            "- **Chrome 127+/Edge 的 Cookie 是应用绑定加密(v20)**:密钥只有浏览器本体在"
                            "**原始配置路径**上能解,关掉浏览器后测试可正常读出;开着浏览器时值读不到,"
                            "但登录态(名字/有效期)仍会如实显示。"
                        )
                    with ui.expansion("下载失败的常见原因", icon="build").classes("w-full"):
                        ui.markdown(
                            "- **提示需要 Cookie**:先在浏览器登录该平台,再点「一键检测」;"
                            "或导出 cookies.txt 指给软件。\n"
                            "- **抖音/小红书解析失败**:多为登录态过期 → 重新登录后重新导出。\n"
                            "- **YouTube 解析超时**:检查代理端口是否填对(设置页「探测」可自动找),"
                            "代理软件是否允许局域网连接。\n"
                            "- **B 站高清只有 480P**:需要登录 Cookie(大会员内容还要大会员账号)。"
                        )

                    # 并发工作线程数:影响下载/转码并行度(转写始终串行保护 GPU)
                    ui.select(
                        {1: "1(最省资源)", 2: "2(默认)", 3: "3", 4: "4(高吞吐)"},
                        label="并发任务数(下载/转码并行,转写仍串行)",
                        value=settings.workers,
                    ).bind_value(settings, "workers").classes("w-full")

                    # ---------- AI 调用与命令行(外部程序访问) ----------
                    ui.separator().classes("w-full")
                    ui.label("AI 调用与命令行").classes("font-medium")
                    ui.switch(
                        "允许命令行(CLI)调用:脚本 / 自动化 / 终端",
                        value=settings.enable_cli,
                    ).bind_value(settings, "enable_cli")
                    ui.switch(
                        "允许 MCP 调用:WorkBuddy / DeepSeek-Honey 等 AI 软件",
                        value=settings.enable_mcp,
                    ).bind_value(settings, "enable_mcp")
                    ui.label(
                        "两个通道默认开启,关闭后新启动的进程会被直接拒绝"
                        "(退出码 3 / MCP 退出码 1),已在连接中的客户端需重连才生效;\n"
                        "桌面端本身始终可用。重新开启:勾选后点「保存设置」,"
                        "或把 settings.json 里的 enable_cli / enable_mcp 改成 true。"
                    ).classes("text-xs text-grey-6")

                    def _mcp_config_text() -> str:
                        from app.mcp_server import client_config
                        return json.dumps(client_config(), ensure_ascii=False, indent=2)

                    with ui.expansion("MCP 客户端配置(复制到 AI 软件即可)", icon="content_copy") \
                            .classes("w-full"):
                        ui.code(_mcp_config_text(), language="json").classes("w-full text-xs")

                        def _copy_mcp() -> None:
                            ui.clipboard.write(_mcp_config_text())
                            ui.notify("配置已复制到剪贴板", type="positive")

                        ui.button("复制配置", icon="content_copy", on_click=_copy_mcp) \
                            .props("flat dense")
                        ui.label(
                            f"当前可执行文件:{sys.executable}"
                            if getattr(sys, "frozen", False)
                            else f"源码模式:{sys.executable} -m app.mcp_server"
                        ).classes("text-xs text-grey-6")

                    # 模型外置:模型目录(用户自放模型包) + 状态提示
                    model_dir_input = ui.input("模型目录(留空=默认 AppData 位置)").bind_value(
                        settings, "model_dir").classes("w-full")
                    model_status = ui.label("").classes("text-xs text-grey-7")
                    ui.label(
                        "模型外置:模型包解压后整个目录放入模型目录即可自动检测。\n"
                        "• SenseVoice(最快,CPU 约 30 倍实时):下载 sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17\n"
                        "• Qwen3-ASR:从 ModelScope 下载 Qwen/Qwen3-ASR-0.6B 或 Qwen3-ASR-1.7B"
                    ).classes("text-xs text-grey-6")

                    def refresh_model_status() -> None:
                        from app.models import describe_models
                        d = resolve_model_dir(settings.model_dir)
                        model_status.set_text(f"模型目录:{d}\n{describe_models(d)}")

                    with ui.row().classes("items-center gap-2"):
                        ui.button("打开模型目录", icon="folder_open", on_click=lambda: _open_dir(
                            resolve_model_dir(settings.model_dir)))
                        ui.button("刷新检测", icon="refresh", on_click=refresh_model_status)
                        ui.button("打开日志目录", icon="folder_open", on_click=lambda: _open_dir(log_dir()))
                    refresh_model_status()

                    def save() -> None:
                        save_settings(settings, DATA_DIR / "settings.json")
                        pipeline.apply_settings(settings)
                        refresh_model_status()
                        pipeline.prewarm()  # 引擎/模型变更后后台预热,提交任务免冷启动
                        ui.notify("设置已保存,模型正在后台预热(提交任务即可直接转写)",
                                  type="positive")

                    with ui.row().classes("gap-2 mt-2"):
                        ui.button("保存设置", icon="save", on_click=save)
                        ui.button("恢复默认", on_click=lambda: _reset_settings(
                            out_input, model_dir_input, refresh_model_status)).props("flat")

            # ---------- 硬件检测与设备建议 ----------
            with ui.card().classes("w-full max-w-3xl mt-4"):
                with ui.column().classes("gap-3"):
                    ui.label("硬件检测与设备建议").classes("font-medium")
                    hw_status = ui.label(
                        "点击「检测硬件」查看当前 CPU/GPU 与推荐的转写设备。"
                    ).classes("text-sm text-grey-7")
                    hw_result = ui.label("").classes("text-xs whitespace-pre-wrap")

                    def _run_hw_detect() -> None:
                        hw_status.set_text("正在检测硬件,请稍候…")
                        hw_result.set_text("")
                        threading.Thread(target=_hw_worker, daemon=True).start()

                    def _hw_worker() -> None:
                        try:
                            from app.hardware import detect_hardware
                            # 按当前引擎探测,给出匹配的 CUDA 建议
                            hw_results.put(detect_hardware(engine=settings.engine))
                        except Exception as exc:  # noqa: BLE001 — 检测失败也要回显给用户
                            hw_results.put(exc)

                    def _apply_hw_result(info) -> None:
                        if info.engine == "qwen3-asr" and not info.torch_cuda_available:
                            hw_status.set_text(
                                "检测完成:当前所选 Qwen3-ASR 引擎仅支持 CPU 转写"
                                "(内置 PyTorch 未启用 CUDA)。如需 GPU,请切到 Whisper 引擎。"
                            )
                        else:
                            hw_status.set_text("检测完成,已按建议更新设备/精度(记得点保存设置)")
                        hw_result.set_text(
                            f"CPU:{info.cpu_name}\n"
                            f"GPU:{info.gpu_name or '未检测到'}"
                            f"(显存 {info.vram_mb} MB)\n"
                            f"GPU 加速包:{'已放置' if info.gpu_pack_present else '未放置'}\n"
                            f"CUDA 可用(Whisper/ctranslate2):{'是' if info.cuda_available else '否'}\n"
                            f"PyTorch CUDA(仅 Qwen3-ASR):{'是' if info.torch_cuda_available else '否'}\n\n"
                            f"针对当前引擎 {info.engine} 的建议:\n{info.recommendation}"
                        )
                        settings.device = info.device
                        settings.compute_type = info.compute_type

                    with ui.row().classes("items-center gap-2"):
                        ui.button("检测硬件", icon="memory", on_click=_run_hw_detect)
                        ui.button("打开 GPU 目录", icon="folder_open",
                                  on_click=lambda: _open_dir(default_gpu_dir()))

            # ---------- 关于与帮助 ----------
            with ui.card().classes("w-full max-w-3xl mt-4"):
                ui.label(f"关于与帮助 · v{APP_VERSION}").classes("font-medium")
                ui.label(
                    "纯本地处理:音频/视频与转写文本均不上传(仅首次使用时联网下载模型)。\n"
                    "详细文档见安装目录 docs/(用户手册/技术手册)。"
                ).classes("text-xs text-grey-6")

                with ui.expansion("快速上手(四步)", icon="play_circle").classes("w-full"):
                    ui.markdown(
                        "1. **链接转写**:任务页粘贴 B 站链接(单 P / 多 P / 合集均可)→ 点「添加任务」。\n"
                        "2. **本机文件**:点「选择文件(推荐,立即转写)…」在系统文件框里选中视频/音频,立即入队。\n"
                        "3. 等任务状态变为**已完成**(下载 → 转码 → 转写 → 保存,进度条实时可见)。\n"
                        "4. 点操作列的 📁 图标打开输出目录取结果:\n"
                        "   - `.srt` 带时间戳字幕(可导入剪辑软件)\n"
                        "   - `.txt` 纯文本稿 / `.md` 带时间戳的 Markdown\n"
                        "   - `.notes.md` 图文讲义(需在设置中开启)"
                    )
                with ui.expansion("图文讲义(PPT 截图)怎么用", icon="image").classes("w-full"):
                    ui.markdown(
                        "- 设置页开启「生成图文讲义」后,任务会自动检测画面切换(如 PPT 翻页)截图,"
                        "并配上该页期间的字幕,输出「时间戳 + 截图 + 文字」的 `{标题}.notes.md`;\n"
                        "   标题时间戳可点击跳回视频对应位置,适合网课/公开课复习。\n"
                        "- **注意**:B 站链接任务会改为**下载视频流**(体积远大于纯音频,介意流量请关闭);"
                        "本机文件 / 快手直接用原视频,无额外流量。\n"
                        "- 讲义与 `{标题}_frames` 截图文件夹是相对引用,**移动/备份时要放在一起**;"
                        "推荐用 Typora / VSCode / Obsidian 查看。\n"
                        "- 纯音频文件(mp3 等)没有画面,自动跳过讲义;讲义失败不影响字幕输出。"
                    )
                with ui.expansion("本机文件 vs 浏览器上传", icon="upload_file").classes("w-full"):
                    ui.markdown(
                        "- **选择文件(推荐)**:弹系统文件框后直接转写原文件,零拷贝、秒提交,大文件首选。\n"
                        "- **浏览器上传**:仅在用手机/其他电脑访问本机网页时使用;"
                        "该通道按 base64 传输,**大文件会明显偏慢**属正常现象。\n"
                        "- 浏览器上传的副本保存在 `uploads/` 目录,属用户资产不会被自动删除;"
                        "系统文件框方式不复制文件,直接引用原路径。"
                    )
                with ui.expansion("识别引擎与模型怎么选", icon="graphic_eq").classes("w-full"):
                    ui.markdown(
                        "- **Whisper(默认)**:`large-v3-turbo` 通用性最好,GPU/CPU 均可,模型约 1.6 GB(首次自动下载)。\n"
                        "- **Qwen3-ASR**:中文/方言识别强,`0.6B` 低配机也能跑(从 ModelScope 下载)。\n"
                        "- **SenseVoice**:速度最快,适合长音频批量。\n"
                        "- 模型均外置:可自行下载模型包放入设置页的「模型目录」→ 刷新检测即可离线使用;\n"
                        "   首次转写需加载模型,等待时间稍长属正常。"
                    )
                with ui.expansion("GPU 加速包(可选)", icon="bolt").classes("w-full"):
                    ui.markdown(
                        "- 安装包不内置 CUDA 运行时;需要 GPU 加速时下载 `bili-transcriber-gpu-<版本>.zip`,\n"
                        "   解压到 `%LOCALAPPDATA%\\Bili Note\\gpu\\`(保持 `nvidia/` 目录结构)。\n"
                        "- 软件启动自动检测并启用 CUDA;未放置则回退 CPU,功能不受影响。\n"
                        "- 不确定机器配置?用上方「检测硬件」一键获得设备/精度建议。"
                    )
                with ui.expansion("常见问题", icon="help").classes("w-full"):
                    ui.markdown(
                        "- **转写慢**:确认设置中设备为 GPU;首次运行需下载模型(约 1.6 GB);\n"
                        "   CPU 模式建议换 Qwen3-ASR 引擎或更小的 Whisper 模型。\n"
                        "- **显存不足**:计算精度改 `int8`,或换 `medium`/`small` 模型。\n"
                        "- **唱歌/纯音乐没字幕**:VAD 只识别人声,程序会自动关闭 VAD 重试,属正常。\n"
                        "- **链接解析失败**:B 站接口变动,`pip install -U yt-dlp` 后重试。\n"
                        "- **端口被占用**:设置环境变量 `BILI_PORT=9000` 后启动。\n"
                        "- **需要 FFmpeg 吗?**:不需要,软件内置(若系统装有完整版会优先使用)。\n"
                        "- **抖音/小红书要登录吗?**:建议要。设置页「一键检测本机网络与 Cookie」"
                        "会自动识别浏览器登录态并填好。\n"
                        "- **YouTube 打不开?**:先配代理。同一节的「探测」按钮会扫本机代理端口"
                        "并验证哪个真能访问 YouTube。"
                    )
                with ui.expansion("输出文件与数据说明", icon="folder").classes("w-full"):
                    ui.markdown(
                        "- 输出目录(设置页可改)下按 `{BV号}_{标题}` 分文件夹,含 srt / txt / md /"
                        " 讲义 / 截图,原始音频默认保留(可在设置关闭)。\n"
                        "- 断点续跑:下载与转码有缓存,失败重跑不会重复下载/转码。\n"
                        "- 历史页删除记录时可勾选「同时删除磁盘产物」;本地上传的原文件永不自动删除。\n"
                        "- 历史数据库/设置保存在 `data/` 目录,卸载不会删除(缓存 `cache/` 会清理)。"
                    )

    # ---------- 定时刷新(工作线程只写 Store/deque,UI 线程轮询) ----------
    stall_notified: set[str] = set()  # 已提醒过卡顿的任务(恢复进度后重置)
    STALL_THRESHOLD = 60.0  # 秒:超过该时长无进度更新则提醒

    def refresh() -> None:
        while events:
            log.push(events.popleft())
        queue_label.set_text(f"待处理:{pipeline.pending_count}")
        rows = [_job_row(j) for j in store.list_jobs()]
        active_table.rows = [r for r in rows if r["raw_status"] in ACTIVE_STATUSES]
        active_table.update()
        # 历史表带搜索过滤(空搜索 = 全部);进行中任务表不受搜索影响
        filtered = [_job_row(j) for j in store.list_jobs(search.value or "")]
        history_table.rows = [r for r in filtered if r["raw_status"] not in ACTIVE_STATUSES]
        history_table.update()
        # 长时间无进展提醒:转写/下载/转码超过阈值无进度更新时提示一次
        for r in rows:
            mid = r["media_id"]
            if r["raw_status"] in ("transcribing", "downloading", "converting"):
                if pipeline.stalled(mid, STALL_THRESHOLD) and mid not in stall_notified:
                    stall_notified.add(mid)
                    ui.notify(
                        f"「{r['title']}」长时间无进展(可能仍在加载模型或处理长音频),"
                        f"请耐心等待;若长时间卡住可暂停或取消后重试。",
                        type="warning", timeout=0,
                    )
            else:
                stall_notified.discard(mid)
        # 后台线程的硬件检测结果回填到设置页
        while not hw_results.empty():
            item = hw_results.get_nowait()
            if isinstance(item, Exception):
                hw_status.set_text(f"检测失败:{item}")
            else:
                _apply_hw_result(item)

    ui.timer(0.6, refresh)

    def refresh_diary_if_active() -> None:
        if tabs.value is tab_diary:
            refresh_diary()

    ui.timer(2.0, refresh_diary_if_active)
