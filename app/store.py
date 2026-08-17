"""SQLite 历史索引 + JSON 设置持久化。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

# 任务状态机:queued -> downloading -> converting -> transcribing -> saving -> done / failed
STATUSES = ("queued", "downloading", "converting", "transcribing", "saving", "done", "failed")


@dataclass
class Job:
    media_id: str
    bv: str
    title: str
    uploader: str
    duration: float
    url: str
    part: int = 1
    total_parts: int = 1
    status: str = "queued"
    progress: float = 0.0  # 当前阶段内的进度 0~1
    error: str = ""
    audio_path: str = ""
    srt_path: str = ""
    txt_path: str = ""
    md_path: str = ""
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
                    created_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(**dict(row))

    def upsert_job(self, job: Job) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO jobs (media_id, bv, title, uploader, duration, url, part, total_parts,
                                  status, progress, error, audio_path, srt_path, txt_path, md_path,
                                  created_at, finished_at)
                VALUES (:media_id, :bv, :title, :uploader, :duration, :url, :part, :total_parts,
                        :status, :progress, :error, :audio_path, :srt_path, :txt_path, :md_path,
                        :created_at, :finished_at)
                ON CONFLICT(media_id) DO UPDATE SET
                    status=excluded.status, progress=excluded.progress, error=excluded.error,
                    audio_path=excluded.audio_path, srt_path=excluded.srt_path,
                    txt_path=excluded.txt_path, md_path=excluded.md_path,
                    finished_at=excluded.finished_at, title=excluded.title, duration=excluded.duration
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

    def resolve_output_dir(self, base_dir: Path) -> Path:
        p = Path(self.output_dir)
        return p if p.is_absolute() else (base_dir / p)


def load_settings(path: Path) -> Settings:
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    known = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__}
    return Settings(**known)


def save_settings(settings: Settings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
