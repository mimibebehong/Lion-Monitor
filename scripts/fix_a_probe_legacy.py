"""批次 A 问题 1 探针：老库（``sensor.node_id NOT NULL``、没有 ``channel_code``）升级。

用法：``.venv/bin/python scripts/fix_a_probe_legacy.py``
造库与判定逻辑与 ``tests/test_db_legacy_upgrade.py`` 同一口径，只是把每一步的真实输出
打印出来，供 ``reports/2026-11-04-batch-a-verification.txt`` 引用。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store.models import Base  # noqa: E402

TMP = Path.home() / "workspace" / "temp" / datetime.now().strftime("%Y-%m-%d") / "batch-a"
TMP.mkdir(parents=True, exist_ok=True)

LEGACY_SENSOR_DDL = """
CREATE TABLE sensor (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    node_id INTEGER NOT NULL,
    model VARCHAR(128) NOT NULL,
    sensitivity VARCHAR(64) NOT NULL,
    range_g VARCHAR(64) NOT NULL,
    mounting VARCHAR(64) NOT NULL,
    calibrated_on VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL,
    remark TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    deleted_at DATETIME
)
"""


def build_legacy(path: Path, rows: int = 1) -> Path:
    """当前库建好后再把 sensor 换成老结构 + 老数据（1 行）。"""
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    engine.dispose()
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE sensor")
        conn.executescript(LEGACY_SENSOR_DDL)
        for index in range(1, rows + 1):
            conn.execute(
                "INSERT INTO sensor (id, task_id, node_id, model, sensitivity, range_g,"
                " mounting, calibrated_on, status, remark, created_at)"
                " VALUES (?, 1, ?, ?, '100mV/g', '5g', '磁吸', '2026-01-01', '启用', '',"
                " '2026-10-01 08:00:00')",
                (index, 100 + index, f"PCB-{index}"),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        out = {}
        for table in ("sensor", "sensor_old"):
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                out[table] = -1
        return out
    finally:
        conn.close()


def schema(path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(path)
    try:
        return [
            (row[0], " ".join((row[1] or "").split()))
            for row in conn.execute(
                "SELECT type, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
            )
        ]
    finally:
        conn.close()


def sensor_view(path: Path) -> dict[str, object]:
    conn = sqlite3.connect(path)
    try:
        columns = {row[1]: int(row[3]) for row in conn.execute("PRAGMA table_info(sensor)")}
        channel_expr = "channel_code" if "channel_code" in columns else "''"
        rows = [
            {"id": row[0], "node_id": row[1], "model": row[2], "channel_code": row[3]}
            for row in conn.execute(
                f"SELECT id, node_id, model, {channel_expr} FROM sensor ORDER BY id"
            )
        ]
        return {
            "node_id_notnull": columns.get("node_id"),
            "channel_code_notnull": columns.get("channel_code"),
            "rows": rows,
        }
    finally:
        conn.close()


def main() -> int:
    out: dict[str, object] = {}

    # ---------- A. 老库升级 + 3 次幂等 ----------
    legacy = build_legacy(TMP / "legacy.db", rows=1)
    out["A_before"] = {"counts": counts(legacy), "sensor": sensor_view(legacy)}
    engine = store_db.make_engine(legacy)
    out["A_pending_migrations"] = store_db.pending_migrations(engine)
    attempts: list[dict[str, object]] = []
    schema_after = None
    for attempt in range(1, 4):
        try:
            store_db.init_db(engine)
            schema_after = schema(legacy)
            attempts.append(
                {
                    "attempt": attempt,
                    "ok": True,
                    "counts": counts(legacy),
                    "schema_items": len(schema_after),
                    "error": None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 失败就是要记录的现象
            attempts.append(
                {
                    "attempt": attempt,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "counts": counts(legacy),
                }
            )
            break
    out["A_attempts"] = attempts
    out["A_after"] = {"counts": counts(legacy), "sensor": sensor_view(legacy)}
    out["A_schema_stable_after_3x"] = schema_after is not None and schema(legacy) == schema_after
    engine.dispose()

    # ---------- B. 中途失败中间态自愈（sensor 空 + sensor_old 有数据） ----------
    half = build_legacy(TMP / "half.db", rows=2)
    conn = sqlite3.connect(half)
    try:
        conn.execute("ALTER TABLE sensor RENAME TO sensor_old")
        conn.commit()
    finally:
        conn.close()
    engine = store_db.make_engine(half)
    with engine.begin() as connection:
        Base.metadata.tables["sensor"].create(connection)
    out["B_half_state"] = counts(half)
    try:
        store_db.init_db(engine)
        store_db.init_db(engine)
        out["B_self_heal"] = "OK"
    except Exception as exc:  # noqa: BLE001
        out["B_self_heal"] = f"{type(exc).__name__}: {exc}"
    out["B_after"] = {"counts": counts(half), "sensor": sensor_view(half)}
    engine.dispose()

    # ---------- C. 更早的中断态：只改名、新表还没建 ----------
    renamed = build_legacy(TMP / "renamed.db", rows=2)
    conn = sqlite3.connect(renamed)
    try:
        conn.execute("ALTER TABLE sensor RENAME TO sensor_old")
        conn.commit()
    finally:
        conn.close()
    engine = store_db.make_engine(renamed)
    try:
        store_db.init_db(engine)
        out["C_after_rename_only"] = "OK"
    except Exception as exc:  # noqa: BLE001
        out["C_after_rename_only"] = f"{type(exc).__name__}: {exc}"
    out["C_counts"] = counts(renamed)
    engine.dispose()

    print(json.dumps(out, ensure_ascii=False, indent=2))
    ok = all(bool(item.get("ok")) for item in attempts)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
