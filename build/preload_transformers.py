"""PyInstaller runtime hook: 预导入 transformers 懒加载子模块。

qwen-asr 在 import 期通过 `from transformers.generation import GenerationMixin`
引用 transformers 的 lazy module。PyInstaller 默认 modulegraph 分析不到
这些延迟加载的子模块,打包后会出现:

    Could not import module 'GenerationMixin'. Are this object's requirements defined correctly?

本 hook 在应用入口前显式 import 这些子模块,既保证打包时被打入,又在运行时
先实例化 lazy module,避免后续动态导入失败。
"""

try:
    import torch  # noqa: F401
except Exception:
    pass

# 为减小体积我们常排除 scipy/sklearn;但 transformers.generation.candidate_generator
# 在 sklearn 可用时会导入 sklearn.metrics.roc_curve,而 sklearn 又依赖 scipy,导致
# GenerationMixin 懒加载失败。这里强制让 transformers 认为 sklearn 不可用,
# 从而绕过该导入链。Qwen3-ASR 转写本身不需要 sklearn 功能。
try:
    import transformers.utils.import_utils as _iu
    _iu._sklearn_available = False
except Exception:
    pass

# qwen-asr 的强制对齐器依赖 nagisa,而 nagisa 的子模块(prepro/model/
# mecab_system_eval/nagisa_utils)在源码里用「裸 import」(顶层导入)互相引用。
# CPython 中靠 PEP 366 隐式相对导入回退到 nagisa.* 才能成功;PyInstaller 冻结后
# 该回退失效,会报 ModuleNotFoundError: No module named 'prepro'。
# 修复:把 nagisa 包目录加入 sys.path,使裸 import prepro 直接命中 nagisa/prepro.py。
# 关键:用 find_spec 仅定位包目录(不执行 __init__),避免触发 nagisa 内部的循环裸导入
# (若先 import nagisa.prepro 会反过头执行 __init__ -> train.py -> import prepro,死循环)。
try:
    import importlib.util as _ilu
    import os as _os
    import shutil as _shutil
    import sys as _sys

    def _is_ascii_path(_p: str) -> bool:
        try:
            _p.encode("ascii")
            return True
        except UnicodeEncodeError:
            return False

    def _ascii_nagisa_dir(_dir: str) -> str:
        """dynet(C++) 用 std::ifstream 读模型,Windows 下无法读取含非 ASCII(中文)字符的路径。
        应用安装在含中文目录(如 D:\\B站音频本地转写\\)或用户名含中文时,nagisa 加载模型抛
        "Could not read model from ..."。把 nagisa 包目录复制到 ASCII 可写缓存目录,
        让 import nagisa 从缓存解析,模型路径随之变为纯 ASCII。"""
        if _is_ascii_path(_dir):
            return _dir
        _candidates = [
            # 环境变量覆盖(测试/自定义缓存位置用)
            _os.environ.get("BILI_NAGISA_CACHE", ""),
            # C:\Users\Public 是固定 ASCII 路径(显示名才被本地化),标准用户默认可写
            _os.path.join(r"C:\Users\Public", "bili_nagisa_cache"),
            _os.path.join(_os.environ.get("SystemDrive", "C:") + "\\", "bili_nagisa_cache"),
        ]
        for _root in _candidates:
            if not _root:
                continue
            try:
                _os.makedirs(_root, exist_ok=True)
                _target = _os.path.join(_root, "nagisa")
                _src_model = _os.path.join(_dir, "data", "nagisa_v001.model")
                _dst_model = _os.path.join(_target, "data", "nagisa_v001.model")
                # 已缓存且模型存在 -> 复用,避免每次启动复制 40MB
                if not (_os.path.isfile(_dst_model)
                        and _os.path.getsize(_dst_model) == _os.path.getsize(_src_model)):
                    if _os.path.isdir(_target):
                        _shutil.rmtree(_target, ignore_errors=True)
                    _shutil.copytree(_dir, _target)
                return _target
            except Exception:
                continue
        return _dir  # 全部失败则退回原路径(可能仍报错,但不阻塞启动)

    _nagisa_dir = None
    _spec = _ilu.find_spec("nagisa")
    if _spec is not None and _spec.submodule_search_locations:
        _nagisa_dir = list(_spec.submodule_search_locations)[0]
    if _nagisa_dir is None:
        # 兜底:扫描 sys.path 找 nagisa 包目录(PyInstaller 会把解压目录加入 sys.path)
        for _p in _sys.path:
            _cand = _os.path.join(_p, "nagisa", "__init__.py")
            if _os.path.isfile(_cand):
                _nagisa_dir = _os.path.join(_p, "nagisa")
                break
    if _nagisa_dir:
        _nagisa_dir = _ascii_nagisa_dir(_nagisa_dir)
        # 父目录入 sys.path 使 `import nagisa` 命中缓存副本(ASCII 路径时父目录已在
        # sys.path 上,not in 检查会跳过);包目录本身入 sys.path 供 nagisa 内部裸导入
        # (import prepro/model/...) 命中。
        _nagisa_parent = _os.path.dirname(_nagisa_dir)
        if _nagisa_parent not in _sys.path:
            _sys.path.insert(0, _nagisa_parent)
        if _nagisa_dir not in _sys.path:
            _sys.path.insert(0, _nagisa_dir)
except Exception:
    # 运行时不一定启用 Qwen3-ASR,失败不应阻塞应用启动
    pass

try:
    import transformers  # noqa: F401
    import transformers.activations  # noqa: F401
    import transformers.cache_utils  # noqa: F401
    import transformers.generation  # noqa: F401
    import transformers.generation.beam_constraints  # noqa: F401
    import transformers.generation.beam_search  # noqa: F401
    import transformers.generation.candidate_generator  # noqa: F401
    import transformers.generation.configuration_utils  # noqa: F401
    import transformers.generation.logits_process  # noqa: F401
    import transformers.generation.stopping_criteria  # noqa: F401
    import transformers.generation.streamers  # noqa: F401
    import transformers.generation.utils  # noqa: F401
    import transformers.generation.watermarking  # noqa: F401
    import transformers.integrations  # noqa: F401
    import transformers.masking_utils  # noqa: F401
    import transformers.modeling_flash_attention_utils  # noqa: F401
    import transformers.modeling_layers  # noqa: F401
    import transformers.modeling_outputs  # noqa: F401
    import transformers.modeling_rope_utils  # noqa: F401
    import transformers.modeling_utils  # noqa: F401
    import transformers.processing_utils  # noqa: F401
    import transformers.utils  # noqa: F401
    import transformers.utils.deprecation  # noqa: F401
    import transformers.utils.generic  # noqa: F401
except Exception:
    # 运行时不一定启用 Qwen3-ASR,失败不应阻塞应用启动
    pass
