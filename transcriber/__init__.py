# -*- coding: utf-8 -*-
"""transcriber 包：原《异键演奏》的转录与 MIDI 转换流水线。

项目根为上一级目录；本包内部模块优先走包内相对导入（`from . import convert`），
直接 `python transcriber/pipeline.py` 运行时自动退回扁平导入，
因此包导入与脚本运行都不会产生两份同名模块。
"""
import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
