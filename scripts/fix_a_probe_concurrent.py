"""批次 A 问题 2 探针：并发导入（2 线程 × 5 轮 + 频率台账 CSV 并发）。

用法：``.venv/bin/python scripts/fix_a_probe_concurrent.py``
打印每轮真实结果（成功/失败、排队耗时、状态），供
``reports/2026-11-04-batch-a-verification.txt`` 引用。真实 SQLite 文件 + threading，不 mock 锁。
"""

from __future__ import annotations

import csv
import io
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from _dat_fixture import write_dat  # noqa: E402

from lion_monitor.features.pipeline import import_batch  # noqa: E402
from lion_monitor.store import component_freq, object_registry, tasks, tree  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402

TMP = Path.home() / "workspace" / "temp" / datetime.now().strftime("%Y-%m-%d") / "batch-a"
TMP.mkdir(parents=True, exist_ok=True)

FS = 32000.0
CHANNELS = ("CH1", "CH2")


def make_pipeline_env(path: Path):
    if path.exists():
        path.unlink()
    engine = store_db.make_engine(path)
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
                    session, task.id, "metric", metric, parent_id=node.id, attrs={"code": metric}
                )
        session.commit()
    return engine, factory, task.id, nodes


def write_batch(folder: Path, stamp: str) -> Path:
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


def run_pair(jobs) -> list[dict]:
    """两个线程同一时刻起跑，各自完成一个「导入」job。"""
    records: list[dict] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(jobs))

    def wrap(name: str, job) -> None:
        barrier.wait()
        started = time.perf_counter()
        record: dict[str, object] = {"name": name}
        try:
            record["result"] = job()
            record["ok"] = True
        except Exception as exc:  # noqa: BLE001
            record["ok"] = False
            record["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        finally:
            record["elapsed_s"] = round(time.perf_counter() - started, 4)
            with lock:
                records.append(record)

    threads = [threading.Thread(target=wrap, args=(name, job)) for name, job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return sorted(records, key=lambda item: item["name"])


def import_job(factory, task_id: int, nodes: dict[str, int], folder: Path):
    def job():
        session = factory()
        try:
            return import_batch(session, task_id, folder, nodes)
        finally:
            session.close()

    return job


def probe_pipeline() -> dict[str, object]:
    engine, factory, task_id, nodes = make_pipeline_env(TMP / "concurrent.db")
    rounds: list[dict] = []
    try:
        serial = 0
        for round_no in range(1, 4):  # 不同数据集
            folders = []
            for lane in ("A", "B"):
                serial += 1
                folders.append(
                    write_batch(TMP / f"batch_{serial}{lane}", f"2026110412{serial:04d}")
                )
            started = time.perf_counter()
            records = run_pair(
                [(folder.name, import_job(factory, task_id, nodes, folder)) for folder in folders]
            )
            rounds.append(
                {
                    "round": round_no,
                    "kind": "不同数据集",
                    "wall_s": round(time.perf_counter() - started, 3),
                    "imports": [
                        {
                            "name": record["name"],
                            "ok": record["ok"],
                            "elapsed_s": record["elapsed_s"],
                            "status": record.get("result").status if record["ok"] else None,
                            "error": record.get("error"),
                        }
                        for record in records
                    ],
                }
            )
        same = write_batch(TMP / "batch_same", "20261104130000")
        for round_no in (4, 5):  # 同一数据集
            records = run_pair(
                [
                    (f"{same.name}-{index}", import_job(factory, task_id, nodes, same))
                    for index in (1, 2)
                ]
            )
            rounds.append(
                {
                    "round": round_no,
                    "kind": "同一数据集",
                    "imports": [
                        {
                            "name": record["name"],
                            "ok": record["ok"],
                            "elapsed_s": record["elapsed_s"],
                            "status": record.get("result").status if record["ok"] else None,
                            "error": record.get("error"),
                        }
                        for record in records
                    ],
                }
            )
        with factory() as session:
            from sqlalchemy import func, select

            from lion_monitor.store.models import Dataset

            dataset_count = session.scalar(select(func.count()).select_from(Dataset))
    finally:
        engine.dispose()

    imports = [item for group in rounds for item in group["imports"]]
    return {
        "rounds": rounds,
        "imports": len(imports),
        "ok": sum(1 for item in imports if item["ok"]),
        "failed": [item for item in imports if not item["ok"]],
        "max_elapsed_s": max(item["elapsed_s"] for item in imports),
        "locked_errors": sum(
            1 for item in imports if "database is locked" in str(item.get("error") or "")
        ),
        "dataset_count": dataset_count,
    }


def write_freq_csv(path: Path, part_code: str) -> Path:
    columns = list(component_freq.CSV_COLUMNS)
    row = dict.fromkeys(columns, "")
    row.update(
        {
            "序号": "CH1",
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
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerow(row)
    path.write_text(buffer.getvalue(), encoding="utf-8")
    return path


def probe_component_freq() -> dict[str, object]:
    path = TMP / "freq.db"
    if path.exists():
        path.unlink()
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with factory() as session:
        task = tasks.create_task(session, name="频率并发")
        obj = object_registry.create_object(session, task.id, name="主减速器", obj_type="轴承")
        object_id = obj.id
        session.commit()

    csv_a = write_freq_csv(TMP / "part_a.csv", "P-001")
    csv_b = write_freq_csv(TMP / "part_b.csv", "P-002")

    def job(csv_path: Path):
        def run():
            session = factory()
            try:
                return component_freq.import_csv(session, object_id, csv_path)
            finally:
                session.close()

        return run

    try:
        records = run_pair([("csv_a", job(csv_a)), ("csv_b", job(csv_b))])
        from sqlalchemy import func, select

        from lion_monitor.store.models import ComponentFreq

        with factory() as session:
            rows = session.scalar(select(func.count()).select_from(ComponentFreq))
    finally:
        engine.dispose()
    return {
        "imports": [
            {
                "name": record["name"],
                "ok": record["ok"],
                "elapsed_s": record["elapsed_s"],
                "error": record.get("error"),
            }
            for record in records
        ],
        "rows": rows,
        "locked_errors": sum(
            1 for record in records if "database is locked" in str(record.get("error") or "")
        ),
    }


def probe_queue() -> dict[str, object]:
    """验证「排队」而不是「失败」：另一连接先持有写锁 1 s，导入应等锁释放后成功。"""
    engine, factory, task_id, nodes = make_pipeline_env(TMP / "queue.db")
    folder = write_batch(TMP / "batch_queue", "20261104160000")
    holding = threading.Event()
    holder_result: dict[str, object] = {}

    def hold() -> None:
        session = factory()
        try:
            store_db.begin_immediate(session)
            holding.set()
            time.sleep(1.0)
        finally:
            session.rollback()
            session.close()

    holder = threading.Thread(target=hold)
    holder.start()
    holding.wait(2.0)
    started = time.perf_counter()
    session = factory()
    try:
        result = import_batch(session, task_id, folder, nodes)
        holder_result["status"] = result.status
        holder_result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        holder_result["ok"] = False
        holder_result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()
    waited = time.perf_counter() - started
    holder.join()
    engine.dispose()
    return {
        "ok": holder_result.get("ok"),
        "status": holder_result.get("status"),
        "waited_s": round(waited, 3),
        "error": holder_result.get("error"),
    }


def main() -> int:
    pipeline = probe_pipeline()
    freq = probe_component_freq()
    queue = probe_queue()
    print(
        json.dumps(
            {"pipeline": pipeline, "component_freq": freq, "queue": queue},
            ensure_ascii=False,
            indent=2,
        )
    )
    failed = (
        bool(pipeline["failed"])
        or not all(item["ok"] for item in freq["imports"])
        or not queue["ok"]
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
