"""pytest 全局初始化：Windows 便携运行时也能 import ``src`` / ``app``。

便携版 Python 带 ``python311._pth``，会忽略 PYTHONPATH；所以这里直接把仓库根目录和
``src`` 插进 ``sys.path``，并给子进程测试准备 UTF-8 输出环境。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for extra in (ROOT / "src", ROOT):
    text = str(extra)
    if text not in sys.path:
        sys.path.insert(0, text)

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
