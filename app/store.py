"""SQLite 历史索引 + JSON 设置持久化。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

# 任务状态机:queued -> downloading -> converting -> transcribing -> extracting(可选) -> saving -> done / failed
STATUSES = ("queued", "downloading", "converting", "transcribing", "extracting", "saving", "done", "failed")


@dataclass
class Job:
    media_id: str
    bv: str
    title: str
    uploader: str
    duration: float
    url: str
    source_type: str = "url"  # 'url' = B站链接;'local' = 本地上传文件
    part: int = 1
    total_parts: int = 1
    status: str = "queued"
    progress: float = 0.0  # 当前阶段内的进度 0~1
    error: str = ""
    audio_path: str = ""
    srt_path: str = ""
    txt_path: str = ""
    md_path: str = ""
    notes_path: str = ""  # 图文讲义(可选,设置开启且视频源可用时生成)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str = ""

    def to_row(self) -> dict:
        return asdict(self)


class Store:
    """跨线程安全;所有调用方共享一个连接 + 锁。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    media_id TEXT PRIMARY KEY,
                    bv TEXT NOT NULL,
                    title TEXT NOT NULL,
                    uploader TEXT NOT NULL,
                    duration REAL NOT NULL,
                    url TEXT NOT NULL,
                    part INTEGER NOT NULL DEFAULT 1,
                    total_parts INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'queued',
                    progress REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    audio_path TEXT NOT NULL DEFAULT '',
                    srt_path TEXT NOT NULL DEFAULT '',
                    txt_path TEXT NOT NULL DEFAULT '',
                    md_path TEXT NOT NULL DEFAULT '',
                    notes_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "source_type" not in cols:
                self._conn.execute(
                    "ALTER TABLE jobs ADD COLUMN source_type TEXT NOT NULL DEFAULT 'url'"
                )
            if "notes_path" not in cols:
                self._conn.execute(
                    "ALTER TABLE jobs ADD COLUMN notes_path TEXT NOT NULL DEFAULT ''"
                )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(**dict(row))

    def count_jobs(self, status: str) -> int:
        """按状态统计任务数(状态机安全:精确反映哪些任务会被执行)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = ?", (status,)
            ).fetchone()
        return int(row[0])

    def upsert_job(self, job: Job) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO jobs (media_id, bv, title, uploader, duration, url, source_type, part, total_parts,
                                  status, progress, error, audio_path, srt_path, txt_path, md_path, notes_path,
                                  created_at, finished_at)
                VALUES (:media_id, :bv, :title, :uploader, :duration, :url, :source_type, :part, :total_parts,
                        :status, :progress, :error, :audio_path, :srt_path, :txt_path, :md_path, :notes_path,
                        :created_at, :finished_at)
                ON CONFLICT(media_id) DO UPDATE SET
                    status=excluded.status, progress=excluded.progress, error=excluded.error,
                    audio_path=excluded.audio_path, srt_path=excluded.srt_path,
                    txt_path=excluded.txt_path, md_path=excluded.md_path,
                    notes_path=excluded.notes_path,
                    finished_at=excluded.finished_at, title=excluded.title, duration=excluded.duration,
                    source_type=excluded.source_type
                """,
                job.to_row(),
            )

    def get_job(self, media_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE media_id = ?", (media_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, search: str = "") -> list[Job]:
        sql = "SELECT * FROM jobs"
        params: tuple = ()
        if search:
            sql += " WHERE title LIKE ? OR uploader LIKE ? OR bv LIKE ?"
            like = f"%{search}%"
            params = (like, like, like)
        sql += " ORDER BY created_at DESC, part ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_job(r) for r in rows]

    def delete_job(self, media_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM jobs WHERE media_id = ?", (media_id,))

    def clear_finished(self) -> int:
        """清空已结束的任务记录(done/failed/cancelled),返回删除条数;磁盘产物不动。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM jobs WHERE status IN ('done','failed','cancelled')"
            )
        return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------- 设置 ----------------

DEFAULT_SETTINGS = {
    "output_dir": "output",       # 相对项目根,也可填绝对路径
    "model_size": "large-v3-turbo",
    "device": "auto",             # auto / cuda / cpu
    "compute_type": "auto",       # auto / float16 / int8_float16 / int8
    "language": "",               # 空 = 自动检测;可填 zh / en
    "keep_audio": True,
    "vad": True,
    "notes": False,               # 生成图文讲义(PPT 关键帧截图);B站任务会下载视频流
    "engine": "whisper",          # 识别引擎:whisper(默认) / qwen3-asr / sensevoice
    "model_dir": "",              # 模型目录(用户自放模型包);空 = 默认 %LOCALAPPDATA%/Bili Note/models
    "cookies_from_browser": "",   # 浏览器 Cookie(抖音/部分 YouTube 视频需要);空 = 不使用
    "cookie_file": "",            # cookies.txt 文件路径(Netscape 格式,优先于浏览器 Cookie)
    "proxy": "",                  # 网络代理(YouTube 等境外平台需要),如 http://127.0.0.1:7890 或 socks5://…
}


@dataclass
class Settings:
    output_dir: str = DEFAULT_SETTINGS["output_dir"]
    model_size: str = DEFAULT_SETTINGS["model_size"]
    device: str = DEFAULT_SETTINGS["device"]
    compute_type: str = DEFAULT_SETTINGS["compute_type"]
    language: str = DEFAULT_SETTINGS["language"]
    keep_audio: bool = DEFAULT_SETTINGS["keep_audio"]
    vad: bool = DEFAULT_SETTINGS["vad"]
    notes: bool = DEFAULT_SETTINGS["notes"]
    engine: str = DEFAULT_SETTINGS["engine"]
    model_dir: str = DEFAULT_SETTINGS["model_dir"]
    cookies_from_browser: str = DEFAULT_SETTINGS["cookies_from_browser"]
    cookie_file: str = DEFAULT_SETTINGS["cookie_file"]
    proxy: str = DEFAULT_SETTINGS["proxy"]

    def resolve_output_dir(self, base_dir: Path) -> Path:
        p = Path(self.output_dir)
        return p if p.is_absolute() else (base_dir / p)


def _coerce_setting(name: str, value):
    """按字段类型强转设置值,防止损坏的 settings.json(如 keep_audio="false")
    把字符串/数字带进 dataclass,导致后续 Path()/布尔判断崩溃或行为错误。"""
    ftype = Settings.__dataclass_fields__[name].type
    if value is None:
        return Settings.__dataclass_fields__[name].default
    if ftype == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if ftype == "str":
        return str(value)
    return value


def load_settings(path: Path) -> Settings:
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    known = {k: _coerce_setting(k, v) for k, v in data.items() if k in Settings.__dataclass_fields__}
    return Settings(**known)


def save_settings(settings: Settings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
