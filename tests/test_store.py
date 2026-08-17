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


def test_settings_ignores_garbage(tmp_path: Path):
    p = tmp_path / "settings.json"
    p.write_text('{"unknown_key": 1, "model_size": "tiny", "corrupted": ', encoding="utf-8")  # 坏 JSON
    loaded = load_settings(p)
    assert loaded.model_size == DEFAULT_SETTINGS["model_size"]  # 解析失败回退默认
    assert loaded.vad is True


def test_resolve_output_dir_relative(tmp_path: Path):
    s = Settings(output_dir="out")
    assert s.resolve_output_dir(tmp_path) == tmp_path / "out"
    s.output_dir = "D:/abs/path"
    assert s.resolve_output_dir(tmp_path) == Path("D:/abs/path")
