"""端到端验证:真实 Qwen3-ASR-0.6B 模型 + 分块转写 + 进度回报。"""

import sys
import time
from pathlib import Path

from app.qwen_transcriber import QwenASRTranscriber

MODEL_DIR = Path(r"C:\Users\iamly\AppData\Local\Bili Note\models")


def main(wav_path: str) -> None:
    t = QwenASRTranscriber(
        model_size="Qwen3-ASR-0.6B",
        device="cpu",
        compute_type="float32",
        language="zh",
        model_dir=str(MODEL_DIR),
    )
    t0 = time.time()
    print(f"[verify] 加载模型...", flush=True)
    t.load()
    print(f"[verify] 模型加载完成 {time.time()-t0:.1f}s", flush=True)

    progress = []
    segs = t.transcribe(
        Path(wav_path),
        duration=0.0,
        progress=lambda p: progress.append(round(p, 3)),
    )
    print(f"[verify] 转写完成 耗时 {time.time()-t0:.1f}s 片段数={len(segs)}", flush=True)
    print(f"[verify] 进度序列: {progress}", flush=True)
    for s in segs[:5]:
        print(f"  [{s.start:.2f} -> {s.end:.2f}] {s.text}", flush=True)
    if len(segs) > 5:
        print(f"  ... 共 {len(segs)} 段", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
