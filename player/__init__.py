# -*- coding: utf-8 -*-
"""player 包：原《异键演奏》的指法编译、预调度与自动演奏模块。

模块内部仍是扁平导入（`import midi_compiler` / `import player_config` ...）。
为了让「包导入」和「脚本运行」共用同一份模块对象、避免 `sys.modules` 里出现
两份同名模块，这里先把每个子模块按顶层名导入一次，再把同一个对象登记到
`player.<名字>` 下。之后 `import player.midi_compiler` 拿到的就是同一个对象。
"""
import importlib
import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

# 顺序按依赖从底到上；只导入，不执行任何业务逻辑（各模块都有 main 保护）。
_SUBMODULES = (
    "player_config",
    "chord_map",
    "guitar_plan",
    "modes",
    "plan",
    "game_io",
    "input_engine",
    "midi_loader",
    "midi_adapter",
    "midi_compiler",
    "timeline_runner",
    "recorder",
    "gui_b_legacy",
    "main_b",
)

for _name in _SUBMODULES:
    try:
        _mod = importlib.import_module(_name)
    except Exception:          # 缺 PyQt5 等环境时不影响其它模块
        continue
    sys.modules.setdefault("%s.%s" % (__name__, _name), _mod)
    globals().setdefault(_name, _mod)

del importlib, os, sys
