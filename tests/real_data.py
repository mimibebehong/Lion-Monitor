"""现场实测 .dat 样本的位置（Windows 测试机）。

默认指向本仓库旁的测试数据；换机器时用环境变量 ``LION_TEST_DATA_DIR`` 覆盖。
找不到数据时，相关测试会跳过，而不是把整份测试跑红。
"""

from __future__ import annotations

import os
from pathlib import Path

REAL_DIR = Path(
    os.environ.get(
        "LION_TEST_DATA_DIR",
        r"D:\code_python\Project\Lion-Monitor\data\四通道保存",
    )
)

#: DS-20261029-001 的 CH2（包络谱 145/290 Hz 特征实测出现在这一条）
REAL_CH2_001 = REAL_DIR / "2026-10-29 15_28_34" / "2026-10-29 15_28_34-CH2_Task1.dat"
#: DS-20261029-002 的 CH2
REAL_CH2_002 = REAL_DIR / "2026-10-29 16_03_13" / "2026-10-29 16_03_13-CH2_Task1.dat"
