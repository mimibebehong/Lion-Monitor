#!/usr/bin/env python
"""安装器 / 排障用的自检入口（逻辑全在 lion_monitor.deploy.selfcheck）。

安装包最后一步会调它：
    runtime\\python.exe tools\\selfcheck.py --project-root C:\\LionMonitor --json

退出码：0 = 没有失败项；1 = 有失败项（安装器据此弹窗列明细，而不是静默装完）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from lion_monitor.deploy.selfcheck import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
