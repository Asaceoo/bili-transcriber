"""NiceGUI 页面构建:任务/历史/运行日记/设置 四页签(原 main.py 的 UI 部分)。"""

from __future__ import annotations

import logging
import threading

from nicegui import run, ui

from app import APP_VERSION, converter, native_dialog
from app.gpu_runtime import default_gpu_dir
from app.logging_setup import log_dir
from app.models import resolve_model_dir
from app.store import save_settings
from app.ui.helpers import (
    _cancel, _confirm_clear_finished, _confirm_delete, _first_arg, _job_row,
    _on_upload, _open_dir, _open_path, _pause, _reset_settings, _resume, _rerun,
)
from app.ui.state import (
    ACTIVE_STATUSES, DATA_DIR, MODEL_OPTIONS, diary_state, diary_lines,
    events, hw_results, pipeline, settings, store,
)

logger = logging.getLogger(__name__)

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
            with ui.card().classes("max-w-lg"):
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
                    ui.select(
                        {"": "不使用", "chrome": "Chrome", "edge": "Edge",
                         "firefox": "Firefox", "brave": "Brave", "opera": "Opera",
                         "vivaldi": "Vivaldi"},
                        label="浏览器 Cookie(抖音/部分 YouTube 视频需要)",
                        value=settings.cookies_from_browser,
                    ).bind_value(settings, "cookies_from_browser").classes("w-full")
                    ui.input(
                        "cookies.txt 文件路径(Netscape 格式,优先于浏览器 Cookie)"
                    ).bind_value(settings, "cookie_file").classes("w-full")
                    ui.input(
                        "网络代理(YouTube 等境外平台需要,如 http://127.0.0.1:7890)"
                    ).bind_value(settings, "proxy").classes("w-full")
                    ui.label(
                        "说明:B 站/抖音/小红书/快手国内直连即可;YouTube 需自行配置代理。\n"
                        "Cookie 用于访问需要登录的内容(高清/会员视频等),从浏览器读取或导出 cookies.txt。"
                    ).classes("text-xs text-grey-6")

                    # 并发工作线程数:影响下载/转码并行度(转写始终串行保护 GPU)
                    ui.select(
                        {1: "1(最省资源)", 2: "2(默认)", 3: "3", 4: "4(高吞吐)"},
                        label="并发任务数(下载/转码并行,转写仍串行)",
                        value=settings.workers,
                    ).bind_value(settings, "workers").classes("w-full")

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
            with ui.card().classes("max-w-lg mt-4"):
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
            with ui.card().classes("max-w-lg mt-4"):
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
                        "- **需要 FFmpeg 吗?**:不需要,软件内置(若系统装有完整版会优先使用)。"
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
