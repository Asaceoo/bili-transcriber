from pathlib import Path

from app.store import DEFAULT_SETTINGS, Job, Settings, Store, load_settings, save_settings


def make_job(media_id="BV1abc_p1", title="标题A", status="done"):
    return Job(media_id=media_id, bv=media_id.split("_")[0], title=title,
               uploader="UP", duration=120.0, url="https://www.bilibili.com/video/BV1abc",
               status=status)


def test_upsert_and_get(tmp_path: Path):
    store = Store(tmp_path / "t.db")
    job = make_job()
    store.upsert_job(job)
    got = store.get_job("BV1abc_p1")
    assert got is not None and got.title == "标题A" and got.status == "done"

    job.status, job.progress = "transcribing", 0.5
    store.upsert_job(job)  # 更新已有记录
    got = store.get_job("BV1abc_p1")
    assert got.status == "transcribing" and got.progress == 0.5


def test_list_and_search_and_delete(tmp_path: Path):
    store = Store(tmp_path / "t.db")
    store.upsert_job(make_job("BV1a_p1", "Python 教程"))
    store.upsert_job(make_job("BV2_p1", "做饭视频", ))
    assert len(store.list_jobs()) == 2
    hits = store.list_jobs("python")
    assert len(hits) == 1 and hits[0].bv == "BV1a"
    store.delete_job("BV1a_p1")
    assert store.get_job("BV1a_p1") is None
    assert len(store.list_jobs()) == 1


def test_settings_roundtrip(tmp_path: Path):
    p = tmp_path / "settings.json"
    save_settings(Settings(output_dir="X:/notes", model_size="small", keep_audio=False), p)
    loaded = load_settings(p)
    assert loaded.output_dir == "X:/notes" and loaded.model_size == "small" and loaded.keep_audio is False


def test_settings_defaults_engine_and_model_dir():
    s = Settings()
    assert s.engine == "whisper"
    assert s.model_dir == ""


def test_settings_roundtrip_new_fields(tmp_path: Path):
    p = tmp_path / "settings.json"
    save_settings(Settings(engine="qwen3-asr", model_dir="D:/models"), p)
    loaded = load_settings(p)
    assert loaded.engine == "qwen3-asr"
    assert loaded.model_dir == "D:/models"


def test_settings_ignores_garbage(tmp_path: Path):
    p = tmp_path / "settings.json"
    p.write_text('{"unknown_key": 1, "model_size": "tiny", "corrupted": ', encoding="utf-8")  # 坏 JSON
    loaded = load_settings(p)
    assert loaded.model_size == DEFAULT_SETTINGS["model_size"]  # 解析失败回退默认
    assert loaded.vad is True


def test_settings_coerces_wrong_types(tmp_path: Path):
    """损坏的 settings.json(类型错误/null)不应导致启动崩溃或错误行为。"""
    p = tmp_path / "settings.json"
    p.write_text(
        '{"keep_audio": "false", "vad": "true", "output_dir": 123, "model_size": null}',
        encoding="utf-8",
    )
    loaded = load_settings(p)
    assert loaded.keep_audio is False      # "false" 字符串 -> False
    assert loaded.vad is True              # "true" 字符串 -> True
    assert loaded.output_dir == "123"      # 数字 -> 字符串
    assert loaded.model_size == DEFAULT_SETTINGS["model_size"]  # null -> 默认值


def test_resolve_output_dir_relative(tmp_path: Path):
    s = Settings(output_dir="out")
    assert s.resolve_output_dir(tmp_path) == tmp_path / "out"
    s.output_dir = "D:/abs/path"
    assert s.resolve_output_dir(tmp_path) == Path("D:/abs/path")


def test_notes_setting_roundtrip_and_default(tmp_path: Path):
    p = tmp_path / "settings.json"
    assert Settings().notes is False
    save_settings(Settings(notes=True), p)
    loaded = load_settings(p)
    assert loaded.notes is True


def test_notes_path_column_persisted(tmp_path: Path):
    store = Store(tmp_path / "t.db")
    job = make_job()
    job.notes_path = r"D:\out\BV1abc_合集\标题A.notes.md"
    store.upsert_job(job)
    got = store.get_job("BV1abc_p1")
    assert got is not None and got.notes_path.endswith("标题A.notes.md")

    job.notes_path = ""  # 更新可清空
    store.upsert_job(job)
    assert store.get_job("BV1abc_p1").notes_path == ""


def test_notes_path_migrated_from_legacy_db(tmp_path: Path):
    """旧库(无 notes_path 列)打开时自动迁移,旧行 notes_path 为空串。"""
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE jobs (
            media_id TEXT PRIMARY KEY, bv TEXT NOT NULL, title TEXT NOT NULL,
            uploader TEXT NOT NULL, duration REAL NOT NULL, url TEXT NOT NULL,
            part INTEGER NOT NULL DEFAULT 1, total_parts INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'queued', progress REAL NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '', audio_path TEXT NOT NULL DEFAULT '',
            srt_path TEXT NOT NULL DEFAULT '', txt_path TEXT NOT NULL DEFAULT '',
            md_path TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '', source_type TEXT NOT NULL DEFAULT 'url'
        )"""
    )
    conn.execute(
        "INSERT INTO jobs (media_id, bv, title, uploader, duration, url, created_at)"
        " VALUES ('BV1old', 'BV1old', '旧任务', 'UP', 60.0, 'https://x', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    store = Store(db)  # 打开即迁移
    got = store.get_job("BV1old")
    assert got is not None and got.notes_path == ""
    # 迁移后可正常写入新列
    got.notes_path = "D:/x/old.notes.md"
    store.upsert_job(got)
    assert store.get_job("BV1old").notes_path == "D:/x/old.notes.md"
