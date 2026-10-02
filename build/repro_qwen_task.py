"""真实任务路径复现:engine=qwen3-asr + 本地文件,走完整 pipeline。"""
import sys, time, threading
sys.path.insert(0, r"D:/bilibili")
from pathlib import Path
from app.store import Store, load_settings
from app.pipeline import Pipeline
from app.downloader import Downloader
from app import converter

BASE = Path(r"D:/bilibili")
store = Store(BASE / "data" / "history.db")
settings = load_settings(BASE / "data" / "settings.json")
settings.engine = "qwen3-asr"
settings.model_size = "Qwen3-ASR-0.6B"
settings.model_dir = ""          # 默认 %LOCALAPPDATA%/Bili Note/models
settings.device = "cpu"
settings.compute_type = "float32"
settings.language = ""           # 自动
settings.output_dir = r"D:/bilibili/output"

events = []
pipeline = Pipeline(Downloader(), store=store, settings=settings,
                    base_dir=BASE, on_event=events.append)

# 找一个本地音视频
cands = [p for p in (BASE / "uploads").glob("*.mp4")] + [p for p in (BASE / "uploads").glob("*.mp3")]
if not cands:
    print("NO_INPUT"); sys.exit(2)
src = cands[0]
print("INPUT:", src.name, "size:", src.stat().st_size)
print("ffmpeg:", converter.ffmpeg_available())

pipeline.submit_local(src)
deadline = time.time() + 600
job_id = None
while time.time() < deadline:
    time.sleep(1.0)
    if job_id is None:
        # 找刚入队的 job
        for j in store.list_jobs():
            if j.title == src.stem:
                job_id = j.media_id
    if job_id:
        j = store.get_job(job_id)
        if j and j.status in ("done", "failed"):
            print("FINAL_STATUS:", j.status)
            print("ERROR:", (j.error or "")[:600])
            print("MD:", j.md_path)
            break
else:
    print("TIMEOUT")
for e in events[-15:]:
    print("EVT:", e)
