"""无界面服务层:CLI / MCP 共用(与 GUI 共享 data/ 下的同一份历史与设置)。

设计要点:
- 复用 Pipeline:模型懒加载、串行转写、断点续跑等既有语义不变;
- submit() 先 probe 再入队(借助 Pipeline.submit_prepared),调用方拿得到
  media_id 可立即轮询,不必猜测任务是否已展开;
- 每次调用可临时覆盖引擎/模型等参数,仅本进程内生效,不写回 settings.json
  (避免 AI 调用悄悄改掉用户的桌面端配置);如需持久化用 set_settings()。
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from app.downloader import Downloader
from app.paths import base_dir as default_base_dir
from app.paths import data_dir as default_data_dir
from app.pipeline import ACTIVE_STATUSES, TERMINAL_STATUSES, Pipeline
from app.store import Job, Settings, Store, _coerce_setting, load_settings, save_settings

logger = logging.getLogger(__name__)

# 允许调用方临时覆盖/持久化的设置字段(其余字段不暴露,避免误改)
OVERRIDABLE_SETTINGS = (
    "output_dir", "model_size", "device", "compute_type", "language",
    "notes", "vad", "keep_audio", "engine", "model_dir",
    "cookies_from_browser", "cookie_file", "proxy", "workers",
)


class ServiceError(RuntimeError):
    """面向调用方的可读错误(参数/来源/状态不合法)。"""


_BARE_VIDEO_ID = re.compile(r"^(?:BV[0-9A-Za-z]{10}|av\d+)$", re.IGNORECASE)

VALID_ENGINES = ("whisper", "qwen3-asr", "sensevoice")
VALID_DEVICES = ("auto", "cuda", "cpu")


def _validate_settings(changes: dict) -> list[str]:
    """校验待写入的设置。硬错误抛 ServiceError,可容忍问题作为 warning 返回。

    output_dir 必须在写库之前验证可创建、可写:否则 AI 传一个错路径(盘符不
    存在/只读)会被持久化,下一轮转写在下载+转写全部跑完后才于"保存"阶段
    失败 —— 几十分钟 GPU 白烧,且用户很难把原因和一句设置联系起来。
    """
    warnings: list[str] = []
    engine = changes.get("engine")
    if engine is not None and engine not in VALID_ENGINES:
        raise ServiceError(f"engine 不支持:{engine}(可选:{', '.join(VALID_ENGINES)})")
    device = changes.get("device")
    if device is not None and device not in VALID_DEVICES:
        raise ServiceError(f"device 不支持:{device}(可选:{', '.join(VALID_DEVICES)})")
    out = changes.get("output_dir")
    if out is not None:
        if not str(out).strip():
            # `--set output_dir=` 会把空串落盘,之后 resolve 成 base_dir,
            # 产物直接散落到程序根目录 —— 当场拒绝比事后收拾便宜
            raise ServiceError("output_dir 不能为空(留空不等于使用默认值)")
        target = Path(str(out)).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ServiceError(f"output_dir 无法创建:{target}({exc})") from exc
        probe = target / f".bili_write_probe_{os.getpid()}"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise ServiceError(f"output_dir 不可写:{target}({exc})") from exc
        changes["output_dir"] = str(target)
    cookie = changes.get("cookie_file")
    if cookie and not Path(str(cookie)).exists():
        warnings.append(f"cookie_file 不存在:{cookie}(需要登录的视频会下载失败)")
    model_dir = changes.get("model_dir")
    if model_dir and not Path(str(model_dir)).is_dir():
        warnings.append(f"model_dir 不存在:{model_dir}")
    return warnings


def normalize_source(source: str) -> str:
    """裸 BV/av 号补全为可解析的 B站地址(AI 调用常只给视频号)。"""
    if _BARE_VIDEO_ID.match(source):
        return f"https://www.bilibili.com/video/{source}"
    return source


# ---------- 外部调用通道开关(桌面端设置页维护) ----------
# 通道名 -> 设置字段。这两个字段**不在** OVERRIDABLE_SETTINGS 里:CLI/MCP
# 只能读,不能改 —— 否则一个被提示注入的 AI 就能给自己开门或关门。
CHANNEL_SWITCHES = {"cli": "enable_cli", "mcp": "enable_mcp"}
CHANNEL_LABELS = {"cli": "命令行(CLI)", "mcp": "MCP(AI 软件调用)"}


def channel_block_reason(channel: str, settings_path: Path | None = None) -> str:
    """通道被用户在设置页关闭时返回可读原因;可用时返回空串。

    直接读 settings.json 而不构造 Service:通道关闭时连数据库都不该打开
    (否则白拿一把 SQLite 锁、白建一个 pipeline)。

    配置损坏时按默认值(开启)放行 —— 这道开关用于收敛外部调用面,不是
    抗篡改的安全边界(能手改 settings.json 的人本就能改回来);而毁掉
    settings.json 的用户若连 CLI 都进不去,反而更难自救。
    """
    field = CHANNEL_SWITCHES.get(channel)
    if field is None:
        return ""
    path = Path(settings_path) if settings_path else default_data_dir() / "settings.json"
    try:
        enabled = bool(getattr(load_settings(path), field, True))
    except Exception:  # noqa: BLE001 — 读配置失败不应把用户锁在门外
        logger.exception("读取通道开关失败,按开启处理: %s", path)
        return ""
    if enabled:
        return ""
    label = CHANNEL_LABELS.get(channel, channel)
    return (f"{label}通道已在桌面端「设置」中关闭。"
            f"重新开启:打开桌面端设置页勾选,或把 {path} 里的 \"{field}\" 改成 true。")


def job_to_dict(job: Job) -> dict:
    data = asdict(job)
    data["paths"] = {
        "srt": job.srt_path, "txt": job.txt_path,
        "md": job.md_path, "notes": job.notes_path,
    }
    data["terminal"] = job.status in TERMINAL_STATUSES
    return data


def json_safe(value: Any) -> Any:
    """把结构里的 NaN/Infinity 换成 None,并递归处理容器。

    json.dumps 默认 allow_nan=True 会写出裸 `NaN`,那不是合法 JSON:
    JavaScript 的 JSON.parse / 多数 MCP 客户端会直接抛错,一次坏进度值
    就能把整条响应变成"客户端解析失败"。在出站边界统一净化。
    """
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


class Service:
    def __init__(self, downloader: Downloader | None = None,
                 store: Store | None = None, settings: Settings | None = None,
                 base_dir: Path | None = None, pipeline: Pipeline | None = None):
        self.base_dir = Path(base_dir) if base_dir else default_base_dir()
        data_dir = self.base_dir / "data" if base_dir else default_data_dir()
        self.settings_path = data_dir / "settings.json"
        self.store = store or Store(data_dir / "history.db")
        self.settings = settings or load_settings(self.settings_path)
        # 已持久化的基线(与"本次进程生效的设置"区分开):get_settings() 必须
        # 报磁盘上的真相,否则 AI 传过一次 engine=sensevoice 覆盖后再查设置,
        # 会看到 sensevoice 并误以为用户的配置被改写了。
        self._persisted: Settings = self.settings
        self._overrides: dict[str, Any] = {}
        self.downloader = downloader or Downloader()
        self.pipeline = pipeline or Pipeline(
            downloader=self.downloader, store=self.store,
            settings=self.settings, base_dir=self.base_dir,
            recover_paused=False,  # 后台进程不擅自恢复桌面端暂停的任务
        )
        if pipeline is not None:
            # 注入的 pipeline(测试替身)不接管设置同步
            self.pipeline.settings = self.settings

    # ---------- 提交 ----------

    def submit(self, source: str, **overrides: Any) -> dict:
        """提交 URL 或本地文件。返回 {source, jobs:[{media_id,title,status}],...}。"""
        source = (source or "").strip()
        if not source:
            raise ServiceError("source 不能为空(传 B站/快手链接或本地文件路径)")
        self._apply_overrides(overrides)

        path = Path(source)
        if path.exists():
            if path.is_dir():
                raise ServiceError(f"需要文件而不是目录:{path}")
            return self._submit_local(path)

        source = normalize_source(source)
        try:
            entries = self.downloader.probe(source)
        except Exception as exc:  # noqa: BLE001 — 统一转成可读错误给调用方
            raise ServiceError(f"解析失败:{exc}") from exc
        if not entries:
            raise ServiceError("未解析到任何视频条目(链接可能无效或需要登录)")
        results = self.pipeline.submit_prepared(entries)
        return {
            "source": source, "kind": "url", "jobs": results,
            "total": len(results),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
            "running": sum(1 for r in results if r["status"] == "running"),
        }

    def _submit_local(self, path: Path) -> dict:
        media_id = "local_" + Pipeline._local_hash(path)
        existing = self.store.get_job(media_id)
        if (existing and existing.status == "done" and existing.md_path
                and Path(existing.md_path).exists()):
            return {"source": str(path), "kind": "local",
                    "jobs": [{"media_id": media_id, "title": existing.title,
                              "status": "skipped"}],
                    "total": 1, "skipped": 1, "running": 0}
        if existing and existing.status in ACTIVE_STATUSES:
            # 同一文件正在排队/执行:不再入队,直接告诉调用方它已在处理
            return {"source": str(path), "kind": "local",
                    "jobs": [{"media_id": media_id, "title": existing.title,
                              "status": "running", "stage": existing.status}],
                    "total": 1, "skipped": 0, "running": 1}
        self.pipeline.submit_local(path)
        job = self.store.get_job(media_id)
        if job is None:
            raise ServiceError(f"本地文件入队失败:{path}")
        return {"source": str(path), "kind": "local",
                "jobs": [{"media_id": job.media_id, "title": job.title,
                          "status": job.status}],
                "total": 1, "skipped": 0, "running": 0}

    def _apply_overrides(self, overrides: dict) -> None:
        unknown = [k for k, v in overrides.items()
                   if v is not None and k not in OVERRIDABLE_SETTINGS]
        if unknown:
            raise ServiceError(f"不支持覆盖的设置项:{', '.join(unknown)}")
        clean = {k: v for k, v in overrides.items()
                 if v is not None and k in OVERRIDABLE_SETTINGS}
        if not clean:
            return
        _validate_settings(clean)
        self._overrides.update(clean)
        self.settings = replace(self.settings, **clean)
        self.pipeline.apply_settings(self.settings)

    # ---------- 查询 ----------

    def status(self, media_id: str) -> dict:
        job = self.store.get_job(media_id)
        if job is None:
            raise ServiceError(f"任务不存在:{media_id}")
        return job_to_dict(job)

    def list_jobs(self, status: str = "", search: str = "", limit: int = 20) -> list[dict]:
        jobs = self.store.list_jobs(search or "")
        status = (status or "").strip()
        if status:
            # AI 常按直觉传 "running"/"active":映射到进行中的真实状态集合,
            # 而不是返回空列表让它以为"没有任务在跑"。
            if status.lower() in ("running", "active", "processing", "pending"):
                jobs = [j for j in jobs if j.status in ACTIVE_STATUSES]
            else:
                jobs = [j for j in jobs if j.status == status]
        try:
            limit = int(limit if limit not in (None, "") else 20)
        except (TypeError, ValueError):
            limit = 20  # 坏值回退默认,不让 AI 传参失误把列表查询打成异常
        limit = max(1, min(limit, 200))
        return [job_to_dict(j) for j in jobs[:limit]]

    def wait(self, media_ids: list[str], timeout_sec: float = 600.0,
             poll_sec: float = 0.5) -> dict:
        """轮询直到全部进入终态或超时。

        返回 {jobs, all_done, timed_out, waited_sec};若目标任务处于排队态但
        本进程根本不会调度它(上次会话/桌面端遗留),额外返回 stalled=True 并
        立即结束 —— 否则调用方会白等满 600s 才拿到一个"超时"。
        """
        ids = [str(i).strip() for i in (media_ids or []) if str(i).strip()]
        if not ids:
            raise ServiceError("media_ids 不能为空")
        # 快速失败:ID 不存在时不要白等满 timeout(客户端常传错 ID,600s 卡死极糟)
        missing = [mid for mid in ids if self.store.get_job(mid) is None]
        if missing:
            raise ServiceError(f"任务不存在:{', '.join(missing)}")
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        started = time.monotonic()
        while True:
            jobs = []
            for mid in ids:
                job = self.store.get_job(mid)
                if job is not None:
                    jobs.append(job_to_dict(job))
            done = len(jobs) == len(ids) and all(j["terminal"] for j in jobs)
            timed_out = time.monotonic() >= deadline
            if done or timed_out:
                return {
                    "jobs": jobs, "all_done": done, "timed_out": not done and timed_out,
                    "waited_sec": round(time.monotonic() - started, 1),
                }
            if self._has_orphan(ids):
                # 没有工作线程、队列也空:这些排队任务在本进程永远不会动
                return {
                    "jobs": jobs, "all_done": False, "timed_out": False,
                    "stalled": True,
                    "waited_sec": round(time.monotonic() - started, 1),
                    "hint": "任务处于排队态但本进程未在调度它(可能由桌面端或上次会话提交)。"
                            "请用 job_status 轮询;直接重新提交同一链接可直接复用。",
                }
            time.sleep(min(max(0.05, poll_sec), 2.0))

    def _has_orphan(self, ids: list[str]) -> bool:
        """目标里仍有排队任务,而本进程既无工作线程也无待处理队列 → 永远不会推进。"""
        pipeline = self.pipeline
        if getattr(pipeline, "alive_workers", 0) > 0:
            return False
        if getattr(getattr(pipeline, "_queue", None), "unfinished_tasks", 0) > 0:
            return False
        for mid in ids:
            job = self.store.get_job(mid)
            if job is not None and job.status == "queued":
                return True
        return False

    def transcript(self, media_id: str, fmt: str = "md", max_chars: int = 12000) -> dict:
        job = self.store.get_job(media_id)
        if job is None:
            raise ServiceError(f"任务不存在:{media_id}")
        fmt = (fmt or "md").lower()
        path_map = {"md": job.md_path, "txt": job.txt_path, "srt": job.srt_path,
                    "notes": job.notes_path}
        if fmt not in path_map:
            raise ServiceError(f"不支持的格式:{fmt}(可选 md/txt/srt/notes)")
        path = path_map[fmt]
        if not path:
            raise ServiceError(
                f"任务尚无 {fmt} 产物(当前状态:{job.status}"
                + (f",错误:{job.error}" if job.error else "") + ")"
            )
        p = Path(path)
        if not p.exists():
            raise ServiceError(f"产物文件已不存在:{p}")
        text = p.read_text(encoding="utf-8", errors="replace")
        total_chars = len(text)
        truncated = False
        limit = int(max_chars if max_chars is not None else 0)
        if limit > 0 and len(text) > limit:
            text = text[:limit] + f"\n\n…(已截断,原文共 {total_chars} 字符,完整内容见文件)"
            truncated = True
        return {"media_id": media_id, "format": fmt, "path": str(p),
                "chars": len(text), "total_chars": total_chars,
                "truncated": truncated, "content": text}

    def search(self, keyword: str, limit: int = 10,
               context_chars: int = 240, fmt: str = "md") -> dict:
        """在已完成任务的转写稿里做全文检索,返回命中行与上下文。

        AI 的高频诉求是"上次那个视频里讲了 XX",逐个 get_transcript 再自己翻
        既费 token 也容易超限;这里一次把命中点连上下文带 media_id 返回。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            raise ServiceError("缺少参数:keyword")
        limit = max(1, min(int(limit if limit not in (None, "") else 10), 50))
        context_chars = max(40, min(int(context_chars or 240), 2000))
        fmt = (fmt or "md").lower()
        if fmt not in ("md", "txt"):
            fmt = "md"  # srt 是时间轴格式、notes 是讲义,检索正文用 md/txt 才对

        hits: list[dict] = []
        jobs = self.store.list_jobs("")
        for job in jobs:
            if len(hits) >= limit:
                break
            if job.status != "done":
                continue
            path_map = {"md": job.md_path, "txt": job.txt_path}
            p = Path(path_map.get(fmt) or "")
            if not p.exists():
                p = Path(path_map.get("txt" if fmt == "md" else "md") or "")
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if keyword not in text:
                continue
            # 每个 job 只取首个命中,避免长视频把 limit 全吃光
            idx = text.lower().find(keyword.lower())
            if idx < 0:
                continue
            start = max(0, idx - context_chars // 2)
            end = min(len(text), idx + len(keyword) + context_chars // 2)
            snippet = text[start:end].replace("\n", " ").strip()
            hits.append({
                "media_id": job.media_id,
                "title": job.title or job.media_id,
                "path": str(p),
                "position": idx,
                "snippet": ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else ""),
            })
            if len(hits) >= limit:
                break
        return {"keyword": keyword, "count": len(hits), "hits": hits,
                "searched_fmt": fmt}

    def cancel(self, media_id: str) -> dict:
        self.status(media_id)  # 不存在时抛 ServiceError
        self.pipeline.cancel(media_id)
        return self.status(media_id)

    def prewarm(self, wait: bool = False, timeout_sec: float = 1800.0) -> dict:
        """预加载模型。

        wait=False(MCP 默认):只是把加载线程踢起来就返回 —— 服务进程常驻,
        后续 transcribe 直接受益;
        wait=True(CLI 用):阻塞到加载结束再返回 —— CLI 是一次性进程,不等待
        的话 close() 会立刻打断加载,`--prewarm` 变成什么都没做的空操作。
        """
        started = time.monotonic()
        self.pipeline.prewarm()
        payload = {"started": True, "engine": self.settings.engine,
                   "model": self.settings.model_size}
        if not wait:
            return payload
        th = getattr(self.pipeline, "_prewarm_thread", None)
        if th is not None:
            th.join(timeout=max(1.0, float(timeout_sec)))
        transcriber = getattr(self.pipeline, "_transcriber", None)
        payload["loaded"] = bool(getattr(transcriber, "loaded", False))
        payload["waited_sec"] = round(time.monotonic() - started, 1)
        if not payload["loaded"]:
            payload["hint"] = ("模型未在超时内加载完成(可能正在下载权重,见日志);"
                               "预加载只对当前进程有效。")
        return payload

    # ---------- 设置 ----------

    def get_settings(self) -> dict:
        """已持久化的设置;本次进程内的临时覆盖单列在 session_overrides。

        transcribe/提交类的 engine/model 等覆盖只在本进程生效、不写盘,
        以前 get_settings 直接回显了被覆盖后的对象,调用方会误判"配置被改了"。
        """
        data = asdict(self._persisted)
        if self._overrides:
            data["session_overrides"] = dict(self._overrides)
        return data

    def set_settings(self, **changes: Any) -> dict:
        """持久化设置到 data/settings.json(GUI 与后续进程同样生效)。"""
        clean = {k: v for k, v in changes.items() if v is not None}
        unknown = [k for k in clean if k not in OVERRIDABLE_SETTINGS]
        if unknown:
            # 通道开关(enable_cli/enable_mcp)故意落在这里:它们只归桌面端。
            hint = ("(通道开关请在桌面端设置页修改)"
                    if any(k in CHANNEL_SWITCHES.values() for k in unknown) else "")
            raise ServiceError(f"不支持修改的设置项:{', '.join(unknown)}{hint}")
        # 按字段类型强转:防止 AI 客户端传 workers="abc" 之类的坏值写进
        # settings.json,把桌面端下次启动也带崩(与 load_settings 同一套规则)。
        coerced = {k: _coerce_setting(k, v) for k, v in clean.items()}
        warnings = _validate_settings(coerced)
        # 读-改-写:本进程可能是长驻的 MCP 服务,启动后用户在设置页改过的
        # 字段不该被这里用内存里的旧值覆盖回去(通道开关尤其致命 —— 会在
        # 用户关掉 MCP 之后又被一次 update_settings 悄悄打开)。
        base = load_settings(self.settings_path) if self.settings_path.exists() else self._persisted
        self._persisted = replace(base, **coerced)
        self.settings = self._persisted
        self._overrides.clear()
        save_settings(self._persisted, self.settings_path)
        self.pipeline.apply_settings(self.settings)
        payload = {"saved": True, "settings": asdict(self.settings)}
        if warnings:
            payload["warnings"] = warnings
        return payload

    def close(self) -> None:
        try:
            self.pipeline.shutdown()
            for t in getattr(self.pipeline, "_workers", []):
                t.join(timeout=2.0)
        except Exception:  # noqa: BLE001 — 关闭阶段异常不阻塞进程退出
            logger.exception("关闭 pipeline 失败")
        # 仍有任务在跑(如 CLI 被 Ctrl-C)时不要关数据库:worker 可能正写库,
        # 关闭连接会让它抛 "Cannot operate on a closed database";进程退出会回收。
        busy = getattr(getattr(self.pipeline, "_queue", None), "unfinished_tasks", 0) > 0
        if busy:
            logger.warning("仍有任务在执行,跳过关闭数据库(进程退出时回收)")
            return
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            logger.exception("关闭 store 失败")
