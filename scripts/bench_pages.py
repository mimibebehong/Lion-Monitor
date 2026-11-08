"""性能基准（P3 / S2 + G14）：临时库里造 N 行指标值，按三条页面取数路径实测耗时。

用法::

    .venv/bin/python scripts/bench_pages.py                     # 24 万行，各路径跑 1 次
    .venv/bin/python scripts/bench_pages.py --rows 240000 --repeat 3 --json reports/bench.json
    .venv/bin/python scripts/bench_pages.py --keep              # 保留临时库，手工翻查用
    .venv/bin/python scripts/bench_pages.py --rows 1200         # 小库对照

口径（与页面同一份业务代码，不改业务行为）：

- **工作台**：``features.overview.task_overview`` —— 首页一次渲染要的全部数字
  （任务/数据集/九步/健康分布/设备总览/超限逐点）。
- **健康树**：``store.tree.list_nodes`` + ``store.tree.tree_snapshot``（健康树配置页取数）；
  另单列 ``features.health.node_health``（树状态 / 趋势页评级用的整树判级）。
- **趋势页**：``store.metrics.list_values``（单指标、按通道节点、默认近 30 天窗）
  + ``features.trend_view.chart_points``（min/max 抽稀到 ≤ 2 000 点）与判级建图（图上点数）
  + ``app/trend_rows.detail_rows``（导出 CSV 的明细行）。CSV 始终是全量原始点。

临时库建在系统临时目录里，跑完即删（``--keep`` 时保留并打印路径），不会写进 data/。
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "app"))

import trend_rows  # noqa: E402
from sqlalchemy import text  # noqa: E402

from lion_monitor.features import overview, trend_view  # noqa: E402
from lion_monitor.features.health import node_health, point_level  # noqa: E402
from lion_monitor.store import datasets, limits, tasks, tree  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store.db import init_db, make_engine, make_session_factory  # noqa: E402

FS = 32000.0
CHANNELS = ("CH1", "CH2", "CH3", "CH4")
METRICS = ("v_RMS", "a_Peak", "a_RMS")
UNIT_OF = {"v_RMS": "mm/s", "a_Peak": "g", "a_RMS": "g"}
WARN, ALERT = 7.0, 8.5
OBJECTS = ("隔膜过辊轴承", "入片牵引电机")
TIME_FMT = "%Y-%m-%d %H:%M:%S"

#: 造数用一条 INSERT，走 executemany；列与 models.MetricValue 对齐
_INSERT_VALUE = text(
    "INSERT INTO metric_value"
    " (channel_id, node_id, code, value, unit, level, computed_at,"
    "  params_hash, rpm_source, stale)"
    " VALUES (:channel_id, :node_id, :code, :value, :unit, 'unset', :computed_at,"
    "  '', '', 0)"
)


# ---------------------------------------------------------------- 造数据


def _value_at(index: int) -> float:
    """确定性波形（约 1.6 ~ 8.4）：慢漂移 + 快波动，够造出一批越限点。"""
    return 5.0 + 2.4 * math.sin(index / 97.0) + 1.0 * math.sin(index / 7.0)


def build_tree(session, task_name: str) -> dict:
    """任务 → 设备 → 2 监测对象 → 每对象 2 通道 → 每通道 3 指标；指标节点上都设阈值。"""
    task = tasks.create_task(session, name=task_name, operator="bench")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    components = [
        tree.create_node(session, task.id, "component", name, parent_id=device.id)
        for name in OBJECTS
    ]

    channels: list[tuple[str, int]] = []
    metrics: dict[tuple[str, str], int] = {}
    for index, code in enumerate(CHANNELS):
        parent = components[index % len(components)]
        channel = tree.create_node(session, task.id, "channel", code, parent_id=parent.id)
        channels.append((code, channel.id))
        for metric_code in METRICS:
            node = tree.create_node(
                session,
                task.id,
                "metric",
                metric_code,
                parent_id=channel.id,
                attrs={"code": metric_code, "单位": UNIT_OF[metric_code]},
            )
            limits.set_limit(session, task.id, node.id, metric_code, WARN, ALERT)
            metrics[(code, metric_code)] = node.id
    return {"task": task, "channels": channels, "metrics": metrics}


def seed_values(
    session,
    task_id: int,
    channel_ids: dict[str, int],
    metrics: dict[tuple[str, str], int],
    rows: int,
    batches: int,
    span_days: int,
) -> tuple[int, dict]:
    """按「批 × 通道 × 指标」造 rows 行指标值；返回实际写入行数与布局说明。

    值按**通道节点**落库（方案 A：``metric_value.node_id`` 是通道节点 id）。
    """
    series = len(CHANNELS) * len(METRICS)
    per_series, series_extra = divmod(rows, series)
    per_batch, batch_extra = divmod(per_series, batches)
    counts = [per_batch + (1 if index < batch_extra else 0) for index in range(batches)]
    starts: list[int] = []
    cursor = 0
    for count in counts:
        starts.append(cursor)
        cursor += count

    base = datetime.now().replace(microsecond=0) - timedelta(days=span_days)
    step = timedelta(seconds=span_days * 86400 / max(per_series, 1))
    dataset_of: dict[int, int] = {}
    file_of: dict[tuple[int, str], int] = {}
    written = 0

    series_list = [(channel, metric) for channel in CHANNELS for metric in METRICS]
    for order, (channel, metric) in enumerate(series_list):
        # 值按通道节点落库（P2 / 方案 A）：指标节点读的正是所属通道节点的行
        value_node_id = channel_ids[channel]
        extra_here = 1 if order < series_extra else 0
        for batch, (start, count) in enumerate(zip(starts, counts, strict=True)):
            total = count + (extra_here if batch == 0 else 0)
            if batch not in dataset_of:
                stamp = (base + step * start).strftime(TIME_FMT)
                row = datasets.create_dataset(
                    session,
                    task_id,
                    f"基准批次{batch + 1:02d}",
                    f"/tmp/lion-bench/b{batch + 1}",
                    stamp,
                )
                dataset_of[batch] = row.id
            key = (batch, channel)
            if key not in file_of:
                row = datasets.add_file(
                    session,
                    dataset_of[batch],
                    f"/tmp/lion-bench/b{batch + 1}/{channel}.dat",
                    channel,
                    FS,
                    total * 512,
                )
                file_of[key] = row.id
            payload = [
                {
                    "channel_id": file_of[key],
                    "node_id": value_node_id,
                    "code": metric,
                    "value": _value_at(start + index),
                    "unit": UNIT_OF[metric],
                    "computed_at": (base + step * (start + index)).strftime(TIME_FMT),
                }
                for index in range(total)
            ]
            session.execute(_INSERT_VALUE, payload)
            session.commit()
            written += len(payload)

    layout = {
        "series": series,
        "batches": batches,
        "channels": len(CHANNELS),
        "metrics": len(METRICS),
        "points_per_series": per_series + (1 if series_extra else 0),
        "span_days": span_days,
    }
    return written, layout


# ---------------------------------------------------------------- 计时


def _timed(fn, repeat: int) -> tuple[Any, dict]:
    """跑 repeat 次，返回最后一次的产出与耗时统计（毫秒）。"""
    samples: list[float] = []
    value: Any = None
    for _ in range(repeat):
        start = time.perf_counter()
        value = fn()
        samples.append((time.perf_counter() - start) * 1000)
    stats = {
        "median": round(statistics.median(samples), 1),
        "min": round(min(samples), 1),
        "max": round(max(samples), 1),
        "samples": [round(item, 1) for item in samples],
    }
    return value, stats


def run_bench(session, task_id: int, trend_target: tuple[str, int, str, int], repeat: int) -> dict:
    """量三条页面路径；trend_target = (通道号, 通道节点 id, 指标码, 指标节点 id)。"""
    channel, channel_node_id, code, metric_node_id = trend_target

    dashboard, dashboard_ms = _timed(lambda: overview.task_overview(session, task_id), repeat)

    def tree_page():
        nodes = tree.list_nodes(session, task_id)
        snapshot = tree.tree_snapshot(session, task_id)
        return nodes, snapshot

    (nodes, _snapshot), tree_ms = _timed(tree_page, repeat)
    health, health_ms = _timed(lambda: node_health(session, task_id), repeat)

    def trend_values():
        # 通道号在本任务下唯一 → 老数据（node_id 为空）一并带上，与页面口径一致；
        # 时间窗与页面同一个默认值（Q6：最近 30 天）
        return metric_store.list_values(
            session,
            task_id,
            channel,
            code,
            node_id=channel_node_id,
            include_unattributed=True,
            since=trend_view.window_start(trend_view.DEFAULT_WINDOW_DAYS),
        )

    rows, values_ms = _timed(trend_values, repeat)
    limit = limits.resolve_limit(session, task_id, metric_node_id, code)

    def chart_build():
        # 页面建图路径：先抽稀再判级（Q6），图上点数 ≤ DEFAULT_MAX_POINTS
        plotted, decimated = trend_view.chart_points(rows)
        times = [row.computed_at for row in plotted]
        values = [row.value for row in plotted]
        levels = [point_level(value, limit) for value in values]
        return times, values, levels, decimated

    (times, values, levels, decimated), chart_ms = _timed(chart_build, repeat)

    # CSV 导出始终用全量 rows，不经过 chart_points
    detail, export_ms = _timed(
        lambda: trend_rows.detail_rows(
            rows,
            channel=channel,
            code=code,
            limit=limit,
            object_of_channel_node={},
        ),
        repeat,
    )

    return {
        "dashboard": dashboard_ms,
        "tree": tree_ms,
        "health": health_ms,
        "trend_values": values_ms,
        "trend_chart": chart_ms,
        "trend_export": export_ms,
        "counts": {
            "nodes": len(nodes),
            "over_records": len(dashboard.over_records),
            "trend_points": len(rows),
            "chart_points": len(times),
            "chart_levels": len(levels),
            "csv_rows": len(detail),
            "health_nodes": len(health),
            "decimated": decimated,
        },
    }


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lion-Monitor 页面取数性能基准（临时库）")
    parser.add_argument("--rows", type=int, default=240_000, help="指标值总行数（默认 240000）")
    parser.add_argument("--batches", type=int, default=6, help="拆成几个采集批次（默认 6）")
    parser.add_argument("--span-days", type=int, default=90, help="时间序列跨度（默认 90 天）")
    parser.add_argument(
        "--repeat", type=int, default=1, help="每条路径重复次数，报中位数（默认 1）"
    )
    parser.add_argument("--json", dest="json_path", default="", help="把结果另存为 JSON")
    parser.add_argument("--keep", action="store_true", help="保留临时库（默认跑完删除）")
    args = parser.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="lion-bench-"))
    db_path = workdir / "lion-bench.db"
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    try:
        engine = make_engine(db_path)
        init_db(engine)
        session = make_session_factory(engine)()

        say("=" * 92)
        say(f"Lion-Monitor 页面取数性能基准　{datetime.now():%Y-%m-%d %H:%M:%S}")
        say("=" * 92)

        started = time.perf_counter()
        built = build_tree(session, "性能基准任务")
        task = built["task"]
        written, layout = seed_values(
            session,
            task.id,
            dict(built["channels"]),
            built["metrics"],
            args.rows,
            args.batches,
            args.span_days,
        )
        seed_ms = (time.perf_counter() - started) * 1000
        total = session.execute(text("SELECT COUNT(*) FROM metric_value")).scalar()
        if total != args.rows or written != args.rows:
            raise RuntimeError(f"造数行数不符：请求 {args.rows}，写入 {written}，库里 {total} 行")
        say(
            f"　造数：{total} 行 / {layout['series']} 条序列 × "
            f"{layout['points_per_series']} 点，{layout['batches']} 批 × "
            f"{layout['channels']} 通道 × {layout['metrics']} 指标，跨 {layout['span_days']} 天"
            f"　计 {seed_ms:.0f} ms"
        )
        say("")

        trend_target = (
            CHANNELS[0],
            built["channels"][0][1],
            METRICS[0],
            built["metrics"][(CHANNELS[0], METRICS[0])],
        )
        bench = run_bench(session, task.id, trend_target, max(args.repeat, 1))
        counts = bench["counts"]
        if counts["trend_points"] <= 0 or counts["csv_rows"] != counts["trend_points"]:
            raise RuntimeError(
                f"基准无效：趋势取数 {counts['trend_points']} 点、CSV {counts['csv_rows']} 行"
            )
        if counts["chart_points"] > trend_view.DEFAULT_MAX_POINTS:
            raise RuntimeError(
                f"抽稀失效：图上 {counts['chart_points']} 点 > 上限 {trend_view.DEFAULT_MAX_POINTS}"
            )
        if counts["trend_points"] > trend_view.DEFAULT_MAX_POINTS and not counts["decimated"]:
            raise RuntimeError(
                f"抽稀没生效：窗口内 {counts['trend_points']} 点，图上 {counts['chart_points']} 点"
            )

        def line(label: str, stats: dict, tail: str = "") -> None:
            spread = ""
            if args.repeat > 1:
                spread = f"（{stats['min']} ~ {stats['max']}）"
            say(f"　{label:<40}{stats['median']:>10.1f} ms{spread}　{tail}")

        say(f"　耗时（{'中位数，' if args.repeat > 1 else ''}各 {args.repeat} 次）")
        line("dashboard.task_overview", bench["dashboard"], f"超限记录 {counts['over_records']} 条")
        line("tree.list_nodes+tree_snapshot", bench["tree"], f"节点 {counts['nodes']} 个")
        line("tree.node_health（整树判级）", bench["health"], f"节点 {counts['health_nodes']} 个")
        window_days = trend_view.DEFAULT_WINDOW_DAYS
        line(
            f"trend.list_values（{trend_target[0]} · {trend_target[2]}，近 {window_days} 天）",
            bench["trend_values"],
            f"返回 {counts['trend_points']} 点",
        )
        line(
            "trend.chart_build（min/max 抽稀 + 判级建图）",
            bench["trend_chart"],
            f"图上 {counts['chart_points']} 点（抽稀 {'是' if counts['decimated'] else '否'}）",
        )
        line(
            "trend.export_rows（CSV 明细，全量）",
            bench["trend_export"],
            f"CSV {counts['csv_rows']} 行",
        )
        say("")

        result = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "command": " ".join(["scripts/bench_pages.py", *sys.argv[1:]]),
            "rows_requested": args.rows,
            "rows_seeded": total,
            "layout": layout,
            "repeat": args.repeat,
            "seed_ms": round(seed_ms, 1),
            "thresholds": {"warn": WARN, "alert": ALERT, "direction": ">"},
            "timings_ms": {
                key: bench[key]
                for key in (
                    "dashboard",
                    "tree",
                    "health",
                    "trend_values",
                    "trend_chart",
                    "trend_export",
                )
            },
            "counts": counts,
            "db": str(db_path) if args.keep else "临时库已清理",
        }
        if args.json_path:
            target = Path(args.json_path).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            say(f"　JSON：{target}")
        say(f"　库：{'保留 ' + str(db_path) if args.keep else '临时库已清理（--keep 可保留）'}")
        session.close()
        engine.dispose()
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
