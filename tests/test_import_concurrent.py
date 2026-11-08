"""复查 v2 严重项 2 回归：并发导入不许报 ``database is locked``。

缺陷背景（``docs/reviews/2026-11-03-总体复查-v2.md`` 问题 2）：

批次导入以 ``session.begin_nested()``（``SAVEPOINT``）起头，SQLite 因此开的是 **DEFERRED**
事务；开头先 SELECT（读快照）、后面 INSERT 要把读事务升级成写事务，另一个写者持锁时得到
``SQLITE_BUSY_SNAPSHOT`` —— 该错误**不调用 busy handler**，``busy_timeout`` 形同虚设，
第二个导入 0.05 s 内就失败。

验收口径（任务书批次 A 问题 2）：2 线程 × 5 轮并发导入（不同数据集 + 同一数据集），
**全部成功**，排队的那个等 ≤2 s；不允许出现 ``database is locked``。本文件用**真实
SQLite 文件 + threading**，不 mock 锁。

同型路径另有一条：``component_freq.import_csv``（频率台账 CSV 导入，预览亦然）。
"""

from __future__ import annotations

import csv
import io
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
from _dat_fixture import write_dat
from sqlalchemy import func, select

from lion_monitor.features.pipeline import import_batch
from lion_monitor.store import component_freq, object_registry, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store.models import ComponentFreq, Dataset

FS = 32000.0
CHANNELS = ("CH1", "CH2")

#: 第二个写者允许的排队上限（任务书：≤2 s）
MAX_QUEUE_S = 2.0


def _make_pipeline_env(db_path: Path):
    """任务 + 设备→监测对象→2 个通道节点（各挂 v_RMS / a_RMS）。"""
    engine = store_db.make_engine(db_path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with factory() as session:
        task = tasks.create_task(session, name="并发导入")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
        nodes: dict[str, int] = {}
        for code in CHANNELS:
            node = tree.create_node(session, task.id, "channel", code, parent_id=comp.id)
            nodes[code] = node.id
            for metric in ("v_RMS", "a_RMS"):
                tree.create_node(
                    session,
                    task.id,
                    "metric",
                    metric,
                    parent_id=node.id,
                    attrs={"code": metric},
                )
        session.commit()
    return engine, factory, task.id, nodes


def _write_batch(folder: Path, stamp: str) -> Path:
    """写一个 2 通道 × 3 s 的采集目录（32 kHz / 100 Hz 正弦，够 2 s 一窗）。"""
    folder.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(3.0 * FS)) / FS
    for code in CHANNELS:
        write_dat(
            folder / f"{code}.dat",
            samples=np.sin(2 * np.pi * 100.0 * t),
            channel=code,
            sample_rate_hz=FS,
            started_at=stamp,
        )
    return folder


def _run_concurrently(workers) -> list[dict]:
    """同时起若干线程（都从同一时刻开始），收集每个线程的结果与耗时。"""
    records: list[dict] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(workers))

    def wrap(name: str, job) -> None:
        barrier.wait()
        started = time.perf_counter()
        record: dict[str, object] = {"name": name}
        try:
            record["result"] = job()
            record["ok"] = True
        except Exception as exc:  # noqa: BLE001 - 失败就是要断言的内容
            record["ok"] = False
            record["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            record["elapsed"] = time.perf_counter() - started
            with lock:
                records.append(record)

    threads = [threading.Thread(target=wrap, args=(name, job)) for name, job in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return records


def _import_in_own_session(factory, task_id: int, nodes: dict[str, int], folder: Path):
    def job():
        session = factory()
        try:
            return import_batch(session, task_id, folder, nodes)
        finally:
            session.close()

    return job


def test_concurrent_import_batch_all_succeed(tmp_path) -> None:
    """2 线程 × 5 轮：3 轮各导不同数据集 + 2 轮同时导同一数据集，10 次全部成功。"""
    engine, factory, task_id, nodes = _make_pipeline_env(tmp_path / "concurrent.db")
    records: list[dict] = []
    try:
        serial = 0
        # 轮 1-3：两个线程各导一个新目录（不同数据集）
        for _round in range(3):
            folders = []
            for lane in ("A", "B"):
                serial += 1
                folders.append(
                    _write_batch(tmp_path / f"batch_{serial}{lane}", f"2026110412{serial:04d}")
                )
            records += _run_concurrently(
                [
                    (folder.name, _import_in_own_session(factory, task_id, nodes, folder))
                    for folder in folders
                ]
            )
        # 轮 4-5：两个线程同时导**同一个**目录（同一数据集，重复导入应幂等）
        same = _write_batch(tmp_path / "batch_same", "20261104130000")
        for _round in range(2):
            records += _run_concurrently(
                [
                    (f"same-{index}", _import_in_own_session(factory, task_id, nodes, same))
                    for index in (1, 2)
                ]
            )

        with factory() as session:
            dataset_count = session.scalar(select(func.count()).select_from(Dataset))
    finally:
        engine.dispose()

    failures = [record for record in records if not record["ok"]]
    assert records and not failures, f"并发导入失败：{failures}"
    assert not any("database is locked" in str(record.get("error", "")) for record in records)
    assert all(record["result"].status == "有效" for record in records)
    assert max(record["elapsed"] for record in records) <= MAX_QUEUE_S, (
        f"排队超过 {MAX_QUEUE_S} s：{[round(r['elapsed'], 3) for r in records]}"
    )
    # 3 轮 × 2 个新目录 = 6 个数据集；同一目录重复导入 4 次仍只有 1 个
    assert dataset_count == 7


# ---------------------------------------------------------------- 频率台账 CSV

FREQ_CSV_COLUMNS = list(component_freq.CSV_COLUMNS)


def _write_freq_csv(path: Path, part_code: str, channel: str = "CH1") -> Path:
    """写一份最小可用的频率台账 CSV（8 个频率项都填）。"""
    row = dict.fromkeys(FREQ_CSV_COLUMNS, "")
    row.update(
        {
            "序号": channel,
            "振动通道名称": "轴承A驱动端",
            "部件类型": "轴承",
            "监测部件编号": part_code,
            "监测部件名称": "轴承A",
            "OM1": "1000",
            "OM2": "2000",
            "OMx": "3000",
            "OM2x": "4000",
            "外圈故障特征频率BPFI": "5.4",
            "内圈故障特征频率BPF0": "7.2",
            "保持架故障特征频率FTF": "0.4",
            "滚动体故障特征频率BSF": "2.1",
            "D": "80",
        }
    )
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FREQ_CSV_COLUMNS)
    writer.writeheader()
    writer.writerow(row)
    path.write_text(buffer.getvalue(), encoding="utf-8")
    return path


def test_concurrent_component_freq_import_all_succeed(tmp_path) -> None:
    """两个线程同时导入频率台账 CSV（同一对象、同一通道）：都成功、通道不重复。"""
    engine = store_db.make_engine(tmp_path / "freq.db")
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with factory() as session:
        task = tasks.create_task(session, name="频率并发")
        obj = object_registry.create_object(session, task.id, name="主减速器", obj_type="轴承")
        object_id = obj.id
        session.commit()

    csv_a = _write_freq_csv(tmp_path / "part_a.csv", "P-001")
    csv_b = _write_freq_csv(tmp_path / "part_b.csv", "P-002")

    def job(path: Path):
        def run():
            session = factory()
            try:
                return component_freq.import_csv(session, object_id, path)
            finally:
                session.close()

        return run

    try:
        records = _run_concurrently([("A", job(csv_a)), ("B", job(csv_b))])
        with factory() as session:
            rows = session.scalar(select(func.count()).select_from(ComponentFreq))
            channels = (
                session.execute(
                    select(ComponentFreq.channel_code).where(
                        ComponentFreq.part_code.in_(("P-001", "P-002"))
                    )
                )
                .scalars()
                .all()
            )
    finally:
        engine.dispose()

    failures = [record for record in records if not record["ok"]]
    assert records and not failures, f"并发频率导入失败：{failures}"
    assert not any("database is locked" in str(record.get("error", "")) for record in records)
    assert max(record["elapsed"] for record in records) <= MAX_QUEUE_S
    # 两个部件 × 8 个频率项
    assert rows == 16
    assert set(channels) == {"CH1"}


def test_begin_immediate_skips_existing_transaction(tmp_path) -> None:
    """已在事务里时不重复 ``BEGIN``（否则报 cannot start a transaction within a transaction）。"""
    engine = store_db.make_engine(tmp_path / "skip.db")
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    try:
        session = factory()
        try:
            assert store_db.begin_immediate(session) is True
            assert store_db.begin_immediate(session) is False
            session.rollback()
        finally:
            session.close()
    finally:
        engine.dispose()


def test_concurrent_write_does_not_leak_locks(tmp_path) -> None:
    """并发结束后库仍可正常写（事务/锁都收干净了）。"""
    engine, factory, task_id, nodes = _make_pipeline_env(tmp_path / "clean.db")
    folder = _write_batch(tmp_path / "batch_after", "20261104150000")
    try:
        _run_concurrently(
            [
                (folder.name, _import_in_own_session(factory, task_id, nodes, folder)),
                (folder.name + "-2", _import_in_own_session(factory, task_id, nodes, folder)),
            ]
        )
        session = factory()
        try:
            # 锁没留下：能立即拿到写锁并写一行
            assert store_db.begin_immediate(session) is True
            session.rollback()
        finally:
            session.close()
        conn = sqlite3.connect(tmp_path / "clean.db", timeout=1)
        try:
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    finally:
        engine.dispose()
