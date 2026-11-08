"""S4W-1 验收脚本：``window_s`` / ``step_s`` 切窗（频域指标）逐条打印真实结果。

用法（工程根目录）::

    .venv/bin/python scripts/verify_s4w1_window.py

覆盖验收项：
1. 20 s 数据、window_s=2、step_s=2 → 每个频域指标恰好 10 个值（贴时间戳）
2. window_s=5、step_s=2 → 8 个值（末段不足一窗丢弃）
3. window_fn=hanning 与 rect → 数值不同、指纹不同
4. 趋势取数（页面同一入口）按节点+指标取到 10 个点
5. 性能：4 通道 × 20 s 单批导入耗时 / 落库行数
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import numpy as np  # noqa: E402
from _dat_fixture import write_dat  # noqa: E402

from lion_monitor.features import trend_view  # noqa: E402
from lion_monitor.features.metrics import (  # noqa: E402
    compute_metric_series,
    compute_metrics,
    window_plan,
)
from lion_monitor.features.pipeline import import_batch  # noqa: E402
from lion_monitor.params import default_params, params_hash  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store import tasks, tree  # noqa: E402
from lion_monitor.store.db import init_db, make_engine, make_session_factory  # noqa: E402

FS = 32000.0
STAMP = "20261102090000"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}：{detail}")


def _params(**overrides) -> dict:
    return {**default_params("v_RMS"), **overrides}


def _signal(seconds: float, seed: int = 20261102) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(FS * seconds)) / FS
    return (
        0.8 * np.sin(2 * np.pi * 120 * t)
        + 0.2 * np.sin(2 * np.pi * 300 * t)
        + 0.02 * rng.standard_normal(t.size)
    )


def pure_checks() -> None:
    data = _signal(20.0)

    series = compute_metric_series(
        data, FS, params=_params(window_s=2.0, step_s=2.0), codes=["v_RMS"]
    )["v_RMS"]
    offsets = [round(offset, 3) for offset, _value in series]
    check(
        "S4W1-1 20 s / 2 s 窗 → v_RMS 10 点",
        len(series) == 10,
        f"{len(series)} 点；窗中心偏移 {offsets}；值 "
        f"{[round(value, 4) for _offset, value in series]}",
    )

    plan = window_plan(int(FS * 20), FS, _params(window_s=5.0, step_s=2.0))
    series5 = compute_metric_series(
        data, FS, params=_params(window_s=5.0, step_s=2.0), codes=["v_RMS"]
    )["v_RMS"]
    check(
        "S4W1-2 window_s=5 / step_s=2 → 8 点",
        len(series5) == 8,
        f"{len(series5)} 点；窗区间 {[(a // int(FS), b // int(FS)) for a, b in plan]}，"
        f"末段 19~20 s 不足一窗丢弃；窗中心 {[round(o, 2) for o, _v in series5]}",
    )

    hann = compute_metric_series(
        data, FS, params=_params(window_s=2.0, step_s=2.0, window_fn="hanning"), codes=["v_RMS"]
    )["v_RMS"]
    rect = compute_metric_series(
        data, FS, params=_params(window_s=2.0, step_s=2.0, window_fn="rect"), codes=["v_RMS"]
    )["v_RMS"]
    hash_hann = params_hash(_params(window_fn="hanning"), code="v_RMS", sample_rate=FS)
    hash_rect = params_hash(_params(window_fn="rect"), code="v_RMS", sample_rate=FS)
    check(
        "S4W1-3 hanning 与 rect 数值/指纹都不同",
        hann[0][1] != rect[0][1] and hash_hann != hash_rect,
        f"第 1 窗 hanning={hann[0][1]:.6f} / rect={rect[0][1]:.6f}；指纹 {hash_hann} / {hash_rect}",
    )

    # 整段单值接口保持改造前口径（回归金标准）
    check(
        "S4W1-3b 单值接口不套窗（回归口径不变）",
        compute_metrics(_signal(2.0), FS)["v_RMS"]
        == compute_metric_series(
            _signal(2.0), FS, params=_params(window_s=99.0, window_fn="rect"), codes=["v_RMS"]
        )["v_RMS"][0][1],
        "compute_metrics 与 rect 单窗逐位一致",
    )


def _batch_dir(folder: Path, channels: tuple[str, ...]) -> Path:
    t = np.arange(int(FS * 20)) / FS
    samples = (0.8 * np.sin(2 * np.pi * 120 * t) + 0.2 * np.sin(2 * np.pi * 300 * t)).astype(
        "float32"
    )
    for code in channels:
        write_dat(
            folder / f"{STAMP}-{code}_Task1.dat",
            samples=samples,
            channel=code,
            started_at=STAMP,
            sample_rate_hz=FS,
        )
    return folder


def pipeline_checks() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="s4w1-"))
    engine = make_engine(tmp / "verify.db")
    init_db(engine)
    factory = make_session_factory(engine)
    channels = ("CH1", "CH2", "CH3", "CH4")
    folder = _batch_dir(tmp / "批次" / STAMP, channels)

    with factory() as session:
        task = tasks.create_task(session, name="S4W1验收")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
        node_ids = {}
        for code in channels:
            channel = tree.create_node(session, task.id, "channel", code, parent_id=comp.id)
            node_ids[code] = channel.id
            for metric_code in ("v_RMS", "a_Peak", "a_RMS"):
                tree.create_node(
                    session,
                    task.id,
                    "metric",
                    metric_code,
                    parent_id=channel.id,
                    attrs={"code": metric_code},
                )

        started = time.perf_counter()
        result = import_batch(session, task.id, folder, node_ids)
        elapsed_ms = (time.perf_counter() - started) * 1000

        ch1 = node_ids["CH1"]
        vrms_rows = metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=ch1)
        peak_rows = metric_store.list_values(session, task.id, "CH1", "a_Peak", node_id=ch1)
        plotted, decimated = trend_view.chart_points(vrms_rows)
        timestamps = [row.computed_at for row in vrms_rows]

    check(
        "S4W1-4a 单批 4 通道 × 20 s 落库行数",
        result.metrics_written == 4 * 10 + 4 * 2,
        f"metrics_written={result.metrics_written}（每通道 v_RMS 10 行 + a_Peak/a_RMS 各 1 行）；"
        f"状态 {result.status}",
    )
    check(
        "S4W1-4b 趋势取数按节点+指标 10 点且不抽稀",
        len(vrms_rows) == len(plotted) == 10 and not decimated,
        f"取数 {len(vrms_rows)} 点 / 图上 {len(plotted)} 点，抽稀={decimated}；时间戳 {timestamps}",
    )
    check(
        "S4W1-4c 时域指标本轮仍整段 1 点",
        len(peak_rows) == 1,
        f"a_Peak 1 行，computed_at={peak_rows[0].computed_at}",
    )
    check(
        "S4W1-5a 性能：4 通道 × 20 s 单批导入",
        elapsed_ms < 5000,
        f"{elapsed_ms:.0f} ms（改造前同批 12 行 ≈ 586 ms；本轮 48 行，行数 ×4："
        f"v_RMS 每通道 10 点 + 两个时域指标各 1 点）；查询侧索引 P3 已就绪、趋势页 2000 点抽稀不变",
    )


def main() -> int:
    pure_checks()
    pipeline_checks()
    passed = sum(1 for _n, ok, _d in results if ok)
    print(f"\n{passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
