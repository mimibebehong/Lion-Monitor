"""S4A 验收脚本：旧参数值灰点标识 + 手动重算（真实跑页面 / 真库，不造假数据）。

用法（工程根目录）::

    .venv/bin/python scripts/verify_s4a_stale.py

覆盖验收项（``docs/kanban/bodies/s4a_brief.md``）：

1. 造两组值（stale=0 / stale=1）→ 趋势页取数带 stale 标记、图注写「M 个由旧参数算出」
2. CSV 明细多一列「旧参数」，stale 行 = 1
3. 评级页 / 工作台显示「待重算 N 条指标值」
4. 点「重算本节点」→ 该节点该指标 stale 行变 0、值按当前参数更新（贴前后数值与参数）
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import numpy as np  # noqa: E402
import trend_rows  # noqa: E402
from _dat_fixture import write_dat  # noqa: E402

from lion_monitor.features import recompute  # noqa: E402
from lion_monitor.features.metrics import compute_metric_series  # noqa: E402
from lion_monitor.features.pipeline import import_batch  # noqa: E402
from lion_monitor.ingest.vibration import read_vibration  # noqa: E402
from lion_monitor.store import metric_assign, tasks, tree  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store.db import init_db, make_engine, make_session_factory  # noqa: E402
from lion_monitor.store.models import MetricValue  # noqa: E402

FS = 32000.0
_TODAY = datetime.now().strftime("%Y%m%d")
STAMP1 = _TODAY + "100000"  # 第一组（改参数后置 stale）
STAMP2 = _TODAY + "110000"  # 第二组（按新参数导入，stale=0）
BAND_LOW = 300.0  # 改这个参数值会真的变：120 Hz 成分被滤掉
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}：{detail}")


def _signal(seconds: float = 8.0) -> np.ndarray:
    t = np.arange(int(FS * seconds)) / FS
    return (0.8 * np.sin(2 * np.pi * 120 * t) + 0.2 * np.sin(2 * np.pi * 300 * t)).astype("float32")


def _batch(folder: Path, stamp: str) -> Path:
    write_dat(
        folder / f"{stamp}-CH1_Task1.dat",
        samples=_signal(),
        channel="CH1",
        started_at=stamp,
        sample_rate_hz=FS,
    )
    return folder


def _task(session):
    task = tasks.create_task(session, name="S4A 验收", operator="mark")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    node = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    metric_node = tree.create_node(
        session, task.id, "metric", "v_RMS", parent_id=node.id, attrs={"code": "v_RMS"}
    )
    assigned = metric_assign.assign_metric(session, task.id, node.id, "v_RMS")
    return task, node, metric_node, assigned


def _expected_points(tmp: Path) -> list[tuple[str, float]]:
    """按当前生效参数（band_low=BAND_LOW）逐文件重算一遍，返回 [(时间戳, 值)]。

    时间戳用 ``pipeline.moment_stamp``（采集起始 + 窗中心），与重算落库同一口径。
    """
    from lion_monitor.features.pipeline import moment_stamp

    points: list[tuple[str, float]] = []
    for stamp in (STAMP1, STAMP2):
        source = _batch(tmp / f"批次{stamp}", stamp) / f"{stamp}-CH1_Task1.dat"
        record = read_vibration(source)
        series = compute_metric_series(
            record.samples, FS, params={"band_low": BAND_LOW}, codes=["v_RMS"]
        )["v_RMS"]
        collected = datetime.strptime(stamp, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        points.extend((moment_stamp(collected, offset), value) for offset, value in series)
    return points


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="s4a-"))
    engine = make_engine(tmp / "s4a.db")
    init_db(engine)
    factory = make_session_factory(engine)

    print("=== 准备：两批真数据 + 改参数把第一批置 stale ===")
    with factory() as session:
        task, node, metric_node, assigned = _task(session)
        channel_node = node.id
        first = import_batch(session, task.id, _batch(tmp / "批次1", STAMP1), {"CH1": channel_node})
        print(f"  第一批 {first.status}：v_RMS {first.metrics_written} 个点")
        metric_assign.update_params(session, assigned.id, {"band_low": BAND_LOW})
        second = import_batch(
            session, task.id, _batch(tmp / "批次2", STAMP2), {"CH1": channel_node}
        )
        print(f"  第二批 {second.status}：v_RMS {second.metrics_written} 个点（按新参数，stale=0）")
        stale_n = metric_store.count_stale(session, task.id)
        rows = metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=channel_node)
        fresh_n = len(rows) - stale_n
        check(
            "两组值：stale=0 与 stale=1 各自可取到",
            stale_n == 4 and fresh_n == 4,
            f"共 {len(rows)} 行（stale=1 的 {stale_n} 行、stale=0 的 {fresh_n} 行）",
        )
        plan = recompute.recompute_plan(session, task.id, channel_node)
        print(
            f"  重算预览：codes={plan.codes}、待重算 {plan.stale_count} 条、"
            f"将重算 {plan.point_estimate} 条、预计约 {plan.estimate_seconds:g} 秒"
        )
        task_id = task.id

    print("\n=== 1 趋势页：灰点标记 + 图注计数（AppTest 真跑页面） ===")
    os.environ["LION_DB"] = str(tmp / "s4a.db")
    import store_ctx  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    trends_app = AppTest.from_file(
        str(ROOT / "app" / "pages" / "trends.py"), default_timeout=180
    ).run()
    if trends_app.exception:
        print("  页面异常：", [exc.value for exc in trends_app.exception])
    captions = "\n".join(item.value for item in trends_app.caption)
    hit = [line for line in captions.splitlines() if "由旧参数算出" in line]
    check(
        "图注含「其中 4 个由旧参数算出」",
        any("共 8 个点，其中 4 个由旧参数算出" in line for line in hit),
        hit[0] if hit else "（没找到图注）",
    )

    import json  # noqa: E402

    spec = json.loads(trends_app.get("plotly_chart")[0].proto.spec)
    stale_trace = next((t for t in spec["data"] if t.get("name") == "旧参数（待重算）"), None)
    check(
        "图上旧参数点是灰色空心点（circle-open / #8b949e）",
        stale_trace is not None
        and stale_trace["marker"]["symbol"] == "circle-open"
        and stale_trace["marker"]["color"] == "#8b949e"
        and len(stale_trace["y"]) == 4,
        f"轨迹点数 {0 if stale_trace is None else len(stale_trace['y'])}、"
        f"symbol={None if stale_trace is None else stale_trace['marker']['symbol']}",
    )
    fresh_trace = next((t for t in spec["data"] if t.get("name") == "v_RMS · 正常点"), None)
    check(
        "正常点单独一条轨迹（4 点，不混进灰点）",
        fresh_trace is not None and len(fresh_trace["y"]) == 4,
        f"轨迹点数 {0 if fresh_trace is None else len(fresh_trace['y'])}",
    )

    print("\n=== 2 CSV 明细：多一列「旧参数」 ===")
    with factory() as session:
        rows = metric_store.list_values(session, task_id, "CH1", "v_RMS", node_id=channel_node)
        detail = trend_rows.detail_rows(
            rows,
            channel="CH1",
            code="v_RMS",
            limit=None,
            object_of_channel_node={channel_node: "轴承A"},
        )
    print("  列顺序：" + " | ".join(trend_rows.COLUMNS))
    for row in detail:
        print(f"  {row['计算时间']}  {row['值']:>8} {row['单位']}  旧参数={row['旧参数']}")
    flags = [row["旧参数"] for row in detail]
    check(
        "CSV 含「旧参数」列且 stale 行 = 1（4 行 1、4 行 0）",
        "旧参数" in trend_rows.COLUMNS
        and trend_rows.COLUMNS[-1] == "旧参数"
        and flags.count(1) == 4
        and flags.count(0) == 4,
        f"1 的个数 {flags.count(1)}、0 的个数 {flags.count(0)}",
    )

    print("\n=== 3 评级页 / 工作台：待重算 N 条 ===")
    rank_metrics = {item.label: item.value for item in trends_app.metric}
    check(
        "评级页指标卡「待重算」= 4（只有第一批的旧值过期）",
        rank_metrics.get("待重算") == "4",
        f"待重算={rank_metrics.get('待重算')}",
    )
    store_ctx._session_factory.clear()
    home_app = AppTest.from_file(str(ROOT / "app" / "pages" / "home.py"), default_timeout=180).run()
    home_metrics = {item.label: item.value for item in home_app.metric}
    home_caption = [c.value for c in home_app.caption if "待重算" in c.value]
    check(
        "工作台指标卡「待重算」= 4 且有一行说明",
        home_metrics.get("待重算") == "4" and bool(home_caption),
        f"待重算={home_metrics.get('待重算')}；说明「{home_caption[0] if home_caption else ''}」",
    )

    print("\n=== 4 点「重算本节点」：stale 归零 + 值按当前参数更新 ===")

    def _dump(session) -> list[MetricValue]:
        return list(
            session.query(MetricValue)
            .filter(
                MetricValue.node_id == channel_node,
                MetricValue.code == "v_RMS",
                MetricValue.deleted_at.is_(None),
            )
            .order_by(MetricValue.computed_at)
            .all()
        )

    def _show(title: str, rows: list[MetricValue]) -> None:
        print(f"  {title}：")
        for row in rows:
            print(
                f"    {metric_store._moment_text(row.computed_at)}  值 {row.value:.6f}  "
                f"旧参数={int(row.stale)}  hash={row.params_hash or '—'}  "
                f"生效参数={row.effective_params_json or '—'}"
            )

    with factory() as session:
        _show("重算前", _dump(session))

    confirm = next(box for box in trends_app.checkbox if box.label == "确认重算")
    confirm.check()
    trends_app.run()
    next(btn for btn in trends_app.button if btn.label == "重算本节点").click()
    trends_app.run()
    if trends_app.exception:
        print("  点击后页面异常：", [exc.value for exc in trends_app.exception])
    success = [item.value for item in trends_app.success]

    with factory() as session:
        after = _dump(session)
        stale_after = metric_store.count_stale(session, task_id)
        expected_values = sorted(round(value, 6) for _stamp, value in _expected_points(tmp))
    _show("重算后", after)
    check(
        "该节点该指标 stale 行变 0",
        stale_after == 0,
        f"待重算 {stale_after} 条（重算前 8 条）",
    )
    check(
        "值按当前参数（band_low=300）重算",
        sorted(round(row.value, 6) for row in after) == expected_values,
        f"生效参数 {after[0].effective_params_json if after else '—'}，"
        f"值 {[round(row.value, 4) for row in after]}",
    )
    check(
        "页面提示「重算完成」并写明覆盖/作废条数",
        any("重算完成" in text for text in success),
        success[0] if success else "（没看到提示）",
    )

    passed = sum(1 for _name, ok, _detail in results if ok)
    print(f"\n合计 {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
