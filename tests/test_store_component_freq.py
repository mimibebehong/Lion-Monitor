"""CF-1：部件频率台账 ``component_freq`` + 附件 CSV 导入导出 + 转速/频率解析。

口径（任务书 CF-1）：

- 唯一键 ``(object_id, part_code, freq_kind)``，``deleted_at IS NULL`` 部分唯一索引
- ``hz_at_1rpm = value_hz / ref_rpm``（归一化），``scaling=speed`` 随转速缩放、
  ``fixed`` 直接用 ``value_hz``
- 附件 131 行 → 57 个部件、11 个通道；同部件跨通道只建一条部件记录
- 导出与附件同列序、GBK 可读，再导入 0 变化（往返幂等）
"""

from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect

from lion_monitor.store import (
    component_freq,
    datasets,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db
from lion_monitor.store.models import ComponentFreq

ROOT = Path(__file__).resolve().parents[1]
ATTACHMENT = ROOT / "tests" / "data" / "examples" / "监测对象_20260309.csv"
ATTACHMENT_GBK = ROOT / "tests" / "data" / "examples" / "监测对象_20260309_GBK原始.csv"

#: 附件里各 freq_kind 的「部件数」非空计数（见设计文档 §S3 / 任务书验收 1）
EXPECTED_PER_KIND = {
    "om1": 57,
    "om2": 27,
    "omx": 23,
    "om2x": 23,
    "bpfi": 30,
    "bpfo": 30,
    "ftf": 30,
    "bsf": 30,
}


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "cf1.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="部件频率任务")


@pytest.fixture()
def obj(session, task):
    return object_registry.create_object(session, task.id, name="主减速器", obj_type="轴承")


def _channel_node(session, task_id, object_id, code="Chan_1", rpm=None, name=None):
    """建 设备 → 监测对象(引台账) → 通道 的树链路。"""
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
    return tree.create_node(
        session, task_id, "channel", code, parent_id=comp.id, attrs=attrs, sort=0
    )


# ---------------------------------------------------------------- 表结构 / 迁移


def test_component_freq_table_has_required_columns(session):
    inspector = inspect(session.get_bind())
    assert "component_freq" in inspector.get_table_names()
    columns = {col["name"] for col in inspector.get_columns("component_freq")}
    required = {
        "object_id",
        "part_code",
        "part_name",
        "part_type",
        "channel_code",
        "component_kind",
        "ref_rpm",
        "hz_at_1rpm",
        "scaling",
        "freq_kind",
        "value_hz",
        "geom_d",
        "note",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    assert required <= columns
    object_columns = {col["name"] for col in inspector.get_columns("monitor_object")}
    assert "rated_rpm" in object_columns
    dataset_columns = {col["name"] for col in inspector.get_columns("dataset")}
    assert "measured_rpm" in dataset_columns


def test_legacy_db_gets_rated_and_measured_rpm_columns(tmp_path):
    """老库（monitor_object 无 rated_rpm、dataset 无 measured_rpm）跑 init_db 自动补列。"""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE monitor_object (
                id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                name VARCHAR(128) NOT NULL,
                obj_type VARCHAR(32) NOT NULL,
                part VARCHAR(64) NOT NULL,
                remark TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                deleted_at DATETIME,
                PRIMARY KEY (id)
            );
            CREATE TABLE dataset (
                id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                code VARCHAR(32) NOT NULL,
                name VARCHAR(255) NOT NULL,
                source_dir TEXT NOT NULL,
                channel_count INTEGER NOT NULL,
                started_at DATETIME,
                duration_s FLOAT NOT NULL,
                validity VARCHAR(16) NOT NULL,
                validity_note TEXT NOT NULL,
                imported_at DATETIME NOT NULL,
                deleted_at DATETIME,
                PRIMARY KEY (id)
            );
            """
        )
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    with sqlite3.connect(path) as conn:
        object_columns = {row[1] for row in conn.execute("PRAGMA table_info(monitor_object)")}
        dataset_columns = {row[1] for row in conn.execute("PRAGMA table_info(dataset)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "rated_rpm" in object_columns
    assert "measured_rpm" in dataset_columns
    assert "component_freq" in tables


def test_unique_key_per_object_part_freq_kind(session, task, obj):
    """同 (object, part, kind) 未软删只允许一行。"""
    model = ComponentFreq(
        task_id=task.id,
        object_id=obj.id,
        part_code="Z1_R",
        part_name="右输入级主动锥齿轮",
        part_type="齿轮",
        channel_code="Chan_1",
        component_kind="gear",
        ref_rpm=20898.0,
        hz_at_1rpm=1 / 60,
        scaling="speed",
        freq_kind="om1",
        value_hz=348.3,
    )
    session.add(model)
    session.commit()
    duplicate = ComponentFreq(
        task_id=task.id,
        object_id=obj.id,
        part_code="Z1_R",
        part_name="右输入级主动锥齿轮",
        part_type="齿轮",
        channel_code="Chan_1",
        component_kind="gear",
        ref_rpm=20898.0,
        hz_at_1rpm=1 / 60,
        scaling="speed",
        freq_kind="om1",
        value_hz=348.3,
    )
    session.add(duplicate)
    with pytest.raises(Exception, match="UNIQUE|Integrity"):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------- 导入


def test_import_attachment_creates_57_parts_and_250_freq_rows(session, obj):
    summary = component_freq.import_csv(session, obj.id, ATTACHMENT)

    assert summary.rows_read == 131
    assert summary.created == 250
    assert summary.updated == 0
    assert summary.skipped == 0
    assert summary.parts == 57
    assert summary.channels == 11

    rows = component_freq.list_freqs(session, obj.task_id, object_id=obj.id)
    assert len(rows) == 250
    per_kind: dict[str, int] = {}
    for row in rows:
        per_kind[row.freq_kind] = per_kind.get(row.freq_kind, 0) + 1
    assert per_kind == EXPECTED_PER_KIND


def test_second_import_is_idempotent(session, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    again = component_freq.import_csv(session, obj.id, ATTACHMENT)

    assert again.created == 0
    assert again.updated == 0
    assert again.unchanged == 250
    assert again.skipped == 0
    assert len(component_freq.list_freqs(session, obj.task_id, object_id=obj.id)) == 250


def test_import_gbk_source_same_result(session, task):
    gbk_obj = object_registry.create_object(session, task.id, name="主减速器GBK", obj_type="轴承")
    summary = component_freq.import_csv(session, gbk_obj.id, ATTACHMENT_GBK)

    assert summary.rows_read == 131
    assert summary.created == 250
    assert summary.parts == 57
    assert summary.channels == 11


def test_import_bad_row_is_skipped_without_aborting(session, tmp_path, obj):
    source = tmp_path / "bad.csv"
    lines = [
        "序号,振动通道名称,部件类型,监测部件编号,监测部件名称,OM1,OM2,OMx,OM2x,"
        "外圈故障特征频率BPFI,内圈故障特征频率BPF0,保持架故障特征频率FTF,"
        "滚动体故障特征频率BSF,D",
        "Chan_1,通道一,齿轮,X1,齿轮一,348.3,696.6,,,,,,,",
        "Chan_1,通道一,齿轮,X2,齿轮二,不是数字,,,,,,,,",
        "Chan_1,通道一,轴承,X3,轴承三,95.7,,,,123,456,789,111,77.5",
        "Chan_1,通道一,轴承,X4,轴承四,,696.6,,,123,456,789,111,77.5",
    ]
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = component_freq.import_csv(session, obj.id, source)

    # X1: om1/om2；X2 OM1 非数字 → 跳过；X3: om1+bpfi/bpfo/ftf/bsf；
    # X4 只有 OM2、无 OM1 → 无法反推参考转速
    assert summary.created == 7
    assert summary.skipped == 2
    assert len(summary.skipped_details) == 2
    assert "第 3 行" in summary.skipped_details[0]
    assert "X4" in summary.skipped_details[1]


def test_import_keeps_zero_values(session, obj):
    """附件里 BPFI/BPFO/BSF 有 0：按非空计数保留（0 不是空），报告里单独说明。"""
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    rows = component_freq.list_freqs(
        session, obj.task_id, object_id=obj.id, part_code="1G-H_R", freq_kind="bpfi"
    )
    assert len(rows) == 1
    assert rows[0].value_hz == 0.0


def test_part_kind_mapping_covers_four_types(session, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    rows = component_freq.list_freqs(session, obj.task_id, object_id=obj.id)
    kinds = {row.component_kind for row in rows}
    assert kinds == {"gear", "bearing", "planet", "shaft"}


# ---------------------------------------------------------------- 归一化


def test_z1r_normalization_om1_and_omx(session, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    rows = {
        row.freq_kind: row
        for row in component_freq.list_freqs(
            session, obj.task_id, object_id=obj.id, part_code="Z1_R"
        )
    }
    om1 = rows["om1"]
    assert om1.ref_rpm == pytest.approx(20898.0)
    assert om1.value_hz == pytest.approx(348.3)
    assert om1.hz_at_1rpm == pytest.approx(1 / 60, rel=1e-9)
    assert om1.scaling == "speed"

    omx = rows["omx"]
    assert omx.ref_rpm is not None
    # 齿数 = hz_at_1rpm × ref_rpm ÷ OM1 = 8708.3 / 348.3 ≈ 25
    # （表内 OM1 只有 4 位有效数字，算出来 25.0023，四舍五入为 25）
    assert omx.hz_at_1rpm * omx.ref_rpm / om1.value_hz == pytest.approx(25.0, abs=0.01)
    assert omx.hz_at_1rpm * 60 == pytest.approx(25.0, abs=0.01)


def test_4d_bearing_ref_rpm_and_bpfi_order(session, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    rows = component_freq.list_freqs(
        session, obj.task_id, object_id=obj.id, part_code="4D", freq_kind="bpfi"
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.ref_rpm == pytest.approx(258.26)
    # 阶次 = value_hz ÷ (ref_rpm / 60) = 37 / 4.304333333 ≈ 8.596
    assert row.hz_at_1rpm * 60 == pytest.approx(8.596, abs=5e-3)


def test_om1_mode_precision_rule_for_1a_r(session, obj):
    """1A_R 两行 OM1 精度不同（348.3 / 348.3333333）→ 并列取高精度 → ref_rpm = 20900。"""
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    rows = component_freq.list_freqs(
        session, obj.task_id, object_id=obj.id, part_code="1A_R", freq_kind="om1"
    )
    assert len(rows) == 1
    assert rows[0].ref_rpm == pytest.approx(20900.0)
    assert rows[0].hz_at_1rpm == pytest.approx(1 / 60, rel=1e-9)


# ---------------------------------------------------------------- 多通道


def test_part_on_two_channels_is_one_part_record(session, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    rows = component_freq.list_freqs(
        session, obj.task_id, object_id=obj.id, part_code="Z1_R", freq_kind="om1"
    )
    assert len(rows) == 1
    assert rows[0].channels == ("Chan_1", "Chan_8")

    by_channel = component_freq.list_freqs(
        session, obj.task_id, object_id=obj.id, channel_code="Chan_8"
    )
    assert {row.part_code for row in by_channel} == {
        row.part_code
        for row in component_freq.list_freqs(session, obj.task_id, object_id=obj.id)
        if "Chan_8" in row.channels
    }
    assert "Z1_R" in {row.part_code for row in by_channel}


def test_channel_display_names_recorded_in_channel_reg(session, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    channels = {row.code: row.remark for row in component_freq.list_channels_of(session, obj.id)}
    assert channels["Chan_1"] == "主减右输入组件径向(Z向)_1"
    assert channels["Chan_8"] == "主减右并车处径向_8"


# ---------------------------------------------------------------- 导出


def test_export_same_header_and_row_count_as_attachment(session, task, tmp_path):
    obj = object_registry.create_object(session, task.id, name="主减速器导出", obj_type="轴承")
    component_freq.import_csv(session, obj.id, ATTACHMENT)

    text = component_freq.export_csv_text(session, obj.task_id, object_id=obj.id)
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    assert header == list(component_freq.CSV_COLUMNS)
    body = list(reader)
    assert len(body) == 131

    raw = component_freq.export_csv_bytes(session, obj.task_id, object_id=obj.id)
    decoded = raw.decode("gbk")
    assert "监测部件编号" in decoded

    target = tmp_path / "export.csv"
    target.write_bytes(raw)
    roundtrip = component_freq.import_csv(session, obj.id, target)
    assert roundtrip.created == 0
    assert roundtrip.updated == 0
    assert roundtrip.skipped == 0
    assert roundtrip.unchanged == 250


def test_export_row_values_match_attachment(session, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    text = component_freq.export_csv_text(session, obj.task_id, object_id=obj.id)
    rows = list(csv.DictReader(io.StringIO(text)))
    z1r = [row for row in rows if row["监测部件编号"] == "Z1_R"]
    assert [row["序号"] for row in z1r] == ["Chan_1", "Chan_8"]
    assert z1r[0]["OM1"] == "348.3"
    assert z1r[0]["OMx"] == "8708.3"
    assert z1r[0]["振动通道名称"] == "主减右输入组件径向(Z向)_1"


def test_export_filter_by_channel(session, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    text = component_freq.export_csv_text(
        session, obj.task_id, object_id=obj.id, channel_code="Chan_8"
    )
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 16
    assert {row["序号"] for row in rows} == {"Chan_8"}


# ---------------------------------------------------------------- 转速 / 频率解析


def test_resolve_rpm_three_levels(session, task, obj):
    object_registry.set_rated_rpm(session, obj.id, 1450.0)
    channel = _channel_node(session, task.id, obj.id, code="Chan_1")

    rpm, source = component_freq.resolve_node_rpm(session, channel.id)
    assert (rpm, source) == (1450.0, "rated")

    dataset = datasets.create_dataset(
        session, task.id, name="批次1", source_dir="/tmp/x", collected_at="2026-11-02 10:00"
    )
    datasets.set_measured_rpm(session, dataset.id, 1500.0)
    rpm, source = component_freq.resolve_node_rpm(session, channel.id, dataset.id)
    assert (rpm, source) == (1500.0, "measured")

    tree.update_node(session, channel.id, attrs={"转速": 1480.0})
    rpm, source = component_freq.resolve_node_rpm(session, channel.id, dataset.id)
    assert (rpm, source) == (1480.0, "node")


def test_resolve_rpm_returns_none_without_any_source(session, task, obj):
    channel = _channel_node(session, task.id, obj.id, code="Chan_1")
    assert component_freq.resolve_node_rpm(session, channel.id) == (None, None)


def test_resolve_component_freq_scales_with_rpm(session, task, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    channel = _channel_node(session, task.id, obj.id, code="Chan_1")

    hit = component_freq.resolve_component_freq(
        session, channel.id, "om1", rpm_actual=20898.0, part_code="Z1_R"
    )
    assert hit is not None
    assert hit.value_hz == pytest.approx(348.3, rel=1e-9)
    assert hit.scaling == "speed"
    assert hit.rpm_used == pytest.approx(20898.0)

    # 不给 rpm_actual 时走三级解析（对象额定）
    object_registry.set_rated_rpm(session, obj.id, 300.0)
    hit = component_freq.resolve_component_freq(session, channel.id, "om1", part_code="Z1_R")
    assert hit is not None
    assert hit.value_hz == pytest.approx(5.0, rel=1e-9)
    assert hit.rpm_used == pytest.approx(300.0)


def test_resolve_component_freq_gates_without_rpm(session, task, obj):
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    channel = _channel_node(session, task.id, obj.id, code="Chan_1")
    assert (
        component_freq.resolve_component_freq(session, channel.id, "bpfi", part_code="4D") is None
    )


def test_resolve_fixed_freq_ignores_rpm(session, task, obj):
    model = ComponentFreq(
        task_id=task.id,
        object_id=obj.id,
        part_code="2X_LF",
        part_name="2 倍线频",
        part_type="定频",
        channel_code="Chan_1",
        component_kind="other",
        ref_rpm=None,
        hz_at_1rpm=0.0,
        scaling="fixed",
        freq_kind="om1",
        value_hz=100.0,
    )
    session.add(model)
    session.commit()
    channel = _channel_node(session, task.id, obj.id, code="Chan_1")

    hit = component_freq.resolve_component_freq(session, channel.id, "om1", part_code="2X_LF")
    assert hit is not None
    assert hit.value_hz == 100.0
    assert hit.scaling == "fixed"
    assert hit.rpm_used is None


def test_resolve_ignores_zero_freq_rows(session, task, obj):
    """附件里的 0 表示该特征频率不适用：按原值留痕，但解析不出 0 Hz 标记线。"""
    component_freq.import_csv(session, obj.id, ATTACHMENT)
    channel = _channel_node(session, task.id, obj.id, code="Chan_1")
    object_registry.set_rated_rpm(session, obj.id, 1500.0)
    assert (
        component_freq.resolve_component_freq(session, channel.id, "bpfi", part_code="1G-H_R")
        is None
    )
