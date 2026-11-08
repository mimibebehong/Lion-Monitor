"""模板 S3 验收实跑：导出带生效值 → 预览差异 → 三策略导入 → 向前兼容。

用法（项目根目录）::

    python scripts/verify_template_effective_params.py

只依赖临时库，不碰 data/lion.db；任一条不通过即非零退出。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.store import (  # noqa: E402
    bootstrap,
    channel_reg,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
    tree_template,
)
from lion_monitor.store.db import init_db, make_engine, make_session_factory  # noqa: E402

lines: list[str] = []
failed = 0


def say(text: str = "") -> None:
    print(text)
    lines.append(text)


def check(label: str, ok: bool, detail: str = "") -> None:
    global failed
    say(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"　{detail}" if detail else ""))
    if not ok:
        failed += 1


def ledger(session, task_id: int, band_high: float) -> dict[str, Any]:
    """台账：1 监测对象 + CH1 + 内置三指标；把该任务的 v_RMS 定义默认改成 band_high。"""
    obj = object_registry.create_object(session, task_id, "隔膜过辊轴承", "轴承", part="隔膜过辊")
    ch1 = channel_reg.create_channel(session, task_id, obj.id, "CH1")
    metric_def.seed_defaults(session, task_id)
    defs = {row.code: row for row in metric_def.list_metrics(session, task_id)}
    metric_def.update_metric(session, defs["v_RMS"].id, defaults={"band_high": band_high})
    defs = {row.code: row for row in metric_def.list_metrics(session, task_id)}
    return {"object": obj, "ch1": ch1, "defs": defs}


def build_line(session, task_id: int, refs: dict[str, Any]) -> None:
    """设备 → 监测对象 → CH1 → v_RMS；指标绑定不带任何覆盖项。"""
    root = tree.root_node(session, task_id)
    device = tree.create_node(
        session, task_id, "device", "卷绕产线", parent_id=root.id, attrs={"工位": "WL-1"}
    )
    comp = tree.create_node(
        session,
        task_id,
        "component",
        "隔膜过辊轴承",
        parent_id=device.id,
        attrs={"部位": "隔膜过辊", "转速": "1500"},
        ref_id=refs["object"].id,
    )
    ch1 = tree.create_node(
        session, task_id, "channel", "CH1", parent_id=comp.id, ref_id=refs["ch1"].id
    )
    tree.create_node(
        session,
        task_id,
        "metric",
        "v_RMS",
        parent_id=ch1.id,
        attrs={"code": "v_RMS", "单位": refs["defs"]["v_RMS"].unit},
        ref_id=refs["defs"]["v_RMS"].id,
    )
    metric_assign.assign_metric(session, task_id, ch1.id, "v_RMS", params={})


def effective(session, task_id: int) -> dict[str, Any]:
    row = next(
        item for item in metric_assign.list_assignments(session, task_id) if item.code == "v_RMS"
    )
    return metric_assign.effective_params(session, row.id)


def metric_node(payload: dict[str, Any]) -> dict[str, Any]:
    stack = [payload["root"]]
    while stack:
        node = stack.pop()
        if node.get("type") == "metric":
            return node
        stack.extend(node.get("children") or [])
    raise AssertionError("模板里没有指标节点")


def strip_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """造一份老程序导出的 v2 文件：没有 params_snapshot / effective_params。"""
    payload.pop("params_snapshot", None)
    stack = [payload["root"]]
    while stack:
        node = stack.pop()
        node.pop("effective_params", None)
        stack.extend(node.get("children") or [])
    return payload


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tpl-s3-verify-"))
    engine = make_engine(tmp / "verify.db")
    init_db(engine)
    session = make_session_factory(engine)()

    say("=" * 78)
    say(
        "模板 S3 验收（生效值快照 + 参数三策略 + 漂移预览）　生成时间："
        + __import__("datetime").datetime.now().isoformat(" ", timespec="seconds")
    )
    say(f"临时库：{tmp / 'verify.db'}")
    say("=" * 78)

    try:
        # 源任务：定义默认 band_high=1000，节点无覆盖
        src = tasks.create_task(session, name="卷绕产线振动监测")
        build_line(session, src.id, ledger(session, src.id, 1000))
        # 目标任务：同一物理产线，但定义默认 band_high=500
        dst = tasks.create_task(session, name="新任务（默认 500）")
        ledger(session, dst.id, 500)

        payload = tree.export_template(session, src.id)
        node = metric_node(payload)
        say("")
        say("—— 导出（源任务 v_RMS 指标节点） ——")
        say(f"params（覆盖 diff）={node['params']}")
        say(f"effective_params.band_high={node['effective_params']['band_high']}")
        say(f"文件头 versions={payload['version']}　params_snapshot={payload.get('params_snapshot')}")
        check(
            "验收 1-a) 导出含 effective_params，且是完整生效值（不是 diff）",
            node["effective_params"].get("band_high") == 1000
            and node["params"] == {}
            and len(node["effective_params"]) > 1,
            f"band_high={node['effective_params'].get('band_high')}　"
            f"快照键 {len(node['effective_params'])} 个",
        )

        # 验收 2：预览差异
        preview = tree.preview_import(session, dst.id, payload)
        say("")
        say("—— 预览（目标任务默认 band_high=500） ——")
        say(
            f"未匹配台账 {len(preview.unmatched)} 处　参数差异 {preview.param_diff_count} 处"
            f"　带快照={preview.has_snapshot}"
        )
        for item in preview.param_diffs:
            say(f"     · {item.text}")
        check(
            "验收 2) 预览列出该差异 1 处，文案含「500 → 1000」",
            preview.param_diff_count == 1
            and "500 → 1000" in preview.param_diffs[0].text
            and not preview.blocked,
            preview.param_diffs[0].text if preview.param_diffs else "（无差异）",
        )
        check(
            "验收 2-b) 预览不写库",
            [row.node_type for row in tree.list_nodes(session, dst.id)] == ["task"]
            and metric_assign.list_assignments(session, dst.id) == [],
            "目标任务仍只有根节点、无指标绑定",
        )

        # 验收 1：override 导入
        report = tree.import_template(session, dst.id, payload)
        value = effective(session, dst.id)["band_high"]
        say("")
        say("—— 导入（override，默认策略） ——")
        say(
            f"绑定 {report.metrics_bound} 个　按生效值搬运 {report.params_overridden} 个　"
            f"目标 v_RMS 生效 band_high={value}"
        )
        check(
            "验收 1) 目标节点生效值 = 1000（不再是 500）",
            abs(float(value) - 1000.0) < 1e-9 and report.params_overridden == 1,
            f"生效 band_high={value}",
        )

        # 验收 3：follow_default 保旧行为
        dst2 = tasks.create_task(session, name="新任务（follow_default）")
        ledger(session, dst2.id, 500)
        report2 = tree.import_template(
            session,
            dst2.id,
            payload,
            tree_template.ImportOptions(param_strategy="follow_default"),
        )
        value2 = effective(session, dst2.id)["band_high"]
        say("")
        say(f"—— 导入（follow_default） —— 目标 v_RMS 生效 band_high={value2}")
        check(
            "验收 3) follow_default 仍得到 500（旧行为保留）",
            abs(float(value2) - 500.0) < 1e-9 and report2.params_overridden == 0,
            f"生效 band_high={value2}",
        )

        # 验收 4 前半：v1 旧文件
        v1_task = tasks.create_task(session, name="v1 导入任务")
        ledger(session, v1_task.id, 500)
        v1_payload = bootstrap.bootstrap_example_tree_payload()
        v1_preview = tree.preview_import(session, v1_task.id, v1_payload)
        v1_report = tree.import_template(session, v1_task.id, v1_payload)
        check(
            "验收 4-a) v1 文件仍可导入",
            v1_preview.legacy is True
            and v1_report.version == 1
            and v1_report.node_counts.get("metric") == 12,
            f"version={v1_report.version}　节点 {v1_report.node_counts}",
        )

        # 验收 4 后半：老 v2 文件（无快照）→ follow_default + 提示
        dst3 = tasks.create_task(session, name="新任务（老 v2 文件）")
        ledger(session, dst3.id, 500)
        old = strip_snapshot(tree.export_template(session, src.id))
        old_preview = tree.preview_import(session, dst3.id, old)
        old_report = tree.import_template(
            session,
            dst3.id,
            old,
            tree_template.ImportOptions(param_strategy="override"),
        )
        value3 = effective(session, dst3.id)["band_high"]
        say("")
        say("—— 老 v2 文件（无 effective_params，即便传 override） ——")
        say(f"预览提示：{old_preview.notice}")
        say(f"导入提示：{old_report.notice}")
        check(
            "验收 4-b) 无 effective_params 的 v2 按 follow_default 处理并提示",
            old_preview.has_snapshot is False
            and "生效参数" in old_preview.notice
            and old_report.params_overridden == 0
            and "生效参数" in old_report.notice
            and abs(float(value3) - 500.0) < 1e-9,
            f"生效 band_high={value3}　has_snapshot={old_preview.has_snapshot}",
        )
    finally:
        session.close()

    say("")
    say("=" * 78)
    say("结论：S3 验收 1–4 全部通过。" if not failed else f"结论：{failed} 条未通过。")
    say("=" * 78)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
