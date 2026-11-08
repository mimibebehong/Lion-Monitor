"""MISC（Q5 + Q9）验收脚本：逐条打印真实结果，全过才 exit 0。

用法（工程根目录）::

    .venv/bin/python scripts/verify_misc_q5_q9.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import numpy as np  # noqa: E402

from lion_monitor.features.metrics import compute_metrics  # noqa: E402
from lion_monitor.params import (  # noqa: E402
    BAND_LOW_FLOOR_HZ,
    effective_params,
    prepare_params,
    validate_params,
)
from lion_monitor.store import tasks, tree  # noqa: E402
from lion_monitor.store.bootstrap import bootstrap_example_task  # noqa: E402

FS = 32000.0
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}：{detail}")


def _params(low: float, window: float = 2.0) -> dict:
    return {"window_s": window, "step_s": window, "band_low": low, "band_high": 1000.0}


def q5_checks() -> None:
    tmp = Path(tempfile.mkdtemp()) / "empty.db"
    os.environ["LION_DB"] = str(tmp)
    import store_ctx

    store_ctx._session_factory.clear()
    with store_ctx.open_session() as session:
        rows = tasks.list_tasks(session)
    check("Q5-1 空库首次打开任务数", len(rows) == 0, f"任务数 {len(rows)}")

    with store_ctx.open_session() as session:
        created = bootstrap_example_task(session)
        nodes = tree.list_nodes(session, created.id) if created else []
    check(
        "Q5-2 显式点「恢复内置示例树」",
        created is not None and len(nodes) == 21,
        f"任务「{created.name if created else '—'}」节点数 {len(nodes)}",
    )
    store_ctx._session_factory.clear()
    os.environ.pop("LION_DB", None)


def q9_checks() -> None:
    problems = validate_params(_params(0.1), sample_rate=FS)
    check(
        "Q9-1 band_low=0.1 保存被拒（中文原因）",
        any("带通下限" in text for text in problems),
        "；".join(problems) or "（无问题）",
    )

    prepared, problems2, note = prepare_params(_params(2.0), sample_rate=FS)
    check(
        "Q9-2 band_low=2 + 窗长 2 s 自动抬到 5 Hz 并提示",
        not problems2 and prepared["band_low"] == 5.0 and bool(note),
        f"生效 band_low={prepared['band_low']}；提示「{note}」",
    )

    prepared10, problems10, note10 = prepare_params(_params(10.0), sample_rate=FS)
    check(
        "Q9-3 band_low=10 不受影响",
        not problems10 and prepared10["band_low"] == 10.0 and note10 is None,
        f"生效 band_low={prepared10['band_low']}；提示 {note10}",
    )

    legacy = effective_params({"band_low": 0.1, "band_high": 1000.0, "window_s": 2.0}, FS)
    check(
        "Q9-4 历史/定义默认里的 0.1 在生效层抬到 5 Hz",
        legacy["band_low"] == 5.0,
        f"生效 band_low={legacy['band_low']}（硬下限 {BAND_LOW_FLOOR_HZ:g} Hz）",
    )

    t = np.arange(int(FS * 2.0)) / FS
    rng = np.random.default_rng(7)
    signal = (
        0.5 * np.sin(2 * np.pi * 1.5 * t)
        + 0.2 * np.sin(2 * np.pi * 100.0 * t)
        + 0.01 * rng.standard_normal(t.size)
    )
    guarded = compute_metrics(signal, FS, _params(0.1), codes=["v_RMS"])["v_RMS"]
    reference = compute_metrics(signal, FS, _params(5.0), codes=["v_RMS"])["v_RMS"]
    check(
        "Q9-5 合成数据 v_RMS 不再荒谬（<10 mm/s 且与 5 Hz 口径一致）",
        0.0 < guarded < 10.0 and guarded == reference,
        f"band_low=0.1 参数 → v_RMS={guarded:.3f} mm/s；参照 5 Hz={reference:.3f} mm/s",
    )


def main() -> int:
    print(f"band_low 硬下限 = {BAND_LOW_FLOOR_HZ:g} Hz")
    q5_checks()
    q9_checks()
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n合计 {len(results)} 项，PASS {len(results) - len(failed)}，FAIL {len(failed)}")
    if failed:
        print("失败项：" + "；".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
