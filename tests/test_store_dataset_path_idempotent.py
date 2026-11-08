"""G1 回归：重复导入按原始路径字符串比对，相对 / 绝对路径各建一个数据集。

缺陷背景（docs/reviews/2026-10-31-总体复查.md · G1；probes P7）：

- ``store/datasets.py::import_from_dir`` 用 ``Dataset.source_dir == str(folder)`` 原样比对
- 页面「已处理」标记用的是 ``norm_dir``，两套口径 → 换个写法又能导一次

口径（本批定）：统一 ``norm_dir`` 归一路径后比对，同一目录只保留一个数据集。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from _dat_fixture import write_dat

from lion_monitor.store import datasets, tasks
from lion_monitor.store.db import init_db, make_engine, make_session_factory

FS = 32000.0


def _make_dir(root: Path, stamp: str = "20261029152834") -> Path:
    folder = root / "四通道保存" / stamp
    samples = np.sin(2 * np.pi * 100 * np.arange(1000) / FS).astype("float32")
    for ch in (1, 2):
        write_dat(
            folder / f"{stamp}-CH{ch}_Task1.dat",
            samples=samples,
            channel=f"CH{ch}",
            started_at=stamp,
            sample_rate_hz=FS,
        )
    return folder


def test_relative_then_absolute_path_is_one_dataset(tmp_path, monkeypatch):
    """先 chdir 进批次目录用 '.' 导入，再用绝对路径导入：只应有一个数据集。"""
    engine = make_engine(tmp_path / "g1.db")
    init_db(engine)
    folder = _make_dir(tmp_path)

    with make_session_factory(engine)() as session:
        task = tasks.create_task(session, name="任务G1")

        monkeypatch.chdir(folder)
        first = datasets.import_from_dir(session, task.id, ".")
        second = datasets.import_from_dir(session, task.id, str(folder))

        assert first.id == second.id
        assert len(datasets.list_datasets(session, task.id)) == 1
        assert len(datasets.list_files(session, first.id)) == 2


def test_trailing_slash_is_same_dataset(tmp_path):
    """结尾带斜杠 / 不带斜杠：同一目录，只能有一个数据集。"""
    engine = make_engine(tmp_path / "g1b.db")
    init_db(engine)
    folder = _make_dir(tmp_path)

    with make_session_factory(engine)() as session:
        task = tasks.create_task(session, name="任务G1b")
        first = datasets.import_from_dir(session, task.id, str(folder))
        second = datasets.import_from_dir(session, task.id, str(folder) + "/")

        assert first.id == second.id
        assert len(datasets.list_datasets(session, task.id)) == 1


def test_processed_dirs_and_import_agree_on_same_dir(tmp_path, monkeypatch):
    """页面「已处理」索引（norm_dir）与实际导入比对口径一致。"""
    engine = make_engine(tmp_path / "g1c.db")
    init_db(engine)
    folder = _make_dir(tmp_path)

    with make_session_factory(engine)() as session:
        task = tasks.create_task(session, name="任务G1c")
        row = datasets.import_from_dir(session, task.id, str(folder))

        index = datasets.processed_dirs(session, task.id)
        assert index[datasets.norm_dir(folder)] == row.code

        monkeypatch.chdir(folder)
        again = datasets.import_from_dir(session, task.id, ".")
        assert again.id == row.id
