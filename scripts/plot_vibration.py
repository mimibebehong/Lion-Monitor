"""振动数据绘图脚本：批量生成概览图 PNG。

用法::

    python scripts/plot_vibration.py <文件或目录> [-o 输出目录]

默认输出到 ``reports/figures/``。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lion_monitor.ingest import read_vibration, scan_vibration_dir
from lion_monitor.viz import save_vibration_overview

DEFAULT_OUTDIR = Path(__file__).resolve().parents[1] / "reports" / "figures"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成振动数据概览图")
    parser.add_argument("targets", nargs="+", help=".dat 文件或目录")
    parser.add_argument("-o", "--outdir", default=str(DEFAULT_OUTDIR), help="输出目录")
    args = parser.parse_args(argv)

    outdir = Path(args.outdir)
    files = _collect(args.targets)
    if not files:
        print("没有可处理的文件")
        return 1

    for path in files:
        record = read_vibration(path)
        target = outdir / f"{path.stem}.png"
        saved = save_vibration_overview(record, target)
        print(
            f"[图] {record.meta.channel} {record.meta.sample_rate_hz / 1000:g} kHz "
            f"{record.duration_s:g} s → {saved} ({saved.stat().st_size / 1024:.0f} KiB)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
