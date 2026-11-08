"""CF-1 验收脚本：部件频率台账导入 / 归一化 / 多通道 / 导出往返 / 转速三级。

只读写临时库（``tempfile``），不碰 ``data/lion.db``。运行：

    .venv/bin/python scripts/verify_cf1_component_freq.py
"""

from __future__ import annotations

import csv
import io
import tempfile
from collections import Counter
from pathlib import Path

from lion_monitor.store import component_freq, datasets, object_registry, tasks, tree
from lion_monitor.store.db import init_db, make_engine, make_session_factory

ROOT = Path(__file__).resolve().parents[1]
ATTACHMENT = ROOT / "docs" / "examples" / "监测对象_20260309.csv"
ATTACHMENT_GBK = ROOT / "docs" / "examples" / "监测对象_20260309_GBK原始.csv"


def _build_tree(session, task_id, object_id, rpm=None):
    root = tree.root_node(session, task_id)
    device = tree.create_node(session, task_id, "device", "主减", parent_id=root.id)
    comp = tree.create_node(
        session,
        task_id,
        "component",
        "主减速器",
        parent_id=device.id,
        ref_type="monitor_object",
        ref_id=object_id,
    )
    attrs = {"转速": rpm} if rpm is not None else {}
    return tree.create_node(session, task_id, "channel", "Chan_1", parent_id=comp.id, attrs=attrs)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(Path(tmp) / "cf1.db")
        init_db(engine)
        with make_session_factory(engine)() as session:
            task = tasks.create_task(session, name="CF-1 验收")
            obj = object_registry.create_object(session, task.id, name="主减速器", obj_type="轴承")

            print("== 1. 导入附件（UTF-8） ==")
            first = component_freq.import_csv(session, obj.id, ATTACHMENT)
            print(first.describe())
            rows = component_freq.list_freqs(session, task.id, object_id=obj.id)
            print(
                "  各 freq_kind 记录数：", dict(sorted(Counter(r.freq_kind for r in rows).items()))
            )
            print("  部件数（去重 part_code）：", len({r.part_code for r in rows}))
            print(
                "  通道数（channel_reg）：", len(component_freq.list_channels_of(session, obj.id))
            )
            print(
                "  部件类型 → component_kind：",
                sorted({(r.part_type, r.component_kind) for r in rows}),
            )

            print("== 2. 重复导入（幂等） ==")
            second = component_freq.import_csv(session, obj.id, ATTACHMENT)
            print(second.describe())

            print("== 3. 归一化 ==")
            z1r = {
                r.freq_kind: r
                for r in component_freq.list_freqs(
                    session, task.id, object_id=obj.id, part_code="Z1_R"
                )
            }
            om1, omx = z1r["om1"], z1r["omx"]
            print(
                f"  Z1_R OM1: value={om1.value_hz:g} Hz, ref_rpm={om1.ref_rpm:g}, "
                f"hz_at_1rpm={om1.hz_at_1rpm:.10f}（1/60={1 / 60:.10f}）"
            )
            print(
                f"  Z1_R OMx: value={omx.value_hz:g} Hz → 齿数 = "
                f"hz_at_1rpm×ref_rpm/OM1 = {omx.hz_at_1rpm * omx.ref_rpm / om1.value_hz:.4f}"
            )
            d4 = component_freq.list_freqs(
                session, task.id, object_id=obj.id, part_code="4D", freq_kind="bpfi"
            )[0]
            print(
                f"  4D BPFI: value={d4.value_hz:g} Hz, ref_rpm={d4.ref_rpm:g}, "
                f"hz_at_1rpm={d4.hz_at_1rpm:.8f}, 阶次={d4.hz_at_1rpm * 60:.4f}"
            )
            for code in ("1A_R", "1E-F_R"):
                row = component_freq.list_freqs(
                    session, task.id, object_id=obj.id, part_code=code, freq_kind="om1"
                )[0]
                print(f"  {code}: OM1 精度并列 → ref_rpm={row.ref_rpm:g}")

            print("== 4. 同部件跨通道 ==")
            om1_rows = component_freq.list_freqs(
                session, task.id, object_id=obj.id, part_code="Z1_R", freq_kind="om1"
            )
            print(f"  Z1_R 记录数（om1）: {len(om1_rows)}")
            print(f"  Z1_R 通道（channel_code 多值）: {om1.channels}")
            chan8_rows = component_freq.list_freqs(
                session, task.id, object_id=obj.id, channel_code="Chan_8"
            )
            print(f"  Chan_8 上的部件数: {len({r.part_code for r in chan8_rows})}")

            print("== 5. GBK 源文件导入（新对象） ==")
            gbk_obj = object_registry.create_object(
                session, task.id, name="主减速器GBK", obj_type="轴承"
            )
            gbk = component_freq.import_csv(session, gbk_obj.id, ATTACHMENT_GBK)
            print(gbk.describe())

            print("== 6. 导出 ==")
            text = component_freq.export_csv_text(session, task.id, object_id=obj.id)
            body = list(csv.reader(io.StringIO(text)))
            print("  列序：", body[0])
            print(f"  行数（不含表头）: {len(body) - 1}")
            target = Path(tmp) / "导出.csv"
            target.write_bytes(component_freq.export_csv_bytes(session, task.id, object_id=obj.id))
            decoded = target.read_bytes().decode("gbk")
            print(f"  GBK 解码：OK，{len(decoded)} 字符；前 120 字：{decoded[:120]!r}")
            sample = [line for line in decoded.splitlines() if "Z1_R" in line][0]
            print(f"  Z1_R 行：{sample}")
            chan8 = component_freq.export_csv_text(
                session, task.id, object_id=obj.id, channel_code="Chan_8"
            )
            print(
                "  按 Chan_8 过滤导出行数：",
                len(list(csv.reader(io.StringIO(chan8)))) - 1,
            )
            roundtrip = component_freq.import_csv(session, obj.id, target)
            print("  导出文件再导入：", roundtrip.describe())

            print("== 7. 转速三级 / 频率派生 ==")
            channel = _build_tree(session, task.id, obj.id)
            print("  三者皆无：", component_freq.resolve_node_rpm(session, channel.id))
            object_registry.set_rated_rpm(session, obj.id, 20898.0)
            print("  只有对象额定：", component_freq.resolve_node_rpm(session, channel.id))
            dataset = datasets.create_dataset(
                session,
                task.id,
                name="批次1",
                source_dir=str(Path(tmp) / "batch"),
                collected_at="2026-11-02 10:00",
            )
            datasets.set_measured_rpm(session, dataset.id, 20900.0)
            print(
                "  加批次实测：",
                component_freq.resolve_node_rpm(session, channel.id, dataset.id),
            )
            tree.update_node(session, channel.id, attrs={"转速": 21000.0})
            print(
                "  再手填节点：",
                component_freq.resolve_node_rpm(session, channel.id, dataset.id),
            )
            hit = component_freq.resolve_component_freq(
                session, channel.id, "om1", part_code="Z1_R"
            )
            assert hit is not None
            print(
                f"  Z1_R OM1 @21000 rpm → {hit.value_hz:.4f} Hz"
                f"（hz_at_1rpm={om1.hz_at_1rpm:.8f} × 21000）来源={hit.rpm_source}"
            )
            bpfi = component_freq.resolve_component_freq(
                session, channel.id, "bpfi", part_code="4D"
            )
            assert bpfi is not None
            print(f"  4D BPFI @21000 rpm → {bpfi.value_hz:.4f} Hz")
            gated = component_freq.resolve_component_freq(
                session, channel.id, "bsf", part_code="Z1_R"
            )
            print("  Z1_R 未配 BSF：", gated)
            zero = component_freq.list_freqs(
                session, task.id, object_id=obj.id, part_code="1G-H_R", freq_kind="bpfi"
            )[0]
            print(
                f"  1G-H_R BPFI 台账原值 {zero.value_hz:g} Hz（留痕）→ 解析：",
                component_freq.resolve_component_freq(
                    session, channel.id, "bpfi", part_code="1G-H_R"
                ),
            )


if __name__ == "__main__":
    main()
