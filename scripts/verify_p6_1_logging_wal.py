"""P6-1 验收实跑：日志落盘 + 轮转（G15）、SQLite WAL + busy timeout + BEGIN IMMEDIATE（T10）。

跑法（项目根目录）::

    .venv/bin/python scripts/verify_p6_1_logging_wal.py

只写临时库（~/workspace/temp/<日期>/p6-1/）与仓库 ``logs/``（.gitignore 已忽略），
不碰 ``data/lion.db``。任一条不通过即非零退出。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import struct
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
from sqlalchemy import event  # noqa: E402

from lion_monitor.features.pipeline import import_batch  # noqa: E402
from lion_monitor.logging_setup import LOG_FILE_NAME, setup_logging  # noqa: E402
from lion_monitor.store import tasks, tree  # noqa: E402
from lion_monitor.store.db import init_db, make_engine, make_session_factory  # noqa: E402

FS = 32000.0
STAMP = "20261029152834"
HEADER_SIZE = 512
WORK = Path.home() / "workspace" / "temp" / datetime.now().strftime("%Y-%m-%d") / "p6-1"
OUT = ROOT / "reports" / "2026-11-02-p6-1-verification.txt"

lines: list[str] = []
results: list[tuple[str, bool]] = []


def say(text: str = "") -> None:
    print(text)
    lines.append(text)


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok))
    mark = "PASS" if ok else "FAIL"
    say(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def build_dat(path: Path, channel: str, samples: np.ndarray) -> Path:
    """按 512 B 头写一个 envis .dat（与 tests/_dat_fixture 同格式）。"""
    buf = bytearray(HEADER_SIZE)
    struct.pack_into("<H", buf, 0x00, 20)
    struct.pack_into("<H", buf, 0x02, 64)
    struct.pack_into("<I", buf, 0x04, 131072)
    struct.pack_into("<f", buf, 0x08, 1.0)
    struct.pack_into("<I", buf, 0x40, 12)
    for slot, text in enumerate(("Rule1.d", "Farm", "10.10.10.165", channel)):
        raw = text.encode("ascii")
        buf[0x50 + slot * 16 : 0x50 + slot * 16 + len(raw)] = raw
    buf[0xD0 : 0xD0 + len(STAMP)] = STAMP.encode("ascii")
    struct.pack_into("<I", buf, 0x0F0, samples.size)
    struct.pack_into("<f", buf, 0x110, FS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(buf) + np.asarray(samples, dtype="<f4").tobytes())
    return path


def build_batch() -> Path:
    """CH1 好文件 + CH2 截断文件（头部完整、长度与声明不符 → 读失败）。"""
    folder = WORK / "四通道保存" / STAMP
    samples = np.sin(2 * np.pi * 100 * np.arange(int(FS)) / FS).astype("float32")
    build_dat(folder / f"{STAMP}-CH1_Task1.dat", "CH1", samples)
    broken = build_dat(folder / f"{STAMP}-CH2_Task1.dat", "CH2", samples)
    raw = broken.read_bytes()
    broken.write_bytes(raw[: len(raw) // 3])
    return folder


def import_with_broken_file() -> tuple[Path, int, str]:
    """验收 1：含坏文件的真实导入 → 日志文件里出现对应 warning 行。"""
    log_file = setup_logging(ROOT)
    before = log_file.stat().st_size if log_file.exists() else 0

    engine = make_engine(WORK / "verify.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        task = tasks.create_task(session, name="P6-1 日志验收")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
        channel_nodes = {}
        for code in ("CH1", "CH2"):
            channel = tree.create_node(session, task.id, "channel", code, parent_id=comp.id)
            channel_nodes[code] = channel.id
            tree.create_node(
                session, task.id, "metric", "v_RMS", parent_id=channel.id, attrs={"code": "v_RMS"}
            )
        session.commit()

        result = import_batch(session, task.id, build_batch(), channel_nodes)

    with log_file.open(encoding="utf-8") as handle:
        handle.seek(before)
        fresh = handle.read()
    engine.dispose()
    return log_file, result.files_bad_count, fresh


def check_rotation() -> None:
    """验收 2：轮转配置生效（临时把 maxBytes 调小，看备份文件真的生成）。"""
    rotate_dir = WORK / "rotate-logs"
    setup_logging(log_dir=rotate_dir, max_bytes=2048, backup_count=2)
    logger = logging.getLogger("lion_monitor.verify.rotate")
    for index in range(300):
        logger.warning("轮转验收行 %03d %s", index, "x" * 40)
    backups = sorted(path.name for path in rotate_dir.glob(f"{LOG_FILE_NAME}.*"))
    check(
        "验收2 轮转生效（maxBytes=2048 × 2 份备份）",
        backups == [f"{LOG_FILE_NAME}.1", f"{LOG_FILE_NAME}.2"],
        f"目录内 {sorted(p.name for p in rotate_dir.iterdir())}",
    )
    # 还原成正式配置（10 MB × 5），免得后续日志继续往小文件里轮转
    restored = setup_logging(ROOT)
    handler = next(
        h for h in logging.getLogger().handlers if getattr(h, "_lion_logging_file", False)
    )
    check(
        "验收2b 正式配置恢复为 10 MB × 5",
        handler.maxBytes == 10 * 1024 * 1024 and handler.backupCount == 5,
        f"{restored} maxBytes={handler.maxBytes} backupCount={handler.backupCount}",
    )


def check_wal() -> None:
    """验收 3：journal_mode=wal、busy_timeout=30000（附 synchronous / BEGIN IMMEDIATE）。"""
    path = WORK / "wal.db"
    engine = make_engine(path)
    traces: list[str] = []
    # 注意：要在第一条连接建立前挂 trace，池里复用的连接不会重新走 connect 事件
    event.listen(engine, "connect", lambda conn, _rec: conn.set_trace_callback(traces.append))
    init_db(engine)
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        busy = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
        sync = conn.exec_driver_sql("PRAGMA synchronous").scalar()
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO probe (id) VALUES (1)")

    begins = [s.strip().upper() for s in traces if s.strip().upper().startswith("BEGIN")]
    say(f"  PRAGMA journal_mode={mode}　busy_timeout={busy}　synchronous={sync}（1=NORMAL）")
    say(f"  写事务语句：{begins}")
    check("验收3 PRAGMA journal_mode 返回 wal", mode == "wal", str(mode))
    check("验收3 PRAGMA busy_timeout 返回 30000", busy == 30000, str(busy))
    check(
        "验收3b 写事务 BEGIN IMMEDIATE（不是 DEFERRED）",
        begins == ["BEGIN IMMEDIATE"],
        str(begins),
    )

    # WAL 的核心收益：一个连接持写锁时，另一个连接照样能读（rollback journal 下会撞锁）
    holder = sqlite3.connect(path, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO probe (id) VALUES (2)")
    try:
        reader = sqlite3.connect(path, timeout=0)
        try:
            seen = reader.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
        finally:
            reader.close()
    finally:
        holder.commit()
        holder.close()

    engine.dispose()
    check("验收3c 写事务持锁期间另一连接仍可读（WAL 读写不互斥）", seen == 1, f"读到 {seen} 行")


def main() -> int:
    if WORK.exists():  # 上一轮的同日目录：清掉重跑，结论才可复现
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True, exist_ok=True)
    say(f"P6-1 验收实跑 {datetime.now():%Y-%m-%d %H:%M:%S}")
    say(f"工作目录 {WORK}")
    say()

    say("== 验收 1：含坏文件的导入 → 日志文件 warning 行 ==")
    log_file, bad_count, fresh = import_with_broken_file()
    say(f"  日志文件：{log_file}")
    say(f"  files_bad={bad_count}")
    say("  本次新增日志行：")
    for row in fresh.strip().splitlines():
        say(f"    {row}")
    warning_rows = [row for row in fresh.splitlines() if "WARNING" in row and "跳过损坏文件" in row]
    check(
        "验收1 日志文件出现坏文件 warning 行",
        bool(warning_rows) and f"{STAMP}-CH2_Task1.dat" in warning_rows[0],
        warning_rows[0] if warning_rows else "（未找到）",
    )
    check(
        "验收1b 导入开始/结束都有 INFO 日志（批次·文件数·坏文件数·缺通道）",
        "导入开始" in fresh
        and "导入结束" in fresh
        and "文件成功 1 / 坏 1" in fresh
        and "缺通道" in fresh,
    )
    say()

    say("== 验收 2：轮转配置 ==")
    check_rotation()
    say()

    say("== 验收 3：WAL + busy timeout ==")
    check_wal()
    say()

    passed = sum(1 for _, ok in results if ok)
    say(f"结论：{passed}/{len(results)} 项通过")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"证据已写入 {OUT}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
