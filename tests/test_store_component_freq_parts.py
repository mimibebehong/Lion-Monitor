"""CF-2：频率参数台账的部件视图 / 编辑保存 / 导入预览 / 节点手填覆盖 / 通道标记解析。

口径（任务书 CF-2）：

- 部件视图：同一 ``part_code`` 的多条频率项合成一行（参考转速、已挂通道、显示名）；
- 编辑保存：改频率值 → ``hz_at_1rpm = value_hz / ref_rpm`` 同步重算；改参考转速 →
  该部件所有频率项一起重算；``scaling=fixed`` 标记不随转速缩放；
- 导入预览：跑完导入逻辑再回滚，库里不留痕，计数与真导入一致；
- 节点手填覆盖：存 ``task_node.attrs_json``（键 ``特征频率:<部件>:<频率项>``），
  优先级 节点手填 > 对象派生，来源标「节点手填」/「对象派生」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lion_monitor.store import component_freq, object_registry, tasks, tree
from lion_monitor.store import db as store_db

ROOT = Path(__file__).resolve().parents[1]
ATTACHMENT = ROOT / "tests" / "data" / "examples" / "监测对象_20260309.csv"


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "cf2.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="频率参数任务")


@pytest.fixture()
def obj(session, task):
    return object_registry.create_object(session, task.id, name="主减速器", obj_type="轴承")


def _channel_node(session, task_id, object_id, code="Chan_1", rpm=None):
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
    return tree.create_node(session, task_id, "channel", code, parent_id=comp.id, attrs=attrs)


# ---------------------------------------------------------------- 部件视图


def test_list_parts_groups_rows_by_part(session, task, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)

    parts = component_freq.list_parts(session, task.id, object_id=obj.id)

    assert len(parts) == 57
    first = parts[0]
    assert first.part_code == "Z1_R"
    assert first.part_name == "右输入级主动锥齿轮"
    assert first.part_type == "齿轮"
    assert first.component_kind == "gear"
    assert first.ref_rpm == pytest.approx(20898.0)
    # 一个部件多通道：通道号多值 + 显示名来自 channel_reg.remark
    assert first.channels == ("Chan_1", "Chan_8")
    assert first.channel_names == (
        "主减右输入组件径向(Z向)_1",
        "主减右并车处径向_8",
    )
    # 频率项按 FREQ_KINDS 顺序，值取原值
    assert list(first.points) == ["om1", "om2", "omx", "om2x"]
    assert first.points["om1"].value_hz == pytest.approx(348.3)
    assert first.points["om1"].hz_at_1rpm == pytest.approx(348.3 / 20898.0)


def test_list_parts_keeps_attachment_order_and_zero_values(session, task, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)

    parts = component_freq.list_parts(session, task.id, object_id=obj.id)
    by_code = {part.part_code: part for part in parts}

    # 4D 轴承有 BPFI/BPFO/FTF/BSF；0 值按原值留痕
    four_d = by_code["4D"]
    assert four_d.points["bpfi"].value_hz == pytest.approx(37.0)
    assert four_d.ref_rpm == pytest.approx(258.26, rel=1e-4)
    zero_parts = [
        part for part in parts if part.points.get("bpfi") and part.points["bpfi"].value_hz == 0
    ]
    assert zero_parts, "附件里 0 值特征频率应留痕"
    # 首尾顺序与附件一致（Z1_R 最先）
    assert [part.part_code for part in parts][:3] == ["Z1_R", "Z2_R", "1A_R"]


# ---------------------------------------------------------------- 编辑保存


def test_save_part_updates_value_and_hz_at_1rpm(session, task, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)

    changed = component_freq.save_part(session, obj.id, "Z1_R", values={"om1": 350.0})

    assert changed == 1
    part = next(
        p
        for p in component_freq.list_parts(session, task.id, object_id=obj.id)
        if p.part_code == "Z1_R"
    )
    assert part.points["om1"].value_hz == pytest.approx(350.0)
    assert part.points["om1"].hz_at_1rpm == pytest.approx(350.0 / 20898.0)
    # 同部件其他频率项不受影响
    assert part.points["om2"].value_hz == pytest.approx(696.6)


def test_save_part_edits_ref_rpm_and_recomputes_all_points(session, task, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)

    component_freq.save_part(session, obj.id, "Z1_R", ref_rpm=20900.0)

    part = next(
        p
        for p in component_freq.list_parts(session, task.id, object_id=obj.id)
        if p.part_code == "Z1_R"
    )
    assert part.ref_rpm == pytest.approx(20900.0)
    assert part.points["om1"].hz_at_1rpm == pytest.approx(348.3 / 20900.0)
    assert part.points["omx"].hz_at_1rpm == pytest.approx(8708.3 / 20900.0)


def test_save_part_marks_fixed_scaling(session, task, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    node = _channel_node(session, task.id, obj.id, rpm=21000.0)

    component_freq.save_part(
        session, obj.id, "Z1_R", values={"om1": 100.0}, scalings={"om1": "fixed"}
    )

    resolved = component_freq.resolve_component_freq(session, node.id, "om1", part_code="Z1_R")
    assert resolved is not None
    assert resolved.value_hz == pytest.approx(100.0)
    assert resolved.scaling == "fixed"
    assert resolved.source == "object"


def test_save_part_creates_missing_freq_kind(session, task, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    before = next(
        p
        for p in component_freq.list_parts(session, task.id, object_id=obj.id)
        if p.part_code == "Z1_R"
    )
    assert "bpfi" not in before.points

    component_freq.save_part(session, obj.id, "Z1_R", values={"bpfi": 1234.0})

    after = next(
        p
        for p in component_freq.list_parts(session, task.id, object_id=obj.id)
        if p.part_code == "Z1_R"
    )
    assert after.points["bpfi"].value_hz == pytest.approx(1234.0)
    assert after.points["bpfi"].hz_at_1rpm == pytest.approx(1234.0 / 20898.0)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"ref_rpm": 0.0}, "参考转速"),
        ({"values": {"om1": -1.0}}, "频率值"),
        ({"values": {"xx1": 100.0}}, "未知频率项"),
        ({"scalings": {"om1": "boom"}}, "缩放方式"),
    ],
)
def test_save_part_rejects_invalid_input(session, obj, kwargs, message):
    component_freq.import_csv(session, obj.id, ATTACHMENT)

    with pytest.raises(component_freq.ComponentFreqError) as excinfo:
        component_freq.save_part(session, obj.id, "Z1_R", **kwargs)

    assert message in str(excinfo.value)


def test_save_part_unknown_part_raises(session, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)

    with pytest.raises(component_freq.ComponentFreqError) as excinfo:
        component_freq.save_part(session, obj.id, "NOPE", values={"om1": 1.0})

    assert "部件" in str(excinfo.value)


# ---------------------------------------------------------------- 导入预览


def test_preview_import_reports_counts_without_writing(session, task, obj):
    preview = component_freq.preview_import(session, obj.id, ATTACHMENT)

    assert preview.rows_read == 131
    assert preview.parts == 57
    assert preview.records == 250
    assert preview.created == 250
    assert preview.updated == 0
    assert preview.skipped == 0
    assert preview.channels == 11
    # 预览只读：库里一条都没写
    assert component_freq.list_freqs(session, task.id, object_id=obj.id) == []
    assert component_freq.list_channels_of(session, obj.id) == []


def test_preview_import_after_real_import_shows_only_updates(session, task, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    component_freq.save_part(session, obj.id, "Z1_R", values={"om1": 999.0})

    preview = component_freq.preview_import(session, obj.id, ATTACHMENT)

    assert preview.created == 0
    assert preview.updated == 1
    assert preview.unchanged == 249
    # 回滚后库里仍是编辑后的值
    part = next(
        p
        for p in component_freq.list_parts(session, task.id, object_id=obj.id)
        if p.part_code == "Z1_R"
    )
    assert part.points["om1"].value_hz == pytest.approx(999.0)


# ---------------------------------------------------------------- 节点手填覆盖


def test_node_override_wins_over_object_derivation(session, task, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    node = _channel_node(session, task.id, obj.id, rpm=21000.0)

    derived = component_freq.resolve_component_freq(session, node.id, "om1", part_code="Z1_R")
    assert derived is not None and derived.source == "object"
    assert derived.value_hz == pytest.approx(348.3 / 20898.0 * 21000.0)

    component_freq.set_node_freq_override(session, node.id, "Z1_R", "om1", 400.0)

    overridden = component_freq.resolve_component_freq(session, node.id, "om1", part_code="Z1_R")
    assert overridden is not None
    assert overridden.value_hz == pytest.approx(400.0)
    assert overridden.source == "node"
    assert overridden.scaling == "fixed"
    assert overridden.rpm_used is None


def test_node_override_cleared_falls_back_to_object_derivation(session, task, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    node = _channel_node(session, task.id, obj.id, rpm=21000.0)
    component_freq.set_node_freq_override(session, node.id, "Z1_R", "om1", 400.0)

    component_freq.set_node_freq_override(session, node.id, "Z1_R", "om1", None)

    assert component_freq.parse_freq_overrides(tree.get_node(session, node.id).attrs) == {}
    resolved = component_freq.resolve_component_freq(session, node.id, "om1", part_code="Z1_R")
    assert resolved is not None
    assert resolved.source == "object"
    assert resolved.value_hz == pytest.approx(348.3 / 20898.0 * 21000.0)


def test_override_on_object_node_is_inherited_by_channel(session, task, obj):
    """覆盖写在监测对象节点上，通道节点解析时同样生效（沿树往上 3 级）。"""
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    channel = _channel_node(session, task.id, obj.id, rpm=21000.0)
    object_node_id = channel.parent_id
    assert object_node_id is not None

    component_freq.set_node_freq_override(session, object_node_id, "Z1_R", "om2", 700.0)

    resolved = component_freq.resolve_component_freq(session, channel.id, "om2", part_code="Z1_R")
    assert resolved is not None
    assert resolved.value_hz == pytest.approx(700.0)
    assert resolved.source == "node"


def test_resolve_marks_for_channel_uses_ledger_and_override(session, task, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    node = _channel_node(session, task.id, obj.id, rpm=21000.0)

    marks = component_freq.resolve_freq_marks(session, task.id, "Chan_1")
    by_key = {(mark.part_code, mark.freq_kind): mark for mark in marks}

    assert ("Z1_R", "om1") in by_key
    assert by_key[("Z1_R", "om1")].value_hz == pytest.approx(348.3 / 20898.0 * 21000.0)
    # 0 值特征频率不出标记线（CF-1 口径）
    assert all(mark.value_hz > 0 for mark in marks)
    # 只出挂在该通道上的部件：Chan_16 的 4D 不在 Chan_1 里
    assert all(
        "Chan_1" in part.channels
        for part in component_freq.list_parts(session, task.id)
        if part.part_code in {m.part_code for m in marks}
    )

    component_freq.set_node_freq_override(session, node.id, "Z1_R", "om1", 400.0)
    marks = component_freq.resolve_freq_marks(session, task.id, "Chan_1")
    overridden = next(m for m in marks if (m.part_code, m.freq_kind) == ("Z1_R", "om1"))
    assert overridden.value_hz == pytest.approx(400.0)
    assert overridden.source == "node"


def test_resolve_marks_for_unknown_channel_is_empty(session, task, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)

    assert component_freq.resolve_freq_marks(session, task.id, "Chan_99") == []
