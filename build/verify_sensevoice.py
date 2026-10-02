"""SenseVoice int8 模型端到端验证:加载 -> 转写 -> 输出时间戳与文本。"""

import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

MODEL_DIR = Path(r"C:\Users\iamly\AppData\Local\Bili Note\models\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17")


def main(wav_path: str) -> None:
    import sherpa_onnx

    t0 = time.time()
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(MODEL_DIR / "model.int8.onnx"),
        tokens=str(MODEL_DIR / "tokens.txt"),
        num_threads=4,
        use_itn=True,
        language="auto",
        debug=False,
    )
    print(f"[加载] 模型加载耗时 {time.time() - t0:.2f}s")

    samples, sr = sf.read(wav_path, dtype="float32")
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    print(f"[音频] {wav_path} sr={sr} 时长={len(samples) / sr:.1f}s")

    # 分块(每块 30s)转写,模拟长音频处理
    chunk_sec = 30
    chunk_len = int(sr * chunk_sec)
    t1 = time.time()
    all_text = []
    for i in range(0, len(samples), chunk_len):
        chunk = samples[i:i + chunk_len]
        stream = recognizer.create_stream()
        stream.accept_waveform(sr, chunk)
        recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        if text:
            all_text.append(text)
        print(f"  [块{i // chunk_len + 1}] {text[:80]}")
    elapsed = time.time() - t1
    print(f"[转写] 耗时 {elapsed:.1f}s, 实时率 RTF={elapsed / (len(samples) / sr):.3f}")
    print(f"[结果] 全文 {len(''.join(all_text))} 字")
    print("".join(all_text))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"D:\bilibili\cache\test_60s.wav")
