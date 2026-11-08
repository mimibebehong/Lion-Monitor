"""模板 v2 验收实跑：导出「卷绕产线振动监测」→ 导入新任务 → 五条检查。

用法（项目根目录）::

    python scripts/verify_template_v2.py [输出文件]

跑完把结果写到 reports/ 下（默认 reports/2026-10-31-template-v2-verification.txt），
只依赖临时库，不碰 data/lion.db。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.store import (  # noqa: E402
    bootstrap,
    channel_reg,
    limits,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store.db import init_db, make_engine, make_session_factory  # noqa: E402
from lion_monitor.store.tree import NodeRow  # noqa: E402

OUT = (
    Path(sys.argv[1])
    if len(sys.argv) > 1
    else ROOT / "reports" / "2026-10-31-template-v2-verification.txt"
)

lines: list[str] = []


def say(text: str = "") -> None:
    print(text)
    lines.append(text)


def check(label: str, ok: bool, detail: str = "") -> None:
    say(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"　{detail}" if detail else ""))
    if not ok:
        raise SystemExit(f"验收未通过：{label}")


def ledger(session, task_id: int) -> dict[str, Any]:
    """同一物理产线在新任务里的台账（对象 + 2 通道 + 内置三指标）。"""
    obj = object_registry.create_object(session, task_id, "隔膜过辊轴承", "轴承", part="隔膜过辊")
    ch1 = channel_reg.create_channel(session, task_id, obj.id, "CH1")
    ch2 = channel_reg.create_channel(session, task_id, obj.id, "CH2")
    metric_def.seed_defaults(session, task_id)
    defs = {row.code: row for row in metric_def.list_metrics(session, task_id)}
    return {"object": obj, "ch1": ch1, "ch2": ch2, "defs": defs}


def build_line(session, task_id: int, refs: dict[str, Any]) -> None:
    """卷绕产线：1 设备 / 1 监测对象 / 2 通道 / 4 指标，带参数、固化与阈值。"""
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
        attrs={"类型": "轴承", "部位": "隔膜过辊", "转速": "1500"},
        ref_id=refs["object"].id,
    )
    ch_nodes = {}
    for code in ("CH1", "CH2"):
        ch_nodes[code] = tree.create_node(
            session, task_id, "channel", code, parent_id=comp.id, ref_id=refs[f"ch{code[-1]}"].id
        )
    metric_nodes = {}
    plan = {
        "CH1": ("v_RMS", "a_Peak"),
        "CH2": ("v_RMS", "a_RMS"),
    }
    for channel, codes in plan.items():
        for code in codes:
            metric_nodes[code] = tree.create_node(
                session,
                task_id,
                "metric",
                code,
                parent_id=ch_nodes[channel].id,
                attrs={"code": code, "单位": refs["defs"][code].unit},
                ref_id=refs["defs"][code].id,
            )
    # 未固化只覆盖一项 / 固化快照 / 完全不绑（保留默认）三种都要覆盖到
    metric_assign.assign_metric(
        session, task_id, ch_nodes["CH1"].id, "v_RMS", params={"band_high": 2000}
    )
    metric_assign.assign_metric(
        session,
        task_id,
        ch_nodes["CH1"].id,
        "a_Peak",
        params={"detrend": False},
        params_locked=True,
    )
    metric_assign.assign_metric(
        session, task_id, ch_nodes["CH2"].id, "v_RMS", params={"band_low": 20}
    )
    limits.set_limit(session, task_id, metric_nodes["v_RMS"].id, "v_RMS", 1.8, 2.8)
    limits.set_limit(session, task_id, metric_nodes["a_Peak"].id, "a_Peak", 0.9, 1.6)
    limits.set_limit(session, task_id, ch_nodes["CH1"].id, "v_RMS", 2.1, 3.3)


def outline(rows: list[NodeRow]) -> list[tuple[str, int, str]]:
    """节点轮廓：类型 / 层级深度 / 名称（顺序按 path）。"""
    return [(row.node_type, row.path.count("/"), row.name) for row in rows]


def bindings(session, task_id: int) -> dict[str, tuple[str, dict[str, Any]]]:
    """每个指标节点的绑定状态：(状态, 生效参数)，键是「对象/通道/指标」。

    状态 = ``bound``（未固化）/ ``locked``（固化）/ ``unbound``（没有参数绑定）。
    """
    nodes = {row.id: row for row in tree.list_nodes(session, task_id)}
    assigns = {
        (row.node_id, row.code): row for row in metric_assign.list_assignments(session, task_id)
    }
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for node in nodes.values():
        if node.node_type != "metric" or node.parent_id is None:
            continue
        channel = nodes.get(node.parent_id)
        code = str((node.attrs or {}).get("code") or node.name)
        parent = nodes.get(channel.parent_id) if channel and channel.parent_id else None
        key = f"{parent.name if parent else ''}/{channel.name if channel else ''}/{code}"
        row = assigns.get((channel.id if channel else 0, code))
        if row is None:
            out[key] = ("unbound", {})
            continue
        state = "locked" if row.params_locked else "bound"
        out[key] = (state, metric_assign.effective_params(session, row.id))
    return out


def thresholds_of(session, task_id: int) -> list[dict[str, Any]]:
    payload = tree.export_template(session, task_id)
    return payload.get("thresholds") or []


def normalised(payload: dict[str, Any]) -> dict[str, Any]:
    """去掉随时间/任务变化的字段，用于结构等价比较。"""
    copy = json.loads(json.dumps(payload))
    copy.pop("exported_at", None)
    copy.pop("task", None)
    return copy


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tpl-v2-verify-"))
    engine = make_engine(tmp / "verify.db")
    init_db(engine)
    session = make_session_factory(engine)()

    say("=" * 78)
    say(
        "健康树模板 v2 验收（T1–T4）　生成时间："
        + __import__("datetime").datetime.now().isoformat(" ")
    )
    say(f"临时库：{tmp / 'verify.db'}")
    say("=" * 78)

    # ---------------- 准备：源任务「卷绕产线振动监测」
    src = tasks.create_task(session, name="卷绕产线振动监测")
    src_refs = ledger(session, src.id)
    build_line(session, src.id, src_refs)
    payload = tree.export_template(session, src.id)

    say("")
    say("—— v2 模板样例（源任务「卷绕产线振动监测」导出，节选一个通道） ——")
    sample = json.loads(json.dumps(payload))
    for node in sample["root"]["children"][0]["children"][0]["children"]:
        node["children"] = node["children"][:1]
    say(json.dumps(sample, ensure_ascii=False, indent=2))

    # ---------------- 目标任务：空任务 + 同名台账
    dst = tasks.create_task(session, name="复查导入任务")
    ledger(session, dst.id)

    preview = tree.preview_import(session, dst.id, payload)
    say("")
    say(
        f"预览：树节点 {preview.node_total} 个　指标绑定 {preview.metric_bindings} 个　"
        f"阈值 {preview.threshold_rows} 条　未匹配台账 {len(preview.unmatched)} 处"
        f"　版本 v{preview.version}"
    )
    check("预览未匹配台账为空（不猜不错挂直接放行）", not preview.blocked)

    report = tree.import_template(session, dst.id, payload)
    say(
        f"导入报告：新建节点 {report.node_total} 个　绑定指标 {report.metrics_bound} 个　"
        f"写阈值 {report.thresholds_written} 条　跳过 {len(report.skipped)} 条"
    )

    # ---------------- 验收 1：节点数 / 层级 / 名称
    src_rows = tree.list_nodes(session, src.id)
    dst_rows = tree.list_nodes(session, dst.id)
    src_outline = outline(src_rows)
    dst_outline = outline(dst_rows)
    kinds = ("task", "device", "component", "channel", "metric")
    counts_text = ", ".join(
        f"{kind} {sum(1 for row in dst_outline if row[0] == kind)}" for kind in kinds
    )
    check(
        "1) 节点数/层级/名称一致",
        dst_outline == src_outline,
        f"源 {len(src_outline)} 个 / 目标 {len(dst_outline)} 个；类型分布 {counts_text}",
    )

    # ---------------- 验收 2：逐指标节点参数与 locked diff 为 0
    src_bind = bindings(session, src.id)
    dst_bind = bindings(session, dst.id)
    same_keys = set(src_bind) == set(dst_bind)
    diffs: list[str] = []
    for key, (state, params) in src_bind.items():
        other = dst_bind.get(key)
        if other is None:
            diffs.append(f"{key}：目标任务缺这条绑定")
            continue
        if other[0] != state:
            diffs.append(f"{key}：绑定状态 {state}→{other[0]}")
        for param, value in params.items():
            if other[1].get(param) != value:
                diffs.append(f"{key}：{param} {value}→{other[1].get(param)}")
        extra = {k for k in other[1] if k not in params}
        if extra:
            diffs.append(f"{key}：多出参数 {sorted(extra)}")
    check(
        "2) 每个指标节点参数与 locked 逐节点 diff 为 0（含未绑定节点）",
        same_keys and not diffs,
        f"{len(src_bind)} 个指标节点；diff {len(diffs)} 处" + ("" if not diffs else f"：{diffs}"),
    )
    for key in sorted(src_bind):
        state, params = src_bind[key]
        say(
            f"     · {key}　state={state}　band_low={params.get('band_low')} "
            f"band_high={params.get('band_high')} detrend={params.get('detrend')}"
        )

    # ---------------- 验收 3：阈值条数与数值
    src_thr = thresholds_of(session, src.id)
    dst_thr = thresholds_of(session, dst.id)
    check(
        "3) 阈值条数与数值一致",
        src_thr == dst_thr,
        f"源 {len(src_thr)} 条 / 目标 {len(dst_thr)} 条",
    )
    for row in dst_thr:
        say(
            f"     · {' / '.join(row['path'])} × {row['metric_code']}："
            f"warn {row['warn']}　alert {row['alert']}　"
            f"{row['direction']}　enabled={row['enabled']}"
        )

    # ---------------- 验收 4：再导出一次结构等价
    again = tree.export_template(session, dst.id)
    check(
        "4) 再导出一次结构等价（除 exported_at / task 名）",
        normalised(again) == normalised(payload),
        f"两个模板 JSON 规范化后逐字段相同"
        f"（含 {len(normalised(again).get('thresholds') or [])} 条阈值）",
    )

    # ---------------- 验收 5：v1 旧模板仍能导入
    old = tasks.create_task(session, name="旧模板导入任务")
    v1 = bootstrap.bootstrap_example_tree_payload()
    old_preview = tree.preview_import(session, old.id, v1)
    old_report = tree.import_template(session, old.id, v1)
    check(
        "5) v1 旧模板仍能导入",
        old_report.version == 1
        and old_report.node_counts.get("channel") == 4
        and old_report.node_counts.get("metric") == 12,
        f"v1 导入节点 {old_report.node_counts}；提示「{old_preview.notice[:24]}…」",
    )

    # ---------------- 附加：未匹配台账一次性报错
    empty = tasks.create_task(session, name="空台账任务")
    try:
        tree.import_template(session, empty.id, payload)
    except tree.TreeError as exc:
        text = str(exc)
        listed = text.count("\n  · ")
        expected = 1 + 2 + 4  # 1 个监测对象 + 2 个通道 + 4 个指标
        check(
            "附) 台账缺失时未匹配清单一次性报错（不猜不错挂）",
            "未匹配清单" in text
            and listed == expected
            and len(tree.list_nodes(session, empty.id)) == 1,
            f"一次列全 {listed} 处引用（应有 {expected}）；树未落库，仍只有根节点",
        )
        for line in text.splitlines():
            say("     " + line)
    else:
        raise SystemExit("附) 空台账任务竟然导入成功 —— 说明业务键解析失效")

    # ---------------- 汇总
    say("")
    say("=" * 78)
    say("结论：T1–T4 五条验收全部通过。")
    say("=" * 78)
    session.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n已写：{OUT}")


if __name__ == "__main__":
    main()
