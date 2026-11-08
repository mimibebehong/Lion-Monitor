"""振动 .dat 文件巡检脚本：打印元数据与幅值统计。

用法::

    python scripts/inspect_vibration.py <文件或目录> [更多路径...]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from lion_monitor.ingest.vibration import read_vibration, scan_vibration_dir


def _collect(targets: list[str]) -> list[Path]:
    files: list[Path] = []
    for t in targets:
        p = Path(t)
        if p.is_dir():
            files.extend(m.path for m in scan_vibration_dir(p))
        elif p.is_file():
            files.append(p)
        else:
            print(f"[跳过] 路径不存在: {p}")
    return files


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    for path in _collect(argv):
        record = read_vibration(path)
        meta = record.meta
        x = record.samples.astype(np.float64)
        rms = float(np.sqrt(np.mean(x**2)))
        peak = float(np.max(np.abs(x)))
        print(f"文件      : {meta.path.name}")
        print(f"  站点/设备: {meta.site} / {meta.device_ip}")
        print(f"  通道     : {meta.channel}   规则: {meta.rule}   头版本: {meta.format_version}")
        print(f"  起始时间 : {meta.started_at:%Y-%m-%d %H:%M:%S}")
        print(
            f"  采样率   : {meta.sample_rate_hz:g} Hz   "
            f"点数: {meta.sample_count}   时长: {meta.duration_s:g} s"
        )
        print(
            f"  幅值     : min={x.min():+.4f}  max={x.max():+.4f}  "
            f"mean={x.mean():+.4f}  RMS={rms:.4f}  峰值={peak:.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
