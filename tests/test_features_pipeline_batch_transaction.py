"""P1 回归：批次事务边界（B2）+ 批次三态（S5）。

缺陷背景（`docs/reviews/2026-10-31-总体复查.md` §4.1 / §4.7；Q3 拍板）：

- B2：store 层每个写函数自己 ``commit``，批次级回滚无从谈起——计算中途抛异常时，
  「数据集 + 部分通道 + 半套指标值」已经落库，留下半批垃圾。
- S5：``ok_count == len(files)`` 才算有效，否则一律「无效」——「部分有效」被折叠成
  「无效」，而那批里成功通道的值照样参与趋势 / 评级，用户看到「无效」会以为整批不能用。

口径（本卡定，马哥 Q3 拍板）：

1. **一次导入 = 一个事务**：建数据集 + 建通道 + 写指标值 + 落状态包在一个事务里，
   中途抛异常整体回滚，**库内 0 行新增**（不留 pending 数据集、不留半批通道/指标值）。
2. **批次三态**：``有效`` / ``部分有效`` / ``无效``。1/4 文件损坏或某通道计算失败 →
   ``部分有效`` 并在备注/``files_bad`` 里点名哪个通道、什么原因；全部失败 → ``无效``。
3. **只有判定有效的通道写指标值**：判无效的通道（信号近似为零等）不写值，
   否则 0 值会进趋势并参与评级（S5 的误导点）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features.pipeline import import_batch
from lion_monitor.store import datasets, metric_assign, tasks, tree
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.db import init_db, make_engine, make_session_factory
from lion_monitor.store.models import Channel, Dataset, MetricValue

FS = 32000.0
STAMP = "20261029152834"
CHANNELS = ("CH1", "CH2", "CH3", "CH4")


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="任务P1")


@pytest.fixture()
def channel_nodes(session, task):
    """健康树：设备 → 监测对象 → CH1~CH4 通道节点，每个通道下挂三个指标节点。"""
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    nodes = {}
    for code in CHANNELS:
        node = tree.create_node(session, task.id, "channel", code, parent_id=comp.id)
        nodes[code] = node.id
        for metric_code in ("v_RMS", "a_Peak", "a_RMS"):
            tree.create_node(
                session,
                task.id,
                "metric",
                metric_code,
                parent_id=node.id,
                attrs={"code": metric_code},
            )
    return nodes


def _batch_dir(tmp_path, stamp: str = STAMP, channels=CHANNELS) -> Path:
    """造一个多通道批次（每通道 2 s / 32 kHz 正弦，幅值 1 g，恰好一窗）。"""
    folder = tmp_path / "四通道保存" / stamp
    t = np.arange(int(FS * 2)) / FS
    samples = np.sin(2 * np.pi * 100 * t).astype("float32")
    for ch in channels:
        write_dat(
            folder / f"{stamp}-{ch}_Task1.dat",
            samples=samples,
            channel=ch,
            started_at=stamp,
            sample_rate_hz=FS,
        )
    return folder


def _truncate(path, ratio: float = 3) -> None:
    """把数据体截掉尾部（头部 512 B 保持完整 → 长度与头部声明不符）。"""
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // ratio])


def _alive_counts(session) -> tuple[int, int, int]:
    """库内存活的 dataset / channel / metric_value 行数（软删不算）。"""
    return (
        session.query(Dataset).filter(Dataset.deleted_at.is_(None)).count(),
        session.query(Channel).filter(Channel.deleted_at.is_(None)).count(),
        session.query(MetricValue).filter(MetricValue.deleted_at.is_(None)).count(),
    )


def _bad_band_assignment(session, task_id: int, channel_node_id: int) -> int:
    """给该通道的 v_RMS 授一个非数字的 ``band_low``——计算必然抛 ValueError。

    Q9 之后 ``band_low=0`` 已被下限兜住（不再让 ``butter`` 报错），所以改用
    「现场手改 JSON 写进脏值」这条路复现复查 §4.1 的计算异常。
    """
    row = metric_assign.assign_metric(
        session,
        task_id,
        channel_node_id,
        "v_RMS",
        params={"band_low": "abc"},
    )
    return row.id


def test_compute_exception_rolls_back_entire_batch(session, task, channel_nodes, tmp_path):
    """计算中途抛异常 → 整批回滚，库内 0 行新增（不留 pending 数据集/半批通道/半套值）。"""
    good = import_batch(session, task.id, _batch_dir(tmp_path, "20261029150000"), channel_nodes)
    assert good.status == "有效"

    _bad_band_assignment(session, task.id, channel_nodes["CH4"])
    before = _alive_counts(session)
    before_datasets = len(datasets.list_datasets(session, task.id))

    folder = _batch_dir(tmp_path, STAMP)
    with pytest.raises(ValueError):
        import_batch(session, task.id, folder, channel_nodes)

    assert _alive_counts(session) == before, "批次中途失败必须整体回滚，不许留半批数据"
    assert len(datasets.list_datasets(session, task.id)) == before_datasets
    # 失败批次的两个通道文件、指标值都不能残留
    assert (
        metric_store.count_values_for_dataset(
            session, datasets.list_datasets(session, task.id)[0].id
        )
        == before[2]
    )


def test_session_usable_after_rolled_back_batch(session, task, channel_nodes, tmp_path):
    """回滚后再导入同一目录（参数改回合法）：批次照常落库，说明事务状态没被打坏。"""
    assign_id = _bad_band_assignment(session, task.id, channel_nodes["CH4"])
    folder = _batch_dir(tmp_path)

    with pytest.raises(ValueError):
        import_batch(session, task.id, folder, channel_nodes)
    assert _alive_counts(session) == (0, 0, 0)

    metric_assign.unassign(session, assign_id)
    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.status == "有效"
    assert result.metrics_written == 12
    assert _alive_counts(session)[0] == 1


def test_one_damaged_file_marks_partial(session, task, channel_nodes, tmp_path):
    """4 个文件里 1 个损坏 → 状态「部分有效」，只有 3 个通道有值，备注点名文件与原因。"""
    folder = _batch_dir(tmp_path)
    bad_name = f"{STAMP}-CH2_Task1.dat"
    _truncate(folder / bad_name)

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.status == "部分有效"
    assert result.files_ok == 3
    assert result.channels == 4
    assert result.metrics_written == 9
    assert result.discarded is False

    row = datasets.get_dataset(session, result.dataset_id)
    assert row.status == "部分有效"
    assert "损坏 1 个文件" in row.remark
    assert bad_name in row.remark
    assert "长度不一致" in row.remark

    # 指标值只来自成功的 3 个通道，坏通道 0 值（不再参与趋势 / 评级）
    assert metric_store.list_values(session, task.id, "CH2") == []
    for code in ("CH1", "CH3", "CH4"):
        assert len(metric_store.list_values(session, task.id, code)) == 3

    files = {f.channel_code: f for f in datasets.list_files(session, result.dataset_id)}
    assert files["CH2"].status == "无效"
    for code in ("CH1", "CH3", "CH4"):
        assert files[code].status == "有效"


def test_dead_channel_contributes_no_values(session, task, channel_nodes, tmp_path):
    """信号近似为零的通道判无效 → 不写指标值；批次是「部分有效」而不是「无效」。"""
    folder = _batch_dir(tmp_path)
    write_dat(
        folder / f"{STAMP}-CH2_Task1.dat",
        samples=np.zeros(int(FS * 2), dtype="float32"),
        channel="CH2",
        started_at=STAMP,
        sample_rate_hz=FS,
    )

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.status == "部分有效"
    assert result.files_ok == 3
    assert result.metrics_written == 9
    entry = next(item for item in result.files_bad if item.channel_code == "CH2")
    assert entry.damaged is False
    assert "近似为零" in entry.reason
    assert metric_store.list_values(session, task.id, "CH2") == []
    assert datasets.get_dataset(session, result.dataset_id).status == "部分有效"


def test_all_files_failing_is_invalid(session, task, channel_nodes, tmp_path):
    """整批文件都读不出来 → 状态「无效」，不保留数据集（软删留痕，行为不变）。"""
    folder = _batch_dir(tmp_path)
    for ch in CHANNELS:
        _truncate(folder / f"{STAMP}-{ch}_Task1.dat")

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.status == "无效"
    assert result.files_ok == 0
    assert result.discarded is True
    assert result.dataset_id is None
    assert _alive_counts(session) == (0, 0, 0)


def test_all_good_batch_is_valid(session, task, channel_nodes, tmp_path):
    """全部成功 → 状态「有效」，备注留空（与改造前一致）。"""
    result = import_batch(session, task.id, _batch_dir(tmp_path), channel_nodes)

    assert result.status == "有效"
    assert result.files_ok == 4
    assert result.metrics_written == 12
    row = datasets.get_dataset(session, result.dataset_id)
    assert row.status == "有效"
    assert row.remark == ""
