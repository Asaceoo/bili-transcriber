"""单文件便携版专用运行时 hook:默认以浏览器模式启动。

pywebview 在 PyInstaller --onefile 的解压环境里初始化原生 WebView2 控件
时容易静默失败(白屏、不报错),因为原生控件依赖的 .NET/WebView2 DLL 在
解压临时目录下的加载链路脆弱。后端 HTTP 服务本身完全正常,因此单文件版
默认改用浏览器模式(打开默认浏览器访问本地 UI),渲染 100% 可靠。

如需原生窗口,可在启动前设置环境变量 BILI_FORCE_NATIVE=1 覆盖本默认。
"""
import os

os.environ.setdefault("BILI_FORCE_BROWSER", "1")
