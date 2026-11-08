"""现场数据验证：方案 A（值按节点存、按参数指纹去重）在真实批次上的表现。

跑法::

    .venv/bin/python scripts/verify_node_params_import.py \
        --data "/mnt/d/hermes_wp/Project/锂电产线/envision/data/四通道保存"

验证三条：

1. 同一通道号挂两个节点、带通不同 → 两套不同的 v_RMS 值
2. 同一通道号两节点参数（与转速）相同 → 只真算一次，结果写两行（计数器为证）
3. 无转速节点上的频率类指标被跳过，且原因留在 ImportResult.skipped_metrics + 日志里

库落在临时库（默认 workspace/temp/<日期>/），跑完不污染工程 data/lion.db。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(PROJECT_SRC))

from lion_monitor.features.pipeline import import_batch, metric_jobs  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metric_assign, metric_def, tasks, tree  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402

SPEED_SCHEMA = [
    {
        "key": "f0_hz",
        "label": "特征频率",
        "group": "basic",
        "type": "float",
        "unit": "Hz",
        "default": 0.0,
        "scaling": "speed",
        "help": "按转速缩放的频率项（无转速时不可算）",
    }
]


class _Capture(logging.Handler):
    """把管线日志收起来一起打印（证明跳过不是静默的）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _banner(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def _build_tree(session, task_id: int) -> dict[str, int]:
    """装配验证用健康树：

    - 对象A 辊压机1#驱动端轴承 CH1：v_RMS(band_high=2000) + a_RMS
    - 对象B 辊压机1#非驱动端轴承 CH1：v_RMS(band_high=500)   ← 同通道不同带通
    - 对象C 辊压机2#驱动端轴承   CH1：v_RMS(band_high=2000)   ← 同通道同参数（应复用一次计算）
    - 对象E 3#辊压机轴承          CH1：v_RMS(300–2000)        ← 换个高频段，值差一眼能看出
    - 对象D 2#牵引电机           CH2：1X（频率类，无转速）+ a_RMS  ← 应被跳过
    """
    root = tree.root_node(session, task_id)
    device = tree.create_node(session, task_id, "device", "锂电产线", parent_id=root.id)
    spec = [
        ("辊压机1#驱动端轴承", "CH1", [("v_RMS", {"band_high": 2000.0}), ("a_RMS", {})]),
        ("辊压机1#非驱动端轴承", "CH1", [("v_RMS", {"band_high": 500.0})]),
        ("辊压机2#驱动端轴承", "CH1", [("v_RMS", {"band_high": 2000.0})]),
        ("3#辊压机轴承", "CH1", [("v_RMS", {"band_low": 300.0, "band_high": 2000.0})]),
        ("2#牵引电机", "CH2", [("1X", {}), ("a_RMS", {})]),
    ]
    channels: dict[str, int] = {}
    code_counts: dict[str, int] = {}
    for object_name, channel_code, metrics in spec:
        code_counts[channel_code] = code_counts.get(channel_code, 0) + 1
        comp = tree.create_node(session, task_id, "component", object_name, parent_id=device.id)
        channel = tree.create_node(session, task_id, "channel", channel_code, parent_id=comp.id)
        channels[object_name] = channel.id
        for code, params in metrics:
            tree.create_node(
                session, task_id, "metric", code, parent_id=channel.id, attrs={"code": code}
            )
            metric_assign.assign_metric(session, task_id, channel.id, code, params=params or None)
    return channels, code_counts


def main() -> int:
    parser = argparse.ArgumentParser(description="方案 A 现场数据验证")
    parser.add_argument(
        "--data",
        default="/mnt/d/hermes_wp/Project/锂电产线/envision/data/四通道保存",
        help="四通道批次所在目录（每个子目录一个批次）",
    )
    parser.add_argument("--db", default="", help="验证用临时库路径（默认 workspace/temp/<日期>/）")
    args = parser.parse_args()

    data_dir = Path(args.data)
    if not data_dir.is_dir():
        print(f"数据目录不存在：{data_dir}", file=sys.stderr)
        return 2
    batches = sorted(p for p in data_dir.iterdir() if p.is_dir())
    if not batches:
        print(f"目录下没有批次子目录：{data_dir}", file=sys.stderr)
        return 2

    db_path = (
        Path(args.db)
        if args.db
        else Path.home() / "workspace" / "temp" / date.today().isoformat() / "lion_verify_A.db"
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    capture = _Capture()
    pipeline_logger = logging.getLogger("lion_monitor.features.pipeline")
    pipeline_logger.setLevel(logging.WARNING)
    pipeline_logger.addHandler(capture)

    engine = store_db.make_engine(db_path)
    store_db.init_db(engine)

    with store_db.make_session_factory(engine)() as session:
        task = tasks.create_task(session, name=f"方案A现场验证 {date.today():%Y-%m-%d}")
        metric_def.create_metric(session, task.id, "1X", unit="Hz", schema=SPEED_SCHEMA)
        channels, code_counts = _build_tree(session, task.id)

        most = max(code_counts.values())
        _banner(f"健康树装配（{len(channels)} 个监测对象，同一通道号最多挂 {most} 个节点）")
        for object_name, node_id in channels.items():
            print(f"  {object_name:<12} 通道节点 id={node_id}")

        _banner("① 导入前解析的计算任务（每个通道节点的生效参数 + 转速）")
        jobs, skipped = metric_jobs(session, task.id)
        for job in jobs:
            band = "—"
            if "band_low" in job.params:
                band = f"{job.params['band_low']:g}–{job.params['band_high']:g} Hz"
            rpm = f"{job.rpm:g} rpm（{job.rpm_source}）" if job.rpm else "无转速"
            print(
                f"  {job.object_name:<12} {job.code:<6} 带通 {band:<16} "
                f"指纹 {job.params_hash:<13} {rpm}"
            )
        for item in skipped:
            print(f"  跳过 {item.object_name:<12} {item.code:<6} 原因：{item.reason}")

        _banner("② 导入现场 3 个四通道批次")
        results = []
        for folder in batches:
            result = import_batch(session, task.id, folder, {})
            results.append(result)
            print(
                f"  {folder.name} → {result.status}｜通道 {result.channels}｜"
                f"写 {result.metrics_written} 行｜真算 {result.compute_runs} 次｜"
                f"复用 {result.dedupe_hits} 次｜跳过 {len(result.skipped_metrics or [])} 个指标"
            )
            if result.unmapped_channels:
                print(f"    未映射到健康树的通道：{' / '.join(result.unmapped_channels)}")

        ch1 = int(channels["辊压机1#驱动端轴承"])
        ch1_b = int(channels["辊压机1#非驱动端轴承"])
        ch1_c = int(channels["辊压机2#驱动端轴承"])
        ch1_e = int(channels["3#辊压机轴承"])
        ch2_d = int(channels["2#牵引电机"])

        _banner("③ 证据一：同一通道号挂两个节点、带通不同 → 两套不同的 v_RMS")
        rows_a = metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=ch1)
        rows_b = metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=ch1_b)
        print(f"  辊压机1#驱动端轴承（10–2000 Hz）：{len(rows_a)} 个点")
        for row in rows_a:
            print(f"    {row.computed_at}  v_RMS={row.value:.4f} mm/s  指纹 {row.params_hash}")
        print(f"  辊压机1#非驱动端轴承（10–500 Hz）：{len(rows_b)} 个点")
        for row in rows_b:
            print(f"    {row.computed_at}  v_RMS={row.value:.4f} mm/s  指纹 {row.params_hash}")
        diff = [abs(a.value - b.value) for a, b in zip(rows_a, rows_b, strict=True)]
        print(f"  逐点差值：{[f'{d:.4f}' for d in diff]}")
        hashes_a = {r.params_hash for r in rows_a}
        hashes_b = {r.params_hash for r in rows_b}
        print(f"  两套指纹不同：{hashes_a != hashes_b}")
        print(f"  所有点都不同：{all(d > 1e-6 for d in diff)}")
        rows_e = metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=ch1_e)
        print("  （同一通道第三个节点 300–2000 Hz，差异更直观）：")
        for a, e in zip(rows_a, rows_e, strict=True):
            print(
                f"    {a.computed_at}  10–2000 Hz={a.value:.4f} vs 300–2000 Hz={e.value:.4f} mm/s"
                f"（{a.value / e.value:.1f} 倍）"
            )

        _banner("④ 证据二：同参数两节点 → 只算一次（计数器 + 两行等值）")
        rows_c = metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=ch1_c)
        print(f"  每批次计算任务数：{len(jobs)} 个节点指标，其中 A/C 的 v_RMS 参数与转速完全相同")
        for result in results:
            print(
                f"    {result.collected_at}：真算 {result.compute_runs} 次 / "
                f"复用 {result.dedupe_hits} 次（若不去重至少要算 {len(jobs)} 次）"
            )
        same_value = [
            round(a.value, 6) == round(c.value, 6) for a, c in zip(rows_a, rows_c, strict=True)
        ]
        print(f"  A 与 C 逐点是否等值：{same_value}")
        print(
            f"  A 与 C 指纹是否相同："
            f"{ {r.params_hash for r in rows_a} == {r.params_hash for r in rows_c} }"
        )

        _banner("⑤ 证据三：无转速节点上的频率类指标被跳过（有原因，不静默）")
        for result in results:
            for item in result.skipped_metrics or []:
                print(
                    f"  {result.collected_at} 跳过 "
                    f"{item.object_name}·{item.channel_code}·{item.code}"
                    f"（节点 {item.node_id}）：{item.reason}"
                )
        print("  管线日志（logger.warning）：")
        for line in capture.lines:
            print(f"    {line}")
        motor_rows = metric_store.list_values(session, task.id, "CH2")
        print(f"  CH2 实际落库指标：{sorted({r.metric_code for r in motor_rows})}（1X 没有值）")

        _banner("⑥ 落库总览（按节点 × 指标）")
        for code, node_id, label in (
            ("CH1", ch1, "A 辊压机1#驱动端"),
            ("CH1", ch1_b, "B 辊压机1#非驱动端"),
            ("CH1", ch1_c, "C 辊压机2#驱动端"),
            ("CH1", ch1_e, "E 3#辊压机轴承"),
            ("CH2", ch2_d, "D 2#牵引电机"),
        ):
            rows = metric_store.list_values(session, task.id, code, node_id=node_id)
            summary = ", ".join(
                f"{metric}={value:.4f}"
                for metric, value in [(r.metric_code, r.value) for r in rows]
            )
            print(f"  {label:<18} node_id={node_id}  {len(rows)} 行：{summary}")

        _banner("⑦ 补充：改参数 / 改转速 → 旧值置 stale（只标记，不重算）")
        assign_b = next(
            row
            for row in metric_assign.list_assignments(session, task.id)
            if row.node_id == ch1_b and row.code == "v_RMS"
        )
        metric_assign.update_params(session, assign_b.id, {"band_high": 800.0})
        stale_b = [r for r in metric_store.list_values(session, task.id, "CH1", node_id=ch1_b)]
        stale_a = [r for r in metric_store.list_values(session, task.id, "CH1", node_id=ch1)]
        count_b = f"{sum(r.stale for r in stale_b)}/{len(stale_b)}"
        count_a = f"{sum(r.stale for r in stale_a)}/{len(stale_a)}"
        print(
            f"  B 节点带通改 500→800 Hz：B 的 {count_b} 行 stale=1，"
            f"A 的 {count_a} 行 stale=1（没动的节点不受影响）"
        )
        metric_assign.set_node_rpm(session, ch1, 1480.0)
        after_rpm = metric_store.list_values(session, task.id, "CH1", node_id=ch1)
        count_rpm = f"{sum(r.stale for r in after_rpm)}/{len(after_rpm)}"
        print(f"  A 节点填 1480 rpm：A 的 {count_rpm} 行 stale=1（转速基准变了，值待重算）")

    print(f"\n临时库：{db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
