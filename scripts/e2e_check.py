"""端到端验收脚本:真实下载 + 转码 + GPU 转写 + 落盘。"""

import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from app.downloader import Downloader
from app.pipeline import Pipeline
from app.store import Settings, Store

BASE = Path(__file__).resolve().parent.parent
url = sys.argv[1] if len(sys.argv) > 1 else "https://www.bilibili.com/video/BV1GJ411x7h7"

store = Store(BASE / "data" / "history.db")
settings = Settings(output_dir="output", keep_audio=True, model_size="large-v3-turbo")
events: list[str] = []
pipe = Pipeline(downloader=Downloader(), store=store, settings=settings,
                base_dir=BASE, on_event=events.append)

t0 = time.time()
last_stage = ""


def watch():
    global last_stage
    while True:
        jobs = store.list_jobs()
        for j in jobs:
            stage = f"{j.status} {j.progress:.0%}"
            if stage != last_stage:
                last_stage = stage
                print(f"[{time.time()-t0:6.1f}s] {j.title[:30]} -> {stage}", flush=True)
        if jobs and jobs[0].status in ("done", "failed"):
            print(f"[{time.time()-t0:6.1f}s] final: {jobs[0].status} err={jobs[0].error}")
            print("outputs:", jobs[0].srt_path, jobs[0].md_path)
            return
        time.sleep(1)


import threading
threading.Thread(target=pipe._process_url, args=(url,), daemon=True).start()
watch()
