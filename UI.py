import threading
import time

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import (
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

# ========= BossMode ========
from prompt_toolkit.formatted_text import ANSI

boss_text = ANSI("""
📊 Quarterly Financial Analysis Report

Revenue Growth: 18.7%
Cost Reduction: 6.2%
Net Profit: Stable

Please wait...
""")

boss_container = Window(
    content=FormattedTextControl(boss_text),
    style="bg:#000000 fg:#00ff00",
)
boss_mode = {"enabled": False}

# ========= Buffers =========
log_buffer = Buffer(read_only=False)
biz_buffer = Buffer(read_only=False)
input_buffer = Buffer()
# ========= Log Panel =========
log_panel = HSplit(
    [
        Window(
            FormattedTextControl(" LOG "),
            height=1,
            style="class:title",
        ),
        Window(
            BufferControl(buffer=log_buffer),
            wrap_lines=True,
        ),
    ]
)

# ========= Business Panel =========
biz_panel = HSplit(
    [
        Window(
            FormattedTextControl(" BUSINESS "),
            height=1,
            style="class:title",
        ),
        Window(
            BufferControl(buffer=biz_buffer),
            wrap_lines=True,
        ),
    ],
)

# ========= Input =========
input_panel = VSplit(
        [
            Window(
                FormattedTextControl("> "),
                width=2,
                style="class:prompt",
            ),
            Window(
                BufferControl(buffer=input_buffer),
            ),
        ],
        height=1
    )

# ========= Layout =========
root_container = HSplit(
    [
        VSplit(
            [
                log_panel,   # 左：自适应
                biz_panel,   # 右：固定宽度
            ],
            padding=1,
        ),
        input_panel,
    ]
)

layout = Layout(root_container, focused_element=input_panel.children[1])

# ========= Key Bindings =========
kb = KeyBindings()

@kb.add("enter")
def _(event):
    text = input_buffer.text
    input_buffer.text = ""

    log_buffer.text += f"\n[CMD] {text}"
    biz_buffer.text = f"Last command:\n{text}"
    log_buffer.cursor_position = len(log_buffer.text)

@kb.add("c-c")
@kb.add("c-d")
def _(event):
    event.app.exit()

# boos mode 
@kb.add("f12")
def _(event):
    app = event.app

    if not boss_mode["enabled"]:
        boss_mode["enabled"] = True
        app.layout.container = boss_container
    else:
        boss_mode["enabled"] = False
        app.layout.container = root_container
        # recover focus
        app.layout.focus(input_panel.children[1])

    app.invalidate()
# @kb.add_any()
# def _(event):
#     if boss_mode["enabled"]:
#         event.app.invalidate()
#         return


# ========= Background Thread =========
def background_log(app):
    i = 0
    while True:
        time.sleep(1)
        log_buffer.text += f"\n[LOG] tick {i}"
        log_buffer.cursor_position = len(log_buffer.text)
        i += 1
        app.invalidate()

# ========= App =========
style = Style.from_dict({
    "title": "bg:#444444 fg:white bold",
    "prompt": "fg:green bold",
})

app = Application(
    layout=layout,
    key_bindings=kb,
    style=style,
    full_screen=True,
)

# threading.Thread(
#     target=background_log,
#     args=(app,),
#     daemon=True,
# ).start()

# app.run()
