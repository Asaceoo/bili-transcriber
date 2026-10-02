"""验证 NiceGUI 表格插槽 $emit vs $parent.$emit 是否到达 Python 处理器。"""
from nicegui import ui

log: list[str] = []


@ui.page("/")
def page() -> None:
    ui.label("EMIT TEST")

    table = ui.table(
        columns=[
            {"name": "name", "label": "名称", "field": "name"},
            {"name": "actions", "label": "操作", "field": "actions"},
        ],
        rows=[{"name": "Alice"}, {"name": "Bob"}],
        row_key="name",
    )

    # 方式A: 直接 $emit(当前代码写法)
    table.add_slot("body-cell-actions", '''
        <q-td :props="props">
          <q-btn label="A-直接emit" color="primary"
                 @click="$emit('open-path', props.row.name)" />
          <q-btn label="B-parentEmit" color="secondary"
                 @click="$parent.$emit('open-path', props.row.name)" />
        </q-td>
    ''')

    def on_open(e) -> None:
        log.append(f"收到事件 args={e.args!r}")
        ui.notify(f"收到事件 args={e.args!r}")

    table.on("open-path", on_open)

    count_label = ui.label("事件次数: 0")

    def refresh() -> None:
        count_label.set_text(f"事件次数: {len(log)}")

    ui.timer(0.5, refresh)


ui.run(port=8799, show=False, reload=False)
