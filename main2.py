# -*- coding: utf-8 -*-
"""與美股 main2 對齊：自「今日」起算視窗。"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
sys.argv = ["main.py", "--start-offset", "0"]
import main as _tw_main

_tw_main.main()
