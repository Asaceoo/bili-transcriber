"""PyInstaller 运行时导入测试：复现 Qwen3-ASR 打包导入链错误。"""
import sys

try:
    import nagisa
    print("nagisa import OK (Tagger instantiated, data collected)")
except Exception as exc:
    import traceback
    print(f"FAIL nagisa: {exc}")
    traceback.print_exc()
    sys.exit(1)

try:
    from qwen_asr import Qwen3ASRModel
    print("qwen_asr import OK")
except Exception as exc:
    import traceback
    print(f"FAIL qwen_asr: {exc}")
    traceback.print_exc()
    sys.exit(1)

try:
    from transformers.generation import GenerationMixin
    print("GenerationMixin import OK")
except Exception as exc:
    import traceback
    print(f"FAIL GenerationMixin: {exc}")
    traceback.print_exc()
    sys.exit(1)

try:
    from qwen_asr.inference import qwen3_forced_aligner
    print("qwen3_forced_aligner import OK")
except Exception as exc:
    import traceback
    print(f"FAIL qwen3_forced_aligner: {exc}")
    traceback.print_exc()
    sys.exit(1)

print("ALL_IMPORTS_OK")
sys.exit(0)
