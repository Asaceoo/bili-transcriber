"""UI 层:页面构建(app.ui.pages)、共享状态(app.ui.state)、动作辅助(app.ui.helpers)。

拆分约定(自 v0.1.35 起,原 main.py 1130 行拆为四块):
- state.py   : 路径常量/全局单例(store、settings、pipeline、events 等)
- helpers.py : 无 UI 布局的动作函数(打开文件夹/删除/重跑/上传落盘等)
- pages.py   : NiceGUI 页面构建(main_page 四页签 + QTable 插槽模板)
- main.py    : 进程入口(日志/托盘/WebView2/启动),并再导出 UI 名字保持兼容
"""
