"""S-1 实跑验收：阈值「越小越差」（lower）方向保存 / 判级 / 页面 / 模板往返 / 旧数据。

结论全部来自真实运行：临时库不碰 ``data/lion.db``；「迁移前」的判级语义直接从
``git show 4b1e139:src/lion_monitor/features/health.py`` 里取**旧代码**执行，不是我另写一遍。

用法：``.venv/bin/python scripts/verify_s1_direction_limits_2026-10-31.py``
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

TMP = Path(tempfile.mkdtemp(prefix="s1-verify-"))
os.environ["LION_DB"] = str(TMP / "verify.db")

from lion_monitor import limits_rules  # noqa: E402
from lion_monitor.features.health import point_level  # noqa: E402
from lion_monitor.store import (  # noqa: E402
    channel_reg,
    limits,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store.models import MetricLimit  # noqa: E402

BASELINE_COMMIT = "4b1e139"


def line() -> None:
    print("-" * 78)


def build_task(session, name: str) -> dict:
    """任务 → 设备 → 对象 → CH1（油压 下限指标 / v_RMS 上限指标）。"""
    task = tasks.create_task(session, name=name)
    obj = object_registry.create_object(session, task.id, "隔膜过辊轴承", "轴承", part="隔膜过辊")
    ch = channel_reg.create_channel(session, task.id, obj.id, "CH1")
    metric_def.seed_defaults(session, task.id)
    metric_def.create_metric(session, task.id, "油压", unit="MPa")
    defs = {row.code: row for row in metric_def.list_metrics(session, task.id)}
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(
        session,
        task.id,
        "component",
        "隔膜过辊轴承",
        parent_id=device.id,
        attrs={"部位": "隔膜过辊", "转速": "1500"},
        ref_id=obj.id,
    )
    ch_node = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id, ref_id=ch.id)
    oil = tree.create_node(
        session,
        task.id,
        "metric",
        "油压",
        parent_id=ch_node.id,
        attrs={"code": "油压", "单位": "MPa"},
        ref_id=defs["油压"].id,
    )
    vrms = tree.create_node(
        session,
        task.id,
        "metric",
        "v_RMS",
        parent_id=ch_node.id,
        attrs={"code": "v_RMS", "单位": "mm/s"},
        ref_id=defs["v_RMS"].id,
    )
    metric_assign.assign_metric(session, task.id, ch_node.id, "v_RMS")
    return {"task": task, "ch_node": ch_node.id, "oil": oil.id, "v_RMS": vrms.id}


def old_point_level():
    """旧实现（迁移前）的 point_level，直接从 git 里取，保证不是我自己重写。"""
    src = subprocess.run(
        ["git", "show", f"{BASELINE_COMMIT}:src/lion_monitor/features/health.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    module = ast.parse(src)
    fn = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "point_level"
    )
    namespace: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<old-health>", "exec"), namespace)
    return namespace["point_level"]


def main() -> int:
    engine = store_db.make_engine(Path(os.environ["LION_DB"]))
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)

    print(f"临时库：{os.environ['LION_DB']}")
    print(f"旧实现来源：git {BASELINE_COMMIT}:src/lion_monitor/features/health.py")
    line()

    # ------------------------------------------------ 验收 1 保存路径按方向校验
    print("[验收 1] 保存路径：按方向校验（上限 warn<alert，下限 warn>alert）")
    with store_db.session_scope(factory) as session:
        ids = build_task(session, "S1 验收任务")
        task_id = ids["task"].id
        row = limits.set_limit(session, task_id, ids["oil"], "油压", 3.0, 1.0, "<")
        print(
            f"　下限 油压 warn=3 / alert=1 → 保存成功：warn={row.warn:g} alert={row.alert:g} "
            f"direction={row.direction}"
        )
        print(f"　读回：{limits.resolve_limit(session, task_id, ids['oil'], '油压')}")
        try:
            limits.set_limit(session, task_id, ids["v_RMS"], "v_RMS", 4.5, 2.8, ">")
        except limits.LimitError as exc:
            print(f"　上限 warn=4.5 / alert=2.8 → LimitError：{exc}")
        try:
            limits.set_limit(session, task_id, ids["oil"], "油压", 1.0, 3.0, "<")
        except limits.LimitError as exc:
            print(f"　下限 warn=1 / alert=3 → LimitError：{exc}")
        limits.set_limit(session, task_id, ids["v_RMS"], "v_RMS", 2.8, 4.5, ">")
        print("　上限 振动 warn=2.8 / alert=4.5 → 保存成功")
    line()

    # ------------------------------------------------ 验收 2 判级
    print("[验收 2] 判级（grade）方向口径")
    print("　下限 油压 warn=3 / alert=1：")
    for value in (3.2, 2.5, 0.8):
        print(
            f"　　值 {value:<4} → grade={limits_rules.grade(value, 'lower', 3.0, 1.0)}"
            f"　point_level={point_level(value, (3.0, 1.0, '<', '指标级'))}"
        )
    print("　上限 振动 warn=2.8 / alert=4.5：")
    for value in (1.0, 2.9, 5.0):
        print(
            f"　　值 {value:<4} → grade={limits_rules.grade(value, 'upper', 2.8, 4.5)}"
            f"　point_level={point_level(value, (2.8, 4.5, '>', '指标级'))}"
        )
    line()

    # ------------------------------------------------ 验收 4 模板往返
    print("[验收 4] 模板 v2 往返：下限阈值导出 → 导入新任务")
    with store_db.session_scope(factory) as session:
        payload = tree.export_template(session, task_id)
        exported = [r for r in payload["thresholds"] if r["metric_code"] == "油压"]
        print(f"　导出：{exported}")
        new_ids = build_task(session, "S1 导入任务")
        report = tree.import_template(session, new_ids["task"].id, payload)
        print(f"　导入：thresholds_written={report.thresholds_written}")
        src_limit = limits.resolve_limit(session, task_id, ids["oil"], "油压")
        # 导入是整树替换：目标任务原来的节点 id 已失效，按指标码重新定位
        dst_node = next(
            node.id
            for node in tree.list_nodes(session, new_ids["task"].id)
            if node.node_type == "metric" and (node.attrs or {}).get("code") == "油压"
        )
        dst_limit = limits.resolve_limit(session, new_ids["task"].id, dst_node, "油压")
        print(
            f"　源任务上限值/判级：{src_limit[0]:g}/{src_limit[1]:g} {src_limit[2]}"
            f"　判级 3.2/2.5/0.8 = "
            f"{point_level(3.2, src_limit)}/{point_level(2.5, src_limit)}/"
            f"{point_level(0.8, src_limit)}"
        )
        print(
            f"　目标任务上限值/判级：{dst_limit[0]:g}/{dst_limit[1]:g} {dst_limit[2]}"
            f"　判级 3.2/2.5/0.8 = "
            f"{point_level(3.2, dst_limit)}/{point_level(2.5, dst_limit)}/"
            f"{point_level(0.8, dst_limit)}"
        )
        same = src_limit[:3] == dst_limit[:3] and all(
            point_level(v, src_limit) == point_level(v, dst_limit) for v in (3.2, 2.5, 0.8)
        )
        print(f"　往返一致：{same}")
    line()

    # ------------------------------------------------ 验收 5 旧数据
    print('[验收 5] 老数据 direction="<"：读得到、判级不变、数值不改')
    old = old_point_level()
    with store_db.session_scope(factory) as session:
        legacy_ids = build_task(session, "S1 旧库任务")
        # 老行 A：模板层写进去的合法老行（下限 warn>alert）
        session.add(
            MetricLimit(
                task_id=legacy_ids["task"].id,
                node_id=legacy_ids["oil"],
                metric_code="油压",
                warn=3.0,
                alert=1.0,
                direction="<",
                enabled=1,
            )
        )
        # 老行 B：旧硬校验残留（下限 warn<alert，反向）
        session.add(
            MetricLimit(
                task_id=legacy_ids["task"].id,
                node_id=legacy_ids["v_RMS"],
                metric_code="v_RMS",
                warn=0.2,
                alert=0.5,
                direction="<",
                enabled=1,
            )
        )
        session.commit()

        for label, node_id, code, values in (
            ("老行 A（下限 3/1）", legacy_ids["oil"], "油压", (3.2, 2.5, 0.8)),
            ("老行 B（下限 0.2/0.5，反向）", legacy_ids["v_RMS"], "v_RMS", (0.1, 0.3, 0.6)),
        ):
            limit = limits.resolve_limit(session, legacy_ids["task"].id, node_id, code)
            before = [old(v, limit) for v in values]
            after = [point_level(v, limit) for v in values]
            print(
                f"　{label}：读回 {limit[0]:g}/{limit[1]:g} direction={limit[2]}"
                f"　问题标记：{limits_rules.problem_of(limit[2], limit[0], limit[1])}"
            )
            print(f"　　值 {values} 迁移前判级 {before} / 现在 {after} → 一致 {before == after}")
        stored = [
            (m.metric_code, m.warn, m.alert, m.direction)
            for m in session.query(MetricLimit)
            .filter(MetricLimit.task_id == legacy_ids["task"].id)
            .all()
        ]
        print(f"　库里数值（未改动）：{stored}")
    line()

    # ------------------------------------------------ 验收 3 页面 AppTest
    print("[验收 3] 阈值设置页 AppTest：下限 3/1 保存 + 下限 1/3 报错")
    import store_ctx  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402

    LOWER = limits_rules.DIRECTION_TEXT["lower"]
    with store_db.session_scope(factory) as session:
        page_ids = build_task(session, "S1 页面任务")
        page_task_id = page_ids["task"].id

    def page_rows(factory_, task: int) -> list[tuple[str, float, float, str]]:
        with store_db.session_scope(factory_) as session:
            return [
                (row.metric_code, row.warn, row.alert, row.direction)
                for row in limits.list_limits(session, task)
            ]

    def page():
        store_ctx._session_factory.clear()
        at = AppTest.from_file(str(ROOT / "app" / "pages" / "thresholds.py"), default_timeout=120)
        # 页面按「当前任务」取树，这里钉到本次造的任务上
        at.session_state["current_task_id"] = page_task_id
        return at

    def edit(at, warn: str, alert: str) -> None:
        at.session_state["limit_editor"] = {
            "edited_rows": {0: {"warn": warn, "alert": alert, "方向": LOWER}},
            "added_rows": [],
            "deleted_rows": [],
        }

    at = page().run()
    print(
        f"　初始：异常={[e.value for e in at.exception]} 表格行数={len(at.dataframe[0].value)}"
        f" 方向列={list(at.dataframe[0].value['方向'])}"
    )
    edit(at, "3", "1")
    at.run()
    print(f"　选下限后提示：{[c.value for c in at.caption if '下限' in c.value]}")
    at.button(key="lim_save").click().run()
    print(
        f"　保存结果：异常={[e.value for e in at.exception]} 错误={[e.value for e in at.error]}"
        f" 成功={[e.value for e in at.success]}"
    )
    frame = at.dataframe[0].value
    print(
        f"　回显：warn={frame.iloc[0]['warn']} alert={frame.iloc[0]['alert']}"
        f" 方向={frame.iloc[0]['方向']}"
    )
    print(f"　库内：{page_rows(factory, page_task_id)}")

    edit(at, "1", "3")
    at.run()
    at.button(key="lim_save").click().run()
    print(
        f"　下限 1/3 保存：异常={[e.value for e in at.exception]}"
        f" 错误={[e.value for e in at.error]}"
    )
    print(f"　库内（未写入）：{page_rows(factory, page_task_id)}")
    line()
    print("结论：上述输出即 S-1 六条验收的实跑证据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
