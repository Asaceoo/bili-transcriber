"""B站音频本地转写桌面工具。"""

import os
import sys
from pathlib import Path

# Anaconda 等环境可能把 SSL_CERT_FILE 指向不存在的文件,会导致
# httpx(模型下载)初始化失败;启动时清理指向失效路径的证书环境变量。
for _var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
    _path = os.environ.get(_var)
    if _path and not Path(_path).exists():
        del os.environ[_var]

# 大陆网络下 huggingface.co 常直连超时,默认改用镜像站下载模型;
# 需要官方源时自行设置 HF_ENDPOINT 环境变量即可覆盖。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# Xet 下载协议会绕过镜像直连 xethub.hf.co 导致失败,强制走普通 CDN。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# CTranslate2(CUDA)需要的 cuBLAS/cuDNN 通过 pip 轮子安装,
# 位于 site-packages/nvidia/*/bin,须在加载模型前加入 DLL 搜索路径。
if getattr(sys, "frozen", False):
    _dll_base = Path(sys._MEIPASS)
else:
    import sysconfig
    _dll_base = Path(sysconfig.get_paths()["purelib"])

for _sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
    _dll_dir = _dll_base / _sub
    if _dll_dir.is_dir():
        os.add_dll_directory(str(_dll_dir))
        os.environ["PATH"] = str(_dll_dir) + os.pathsep + os.environ.get("PATH", "")
