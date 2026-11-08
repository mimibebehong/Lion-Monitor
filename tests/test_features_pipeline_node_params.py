"""导入计算接节点生效参数（方案 A：值按节点存、按参数指纹去重）。

口径（2026-10-31 拍板）：

- 每个通道号找到挂载它的**全部**通道节点；每个节点按「指标定义默认 + 节点覆盖 diff +
  params_locked 固化」解析生效参数，用 ``resolve_rpm`` 拿转速
- 按 ``(channel_id, node_id, metric_code)`` 落库，同时写 ``params_hash``/``rpm_used``/``rpm_source``
- 同一通道 + 指标 + 参数指纹 + 转速 只真算一次，结果写多份节点行
- 需要转速却没转速的指标跳过并记录原因（不静默吞掉）
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features.pipeline import import_batch, metric_jobs
from lion_monitor.params import default_params, params_hash
from lion_monitor.store import metric_assign, metric_def, object_registry, tasks, tree
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.db import init_db, make_engine, make_session_factory

FS = 32000.0
STAMP = "20261031100000"


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="节点参数")


def _montage(session, task, objects: list[dict]) -> dict[str, list[int]]:
    """按 [{监测对象, 通道号, 指标码, 覆盖参数, 转速}] 装配健康树，返回 通道号 → 节点 id 列表。"""
    root = tree.root_node(session, task.id)
    device_id = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id).id
    ids: dict[str, list[int]] = {}
    for item in objects:
        comp = tree.create_node(session, task.id, "component", item["对象"], parent_id=device_id)
        attrs = {"转速": f"{item['转速']:g}"} if item.get("转速") else {}
        channel = tree.create_node(
            session,
            task.id,
            "channel",
            item["通道号"],
            parent_id=comp.id,
            attrs=attrs,
        )
        ids.setdefault(item["通道号"], []).append(channel.id)
        for code in item["指标"]:
            tree.create_node(
                session, task.id, "metric", code, parent_id=channel.id, attrs={"code": code}
            )
            metric_assign.assign_metric(
                session,
                task.id,
                channel.id,
                code,
                params=item.get("参数", {}).get(code),
                params_locked=bool(item.get("固化")),
            )
    return ids


def _batch_dir(tmp_path, channels=("CH1",)) -> Path:
    """一个批次：100 Hz + 800 Hz 双音（带通上限不同 → v_RMS 不同）。"""
    folder = tmp_path / "批次" / STAMP
    t = np.arange(int(FS * 2)) / FS
    samples = (np.sin(2 * np.pi * 100 * t) + 0.8 * np.sin(2 * np.pi * 800 * t)).astype("float32")
    for code in channels:
        write_dat(
            folder / f"{STAMP}-{code}_Task1.dat",
            samples=samples,
            channel=code,
            started_at=STAMP,
            sample_rate_hz=FS,
        )
    return folder


def _rows(session, task, code="CH1"):
    return metric_store.list_values(session, task.id, code)


SEED_SPEED_SCHEMA = [
    {
        "key": "f0_hz",
        "label": "特征频率",
        "group": "basic",
        "type": "float",
        "unit": "Hz",
        "default": 0.0,
        "scaling": "speed",
    }
]


def test_import_writes_one_row_per_node_with_own_band(session, task, tmp_path):
    """同一通道挂两个节点、带通不同 → 两套不同的 v_RMS 值，各自带自己的参数指纹。"""
    nodes = _montage(
        session,
        task,
        [
            {
                "对象": "轴承A",
                "通道号": "CH1",
                "指标": ["v_RMS", "a_RMS"],
                "参数": {"v_RMS": {"band_high": 2000.0}},
            },
            {
                "对象": "电机B",
                "通道号": "CH1",
                "指标": ["v_RMS"],
                "参数": {"v_RMS": {"band_high": 500.0}},
            },
        ],
    )

    result = import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": nodes["CH1"][0]})

    rows = [r for r in _rows(session, task) if r.metric_code == "v_RMS"]
    assert len(rows) == 2
    assert {r.node_id for r in rows} == set(nodes["CH1"])
    values = sorted(r.value for r in rows)
    assert values[0] < values[1]  # 10–500 Hz 少于 10–2000 Hz 的能量
    assert len({r.params_hash for r in rows}) == 2
    assert result.compute_runs == 3  # v_RMS×2 + a_RMS×1
    assert result.dedupe_hits == 0
    assert result.metrics_written == 3


def test_import_dedupes_identical_params_across_nodes(session, task, tmp_path):
    """同一通道两节点参数完全相同 → 只真算一次，结果写两行。"""
    nodes = _montage(
        session,
        task,
        [
            {"对象": "轴承A", "通道号": "CH1", "指标": ["v_RMS"]},
            {"对象": "轴承B", "通道号": "CH1", "指标": ["v_RMS"]},
        ],
    )

    result = import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": nodes["CH1"][0]})

    rows = [r for r in _rows(session, task) if r.metric_code == "v_RMS"]
    assert len(rows) == 2
    assert {r.node_id for r in rows} == set(nodes["CH1"])
    assert rows[0].value == pytest.approx(rows[1].value)
    assert {r.params_hash for r in rows} == {
        params_hash(default_params("v_RMS"), code="v_RMS", sample_rate=FS)
    }
    assert result.compute_runs == 1
    assert result.dedupe_hits == 1
    assert result.metrics_written == 2


def test_import_does_not_dedupe_when_rpm_differs(session, task, tmp_path):
    """参数相同但转速不同 → 不复用（口径要求指纹与转速都相同）还是各算一次、各带自己的转速。"""
    nodes = _montage(
        session,
        task,
        [
            {"对象": "轴承A", "通道号": "CH1", "指标": ["v_RMS"], "转速": 1480.0},
            {"对象": "轴承B", "通道号": "CH1", "指标": ["v_RMS"], "转速": 1500.0},
        ],
    )

    result = import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": nodes["CH1"][0]})

    rows = [r for r in _rows(session, task) if r.metric_code == "v_RMS"]
    assert {r.rpm_used for r in rows} == {1480.0, 1500.0}
    assert {r.rpm_source for r in rows} == {"node"}
    assert result.compute_runs == 2
    assert result.dedupe_hits == 0


def test_import_records_rpm_used_from_channel_node(session, task, tmp_path):
    """节点手填转速落到 rpm_used/rpm_source（节点手填 = node）。"""
    _montage(session, task, [{"对象": "轴承A", "通道号": "CH1", "指标": ["v_RMS"], "转速": 1480.0}])

    import_batch(session, task.id, _batch_dir(tmp_path), {})

    row = next(r for r in _rows(session, task) if r.metric_code == "v_RMS")
    assert row.rpm_used == pytest.approx(1480.0)
    assert row.rpm_source == "node"


def test_import_takes_rpm_from_object_node_upwards(session, task, tmp_path):
    """转速填在监测对象节点上，通道节点继承（沿树往上找）。"""
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(
        session, task.id, "component", "轴承A", parent_id=device.id, attrs={"转速": "1480"}
    )
    channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    tree.create_node(
        session, task.id, "metric", "v_RMS", parent_id=channel.id, attrs={"code": "v_RMS"}
    )

    import_batch(session, task.id, _batch_dir(tmp_path), {})

    row = next(r for r in _rows(session, task) if r.metric_code == "v_RMS")
    assert row.rpm_used == pytest.approx(1480.0)
    assert row.rpm_source == "node"


def test_import_skips_speed_metric_without_rpm(session, task, tmp_path):
    """需要转速但没转速的节点：跳过该指标并记录原因，其他指标照算、不报错。"""
    metric_def.create_metric(session, task.id, "1X", unit="Hz", schema=SEED_SPEED_SCHEMA)
    nodes = _montage(
        session,
        task,
        [
            {"对象": "轴承A", "通道号": "CH1", "指标": ["v_RMS"]},
            {"对象": "轴承B", "通道号": "CH1", "指标": ["1X"]},
        ],
    )

    result = import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": nodes["CH1"][0]})

    codes = {r.metric_code for r in _rows(session, task)}
    assert codes == {"v_RMS"}
    skipped = [s for s in result.skipped_metrics if s.code == "1X"]
    assert len(skipped) == 1
    assert skipped[0].node_id == nodes["CH1"][1]
    assert "缺少转速" in skipped[0].reason
    assert result.metrics_written == 1


def test_import_skips_unimplemented_metric_with_reason(session, task, tmp_path):
    """台账里自建的指标本期没有计算实现：跳过并记录原因（不再抛「不认识的指标码」）。"""
    metric_def.create_metric(session, task.id, "2X", unit="Hz")
    _montage(
        session,
        task,
        [{"对象": "轴承A", "通道号": "CH1", "指标": ["2X", "a_RMS"], "转速": 1480.0}],
    )

    result = import_batch(session, task.id, _batch_dir(tmp_path), {})

    assert {r.metric_code for r in _rows(session, task)} == {"a_RMS"}
    skipped = [s for s in result.skipped_metrics if s.code == "2X"]
    assert len(skipped) == 1
    assert "没有计算实现" in skipped[0].reason


def test_metric_jobs_follows_definition_defaults_and_node_override(session, task):
    """生效参数 = 硬默认 < 定义默认 < 节点覆盖；固化后不跟定义走。"""
    metric_def.seed_defaults(session, task.id)
    ledger = metric_def.find_metric(session, task.id, "v_RMS")
    metric_def.update_metric(session, ledger.id, defaults={"band_high": 800.0})

    _montage(
        session,
        task,
        [
            {"对象": "跟随定义", "通道号": "CH1", "指标": ["v_RMS"]},
            {
                "对象": "节点覆盖",
                "通道号": "CH1",
                "指标": ["v_RMS"],
                "参数": {"v_RMS": {"band_high": 1500.0}},
            },
            {
                "对象": "固化",
                "通道号": "CH2",
                "指标": ["v_RMS"],
                "参数": {"v_RMS": {**default_params("v_RMS"), "band_high": 600.0}},
                "固化": True,
            },
        ],
    )

    jobs, _skipped = metric_jobs(session, task.id)
    by_node_code = {(job.node_id, job.code): job for job in jobs}
    ch1 = [job for job in jobs if job.channel_code == "CH1" and job.code == "v_RMS"]
    assert [job.params["band_high"] for job in ch1] == [800.0, 1500.0]
    locked = next(job for job in jobs if job.channel_code == "CH2")
    assert locked.params["band_high"] == 600.0  # 固化值不跟随定义改的 800
    assert all(
        job.params_hash_for(FS) == params_hash(job.params, code=job.code, sample_rate=FS)
        for job in by_node_code.values()
    )


def test_metric_jobs_lists_every_channel_node_with_same_code(session, task):
    """同一通道号挂多个节点时，每个节点各出一条计算任务（方案 A：各算各的）。"""
    nodes = _montage(
        session,
        task,
        [
            {"对象": "轴承A", "通道号": "CH1", "指标": ["v_RMS"]},
            {"对象": "电机B", "通道号": "CH1", "指标": ["v_RMS"]},
        ],
    )
    jobs, _ = metric_jobs(session, task.id)
    assert {job.node_id for job in jobs} == set(nodes["CH1"])


def test_import_without_compute_writes_nothing_and_reports_nothing(session, task, tmp_path):
    metric_def.create_metric(session, task.id, "1X", unit="Hz", schema=SEED_SPEED_SCHEMA)
    _montage(session, task, [{"对象": "轴承A", "通道号": "CH1", "指标": ["1X"]}])

    result = import_batch(session, task.id, _batch_dir(tmp_path), {}, compute=False)

    assert result.metrics_written == 0
    assert result.compute_runs == 0
    assert result.skipped_metrics in (None, [])
    assert _rows(session, task) == []


def test_metric_jobs_takes_object_rated_rpm_as_third_level(session, task):
    """转速三级接上对象额定（CF-1）：rated 让依赖转速的指标可算，清空后回到「缺少转速」。"""
    obj = object_registry.create_object(session, task.id, name="轴承A", obj_type="轴承")
    object_registry.set_rated_rpm(session, obj.id, 1500.0)
    metric_def.create_metric(session, task.id, "v_RMS", schema=SEED_SPEED_SCHEMA)
    _montage(session, task, [{"对象": "轴承A", "通道号": "CH1", "指标": ["v_RMS"]}])

    jobs, skipped = metric_jobs(session, task.id)
    assert skipped == []
    assert len(jobs) == 1
    assert jobs[0].rpm == pytest.approx(1500.0)
    assert jobs[0].rpm_source == "rated"

    object_registry.set_rated_rpm(session, obj.id, None)
    jobs, skipped = metric_jobs(session, task.id)
    assert jobs == []
    assert len(skipped) == 1
    assert "缺少转速" in skipped[0].reason
