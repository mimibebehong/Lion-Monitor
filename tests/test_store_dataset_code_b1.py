"""B1 回归：数据集编号撞全局唯一键（``dataset.code``）。

缺陷背景（docs/reviews/2026-10-31-总体复查.md · B1；probes P6 / F4）：

- ``store/datasets.py::_next_code`` 用「本任务已有条数 + 1」生成编号，而 ``dataset.code``
  是**全局** UNIQUE → 同一天第二个任务导入必 ``IntegrityError``
- 4 个 tab 并发导入 4/4 只有 2 个成功（两个写者读到同一个 count）

口径（本批定，马哥 2026-10-31 拍板）：编号**全局单调**（当天全局 max + 1），
撞号时重试；历史 ``dataset.code`` 原样保留，不重建表、不改已有值。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.store import datasets, tasks
from lion_monitor.store.db import init_db, make_engine, make_session_factory

FS = 32000.0
CHANNELS = (1, 2)
TODAY = f"DS-{datetime.now():%Y%m%d}-"


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(tmp_path / "b1.db")
    init_db(eng)
    return eng


@pytest.fixture()
def factory(engine):
    return make_session_factory(engine)


@pytest.fixture()
def session(factory):
    with factory() as s:
        yield s


def _make_dir(root: Path, stamp: str = "20261029152834", channels=CHANNELS) -> Path:
    """造一个只含 ``channels`` 两路 .dat 的批次目录。"""
    folder = root / "四通道保存" / stamp
    samples = np.sin(2 * np.pi * 100 * np.arange(1000) / FS).astype("float32")
    for ch in channels:
        write_dat(
            folder / f"{stamp}-CH{ch}_Task1.dat",
            samples=samples,
            channel=f"CH{ch}",
            started_at=stamp,
            sample_rate_hz=FS,
        )
    return folder


def test_two_tasks_same_day_import_same_batch_both_succeed(session, tmp_path):
    """同一天两个任务各导同一批次：都要成功，编号不能撞。"""
    batch = _make_dir(tmp_path)
    first_task = tasks.create_task(session, name="任务A")
    second_task = tasks.create_task(session, name="任务B")

    row_a = datasets.import_from_dir(session, first_task.id, batch)
    row_b = datasets.import_from_dir(session, second_task.id, batch)

    assert row_a.id != row_b.id
    assert row_a.code != row_b.code
    assert row_a.code.startswith(TODAY) and row_b.code.startswith(TODAY)
    assert len(datasets.list_datasets(session, first_task.id)) == 1
    assert len(datasets.list_datasets(session, second_task.id)) == 1
    # 编号全局唯一（不是「每任务各从 001 开始」）
    codes = {row_a.code, row_b.code}
    assert len(codes) == 2


def test_second_batch_in_second_task_does_not_collide(session, tmp_path):
    """任务 A 导两批后再让任务 B 导：任务 B 的编号要接在全局后面。"""
    batch1 = _make_dir(tmp_path, "20261029152834")
    batch2 = _make_dir(tmp_path, "20261029160313")
    task_a = tasks.create_task(session, name="任务A")
    task_b = tasks.create_task(session, name="任务B")

    a1 = datasets.import_from_dir(session, task_a.id, batch1)
    a2 = datasets.import_from_dir(session, task_a.id, batch2)
    b1 = datasets.import_from_dir(session, task_b.id, batch1)

    assert {a1.code, a2.code, b1.code} == {f"{TODAY}{n:03d}" for n in (1, 2, 3)}


def test_existing_codes_are_preserved(session, tmp_path):
    """历史编号不许被改写：老行原样保留，新编号接在最大号之后。"""
    task = tasks.create_task(session, name="老任务")
    legacy = datasets.create_dataset(
        session, task.id, "历史批次", "/mnt/d/x", "2026-10-29 15:28:34"
    )
    assert legacy.code == f"{TODAY}001"

    fresh = datasets.import_from_dir(session, task.id, _make_dir(tmp_path))

    assert datasets.get_dataset(session, legacy.id).code == f"{TODAY}001"
    assert fresh.code == f"{TODAY}002"


def test_four_concurrent_imports_all_succeed(factory, session, tmp_path):
    """4 个并发导入（模拟 4 个 tab）：4/4 成功，4 个不重号的数据集。"""
    task = tasks.create_task(session, name="并发任务")
    dirs = [_make_dir(tmp_path, f"2026102915283{i}") for i in range(4)]

    def _import(folder: Path) -> str:
        with factory() as own:
            return datasets.import_from_dir(own, task.id, folder).code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(_import, dirs))

    assert len(set(codes)) == 4, codes
    assert all(code.startswith(TODAY) for code in codes)
    assert len(datasets.list_datasets(session, task.id)) == 4
