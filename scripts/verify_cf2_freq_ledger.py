"""CF-2 验收脚本：频率参数页数据口径 / 编辑保存 / 导入预览 / 导出往返 / 节点覆盖 / 标记线。

只读写临时库（``tempfile``），不碰 ``data/lion.db``。运行：

    .venv/bin/python scripts/verify_cf2_freq_ledger.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from lion_monitor.store import component_freq, datasets, object_registry, tasks, tree
from lion_monitor.store.db import init_db, make_engine, make_session_factory

ROOT = Path(__file__).resolve().parents[1]
ATTACHMENT = ROOT / "docs" / "examples" / "监测对象_20260309.csv"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        engine = make_engine(work / "cf2.db")
        init_db(engine)
        with make_session_factory(engine)() as session:
            task = tasks.create_task(session, name="CF-2 验收")
            obj = object_registry.create_object(session, task.id, name="主减速器", obj_type="轴承")
            object_registry.set_rated_rpm(session, obj.id, 21000.0)

            root = tree.root_node(session, task.id)
            device = tree.create_node(session, task.id, "device", "主减", parent_id=root.id)
            comp = tree.create_node(
                session,
                task.id,
                "component",
                "主减速器",
                parent_id=device.id,
                ref_type="monitor_object",
                ref_id=obj.id,
            )
            channel = tree.create_node(session, task.id, "channel", "Chan_1", parent_id=comp.id)
            dataset = datasets.create_dataset(
                session, task.id, "批次01", str(work / "b1"), "2026-11-02 10:00:00"
            )

            print("== 1. 导入预览（空对象） ==")
            preview = component_freq.preview_import(session, obj.id, ATTACHMENT)
            print("  预览：", preview.describe())
            print("  预览后台账行数：", len(component_freq.list_freqs(session, task.id)))

            print("== 2. 真导入 + 部件视图 ==")
            summary = component_freq.import_csv(session, obj.id, ATTACHMENT)
            print("  导入：", summary.describe())
            parts = component_freq.list_parts(session, task.id, object_id=obj.id)
            first = parts[0]
            print(f"  部件 {len(parts)} 个；首个：{first.part_code} · {first.part_name}")
            print(f"  已挂通道：{first.channels_text}")
            print(
                f"  参考转速 {first.ref_rpm:g} rpm；OM1={first.points['om1'].value_hz:g} Hz"
                f"（阶次 {first.points['om1'].order:.4f}×）"
            )

            print("== 3. 编辑一个部件 OM1 → hz_at_1rpm 同步 ==")
            changed = component_freq.save_part(session, obj.id, "Z1_R", values={"om1": 350.0})
            edited = next(
                part
                for part in component_freq.list_parts(session, task.id, object_id=obj.id)
                if part.part_code == "Z1_R"
            )
            print(f"  改动 {changed} 项：value_hz={edited.points['om1'].value_hz:g}，"
                  f"hz_at_1rpm={edited.points['om1'].hz_at_1rpm:.10f}"
                  f"（=350/20898={350 / 20898:.10f}）")

            print("== 4. 导出往返 ==")
            exported = work / "export.csv"
            exported.write_bytes(component_freq.export_csv_bytes(session, task.id, object_id=obj.id))
            other = object_registry.create_object(session, task.id, name="空对象", obj_type="轴承")
            once = component_freq.import_csv(session, other.id, exported)
            twice = component_freq.import_csv(session, other.id, exported)
            print("  导入导出文件：", once.describe())
            print("  再导一次：", twice.describe())

            print("== 5. 节点手填覆盖 ==")
            derived = component_freq.resolve_component_freq(
                session, channel.id, "om1", part_code="Z1_R"
            )
            print(
                f"  覆盖前：{derived.value_hz:g} Hz（来源 {derived.source}，"
                f"转速 {derived.rpm_used:g} rpm / {derived.rpm_source}）"
            )
            component_freq.set_node_freq_override(session, channel.id, "Z1_R", "om1", 400.0)
            overridden = component_freq.resolve_component_freq(
                session, channel.id, "om1", part_code="Z1_R"
            )
            print(f"  覆盖后：{overridden.value_hz:g} Hz（来源 {overridden.source}）")
            component_freq.set_node_freq_override(session, channel.id, "Z1_R", "om1", None)
            restored = component_freq.resolve_component_freq(
                session, channel.id, "om1", part_code="Z1_R"
            )
            print(f"  清除后：{restored.value_hz:g} Hz（来源 {restored.source}）")

            print("== 6. 分析页标记线（带批次转速） ==")
            datasets.set_measured_rpm(session, dataset.id, 21000.0)
            marks = component_freq.resolve_freq_marks(
                session, task.id, "Chan_1", dataset_id=dataset.id
            )
            z1 = next(m for m in marks if (m.part_code, m.freq_kind) == ("Z1_R", "om1"))
            print(f"  台账标记 {len(marks)} 条（Chan_1 组装上的部件）；"
                  f"Z1_R OM1={z1.value_hz:g} Hz（来源 {z1.source}，"
                  f"转速 {z1.rpm_used:g} rpm / {z1.rpm_source}）")
            component_freq.set_node_freq_override(session, channel.id, "Z1_R", "om1", 400.0)
            marks = component_freq.resolve_freq_marks(
                session, task.id, "Chan_1", dataset_id=dataset.id
            )
            z1 = next(m for m in marks if (m.part_code, m.freq_kind) == ("Z1_R", "om1"))
            print(f"  覆盖后标记：Z1_R OM1={z1.value_hz:g} Hz（来源 {z1.source}）")
            print("  未配置通道 Chan_99：", component_freq.resolve_freq_marks(session, task.id, "Chan_99"))


if __name__ == "__main__":
    main()
