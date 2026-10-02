"""Qwen3-ASR 端到端冒烟测试:加载用户模型目录 + 转写 3s wav。"""
import sys, time
from pathlib import Path
sys.path.insert(0, r"D:/bilibili")
from app.qwen_transcriber import QwenASRTranscriber
from app.models import resolve_model_dir

d = resolve_model_dir("")
print("model dir:", d)
t = QwenASRTranscriber(model_size="Qwen3-ASR-0.6B", device="cpu",
                       compute_type="float32", language="zh", model_dir="")
t0 = time.time()
t.load()
print("LOADED device=%s in %.1fs" % (t.resolved_device, time.time() - t0))
segs = t.transcribe(Path(r"C:/Users/iamly/AppData/Local/Temp/bili_qwen_test/smoke.wav"), duration=3.0)
print("SEGMENTS:", segs)
print("SMOKE_OK segments=%d" % len(segs))
