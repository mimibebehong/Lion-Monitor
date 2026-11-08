"""G12 验收脚本：不足一窗的样本直接判无效（不出值 + 具体原因）。

用法（工程根目录）::

    .venv/bin/python scripts/verify_g12_short_sample.py

覆盖验收项（docs/kanban/bodies/g12_brief.md）：
1. 32 点样本 → 0 个值，原因含「32 点」「不足一窗」「64000」
2. 32000 点（1 s，不足 2 s 窗）→ 0 个值
3. 64000 点（恰好 2 s 窗）→ 1 个值
4. 128000 点（4 s，2 s 窗 2 s 步）→ 2 个值
5. 日志文件能看到跳过原因（页面侧由 AppTest 用例 test_page_ingest_short_sample.py 覆盖）
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import numpy as np  # noqa: E402
from _dat_fixture import write_dat  # noqa: E402

from lion_monitor.features.metrics import (  # noqa: E402
    compute_metric_series,
    short_sample_reason,
    window_plan,
)
from lion_monitor.features.pipeline import import_batch  # noqa: E402
from lion_monitor.logging_setup import setup_logging  # noqa: E402
from lion_monitor.params import default_params  # noqa: E402
from lion_monitor.store import metric_assign, tasks, tree  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store.db import init_db, make_engine, make_session_factory  # noqa: E402

FS = 32000.0
STAMP = "20261102090000"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}：{detail}")


def _params(**overrides) -> dict:
    return {**default_params("v_RMS"), **overrides}


def _signal(points: int, freq: float = 100.0) -> np.ndarray:
    t = np.arange(points) / FS
    return np.sin(2 * np.pi * freq * t)


def _batch(folder: Path, points: int) -> Path:
    write_dat(
        folder / f"{STAMP}-CH1_Task1.dat",
        samples=_signal(points).astype("float32"),
        channel="CH1",
        started_at=STAMP,
        sample_rate_hz=FS,
    )
    return folder


def _task(session, *, v_rms_window: float | None = None):
    task = tasks.create_task(session, name=f"G12验收-{v_rms_window}")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    node = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    for code in ("v_RMS", "a_Peak", "a_RMS"):
        tree.create_node(session, task.id, "metric", code, parent_id=node.id, attrs={"code": code})
    if v_rms_window is not None:
        metric_assign.assign_metric(
            session, task.id, node.id, "v_RMS", params={"window_s": v_rms_window}
        )
    return task, {"CH1": node.id}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="g12-"))
    engine = make_engine(tmp / "g12.db")
    init_db(engine)
    factory = make_session_factory(engine)
    params = _params(window_s=2.0, step_s=2.0)

    print("=== 1~4 计算层（window_plan / compute_metric_series） ===")
    for points, expected in ((32, 0), (int(FS), 0), (int(FS * 2), 1), (int(FS * 4), 2)):
        plan = window_plan(points, FS, params)
        series = compute_metric_series(_signal(points), FS, params=params)["v_RMS"]
        reason = short_sample_reason(points, FS, params)
        check(
            f"{points} 点 → {expected} 个值",
            len(series) == expected and len(plan) == expected,
            f"窗数 {len(plan)}、v_RMS 点数 {len(series)}、原因「{reason or '—'}」",
        )

    reason = short_sample_reason(32, FS, params)
    check(
        "32 点原因文案含「32 点」「不足一窗」「64000」",
        all(token in reason for token in ("32 点", "不足一窗", "64000")),
        reason,
    )

    print("\n=== 1~4 管线侧（import_batch → 落库行数 / 文件状态 / 跳过明细） ===")
    with factory() as session:
        task, nodes = _task(session)
        for points, expected in ((32, 0), (int(FS), 0), (int(FS * 2), 3), (int(FS * 4), 6)):
            folder = _batch(tmp / f"批次{points}" / STAMP, points)
            result = import_batch(session, task.id, folder, nodes)
            written = len(metric_store.list_values(session, task.id, "CH1"))
            short = [
                item
                for item in (result.skipped_short or [])
                if item.filename.startswith(STAMP) and item.node_id == nodes["CH1"]
            ]
            detail = (
                f"本批 metrics_written={result.metrics_written}、文件 files_ok={result.files_ok}、"
                f"状态「{result.status}」、skipped_short={len(short)} 条"
            )
            if short:
                detail += f"、原因「{short[0].reason}」"
            if not short and result.files_bad:
                detail += f"、判无效原因「{result.files_bad[0].reason}」"
            # 期望：32/1s → 0 行 + 判无效；64000 → 3 行（3 指标 × 1 窗）；128000 → 6 行
            check(
                f"{points} 点管线 → 本批 {expected} 行、累计 CH1 {written} 行",
                result.metrics_written == expected,
                detail,
            )

    print("\n=== 第 3 项补充：窗长按节点生效参数（v_RMS 授 1 s 窗） ===")
    with factory() as session:
        task, nodes = _task(session, v_rms_window=1.0)
        folder = _batch(tmp / "批次节点窗长" / STAMP, int(FS))
        result = import_batch(session, task.id, folder, nodes)
        short_codes = sorted(item.code for item in (result.skipped_short or []))
        check(
            "1 s 文件 + v_RMS 窗 1 s：v_RMS 出 1 值，a_Peak/a_RMS 仍按 2 s 判短",
            result.metrics_written == 1 and short_codes == ["a_Peak", "a_RMS"],
            f"metrics_written={result.metrics_written}、被跳过指标={short_codes}",
        )

    print("\n=== 5 日志文件（logs/lion-monitor.log） ===")
    log_file = setup_logging(log_dir=tmp / "logs")
    try:
        with factory() as session:
            task, nodes = _task(session)
            folder = _batch(tmp / "批次日志" / STAMP, 32)
            import_batch(session, task.id, folder, nodes)
        lines = [
            line.strip()
            for line in log_file.read_text(encoding="utf-8").splitlines()
            if "不足一窗" in line
        ]
        for line in lines:
            print("  " + line)
        check("日志文件有跳过原因", bool(lines), f"{log_file} 命中 {len(lines)} 行")
    finally:
        root_logger = logging.getLogger()
        for handler in [h for h in root_logger.handlers if getattr(h, "_lion_logging_file", False)]:
            root_logger.removeHandler(handler)
            handler.close()

    passed = sum(1 for _name, ok, _detail in results if ok)
    print(f"\n合计 {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
