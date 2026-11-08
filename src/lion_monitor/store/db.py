"""数据库连接与建表。"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .backup import BackupError, backup_database
from .models import Base

_log = logging.getLogger(__name__)

#: 默认库文件（相对工程根目录）
DEFAULT_DB_PATH = Path("data/lion.db")

#: 忙等超时（秒）：另一个进程/标签页在写时最多等这么久再报 database is locked
BUSY_TIMEOUT_S = 30


def make_engine(db_path: str | Path = DEFAULT_DB_PATH) -> Engine:
    """建 SQLite 引擎；父目录不存在则创建。

    T10（复查 §6.4）：默认 ``journal_mode=delete`` 读写互斥，两个 tab 一个查一个写就有
    停顿风险。这里统一改成 WAL + ``synchronous=NORMAL``，并把忙等超时设成 30 s。

    驱动层 ``isolation_level="IMMEDIATE"``：写事务由 pysqlite 发 ``BEGIN IMMEDIATE``
    （不是默认的 DEFERRED），一次拿到写锁，避免「读锁升级写锁」互等；只读语句不开事务，
    所以查询不受影响。
    """
    path = Path(db_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        future=True,
        connect_args={"timeout": BUSY_TIMEOUT_S, "isolation_level": "IMMEDIATE"},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            mode = cursor.execute("PRAGMA journal_mode=WAL").fetchone()
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_S * 1000}")
        finally:
            cursor.close()
        if not mode or str(mode[0]).lower() != "wal":
            # 网络盘 / 只读盘不支持 WAL：照旧用 rollback journal，但要说清楚
            _log.warning("库文件 %s 无法切到 WAL（当前 %s），并发写性能会下降", path, mode)

    return engine


def resolve_db_path(project_root: str | Path, override: str = "") -> Path:
    """定库文件位置：显式覆盖优先（支持 ~），否则用工程根下的默认路径。"""
    text = (override or "").strip()
    if text:
        return Path(text).expanduser()
    return Path(project_root) / DEFAULT_DB_PATH


#: ``sensor`` 的列序：迁移重建表时按它搬数据（老库缺的列补默认值，见 ``_sensor_copy_sql``）
_SENSOR_COLUMNS = (
    "id",
    "task_id",
    "node_id",
    "channel_code",
    "model",
    "sensitivity",
    "range_g",
    "mounting",
    "calibrated_on",
    "status",
    "remark",
    "created_at",
    "deleted_at",
)


def _sensor_copy_sql(conn) -> tuple[str, str]:
    """``sensor_old`` → ``sensor`` 的插入列清单 / 取值表达式（成对使用）。

    老库（以及上一次迁移失败留下的 ``sensor_old``）可能还没有 ``channel_code`` 列，
    这时补空串：新表的该列是 NOT NULL 且默认空串，不透传默认值会直接
    ``NOT NULL constraint failed: sensor.channel_code``（复查 v2 阻塞项 1）。
    """
    old_columns = _table_columns(conn, "sensor_old")
    exprs = []
    for column in _SENSOR_COLUMNS:
        if column != "channel_code":
            exprs.append(column)
        elif column in old_columns:
            exprs.append("COALESCE(channel_code, '')")
        else:
            exprs.append("''")
    return ", ".join(_SENSOR_COLUMNS), ", ".join(exprs)


def _relax_sensor_node_nullable(engine: Engine) -> None:
    """历史库的 sensor.node_id 是 NOT NULL，重建表允许为空（幂等）。"""
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "sensor_old" in tables:
            # 上一次迁移中途失败：先把数据搬回正式表，再丢掉临时表。
            # （pysqlite 的 DDL 是自动提交的，RENAME 留下的临时表会跨进程留存）
            if "sensor" not in tables:
                conn.exec_driver_sql("ALTER TABLE sensor_old RENAME TO sensor")
            else:
                cols, exprs = _sensor_copy_sql(conn)
                conn.exec_driver_sql(
                    f"INSERT OR IGNORE INTO sensor ({cols}) SELECT {exprs} FROM sensor_old"
                )
                conn.exec_driver_sql("DROP TABLE IF EXISTS sensor_old")

        rows = conn.exec_driver_sql("PRAGMA table_info(sensor)").fetchall()
        if not rows:
            return
        node_col = next((row for row in rows if row[1] == "node_id"), None)
        if node_col is None or not node_col[3]:  # 3 = notnull
            return
        _log.info("迁移：sensor.node_id 改为可空（重建表）")
        conn.exec_driver_sql("ALTER TABLE sensor RENAME TO sensor_old")
        for index_name in ("ix_sensor_task_id", "ix_sensor_node_id", "ix_sensor_deleted_at"):
            conn.exec_driver_sql(f"DROP INDEX IF EXISTS {index_name}")
        from .models import Sensor

        Sensor.__table__.create(conn)
        cols, exprs = _sensor_copy_sql(conn)
        conn.exec_driver_sql(f"INSERT INTO sensor ({cols}) SELECT {exprs} FROM sensor_old")
        conn.exec_driver_sql("DROP TABLE sensor_old")


def _relax_channel_reg_object_nullable(engine: Engine) -> None:
    """历史库 channel_reg.object_id 是 NOT NULL，重建表允许为空（自由通道），幂等。"""
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "channel_reg" not in tables:
            return
        cols = "id, task_id, object_id, code, remark, created_at, deleted_at"
        if "channel_reg_old" in tables:
            conn.exec_driver_sql(
                f"INSERT OR IGNORE INTO channel_reg ({cols}) SELECT {cols} FROM channel_reg_old"
            )
            conn.exec_driver_sql("DROP TABLE IF EXISTS channel_reg_old")
        rows = conn.exec_driver_sql("PRAGMA table_info(channel_reg)").fetchall()
        col = next((row for row in rows if row[1] == "object_id"), None)
        if col is None or not col[3]:  # 3 = notnull
            return
        _log.info("迁移：channel_reg.object_id 改为可空（重建表，支持自由通道）")
        conn.exec_driver_sql("ALTER TABLE channel_reg RENAME TO channel_reg_old")
        for index_name in (
            "ix_channel_reg_task_id",
            "ix_channel_reg_object_id",
            "ix_channel_reg_deleted_at",
        ):
            conn.exec_driver_sql(f"DROP INDEX IF EXISTS {index_name}")
        from .models import ChannelReg

        ChannelReg.__table__.create(conn)
        conn.exec_driver_sql(f"INSERT INTO channel_reg ({cols}) SELECT {cols} FROM channel_reg_old")
        conn.exec_driver_sql("DROP TABLE channel_reg_old")


def _free_unassembled_channels(engine: Engine) -> None:
    """把「树上没装配 + 传感器也没挂节点」的通道回填成自由态（object_id=NULL），幂等。

    这类行是旧模型强制「通道必须归属某对象」时写进去的，实际状态是自由（如 CH2）。
    """
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"channel_reg", "task_node", "sensor"} <= tables:
            return
        sensor_cols = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(sensor)").fetchall()
        }
        if "channel_code" not in sensor_cols:  # 老库还没补列，下一轮再回填
            return
        result = conn.exec_driver_sql(
            """
            UPDATE channel_reg SET object_id = NULL
            WHERE deleted_at IS NULL
              AND object_id IS NOT NULL
              AND id NOT IN (
                  SELECT ref_id FROM task_node
                  WHERE ref_type = 'channel_reg' AND ref_id IS NOT NULL AND deleted_at IS NULL
              )
              AND code IN (
                  SELECT channel_code FROM sensor
                  WHERE deleted_at IS NULL AND node_id IS NULL AND channel_code <> ''
              )
            """
        )
        if result.rowcount:
            _log.info("迁移：%d 条未装配且传感器自由的通道回填为自由态", result.rowcount)


def _add_sensor_channel_code(engine: Engine) -> None:
    """历史库补 channel_code 列（手输通道号），幂等。"""
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(sensor)").fetchall()
        if not rows:
            return
        if any(row[1] == "channel_code" for row in rows):
            return
        _log.info("迁移：sensor 补 channel_code 列")
        conn.exec_driver_sql("ALTER TABLE sensor ADD COLUMN channel_code VARCHAR(32) DEFAULT ''")


def _add_task_node_refs(engine: Engine) -> None:
    """task_node 补 ref_type/ref_id（指向台账），幂等。"""
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(task_node)").fetchall()
        if not rows:
            return
        names = {row[1] for row in rows}
        if "ref_type" not in names:
            conn.exec_driver_sql("ALTER TABLE task_node ADD COLUMN ref_type VARCHAR(32) DEFAULT ''")
        if "ref_id" not in names:
            conn.exec_driver_sql("ALTER TABLE task_node ADD COLUMN ref_id INTEGER")


def _add_metric_value_trace(engine: Engine) -> None:
    """metric_value 补参数快照列（params_hash/effective_params_json/rpm_*/stale），幂等。"""
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(metric_value)").fetchall()
        if not rows:
            return
        names = {row[1] for row in rows}
        added: list[str] = []
        for column, ddl in (
            ("params_hash", "VARCHAR(40) DEFAULT ''"),
            ("effective_params_json", "TEXT DEFAULT ''"),
            ("rpm_used", "FLOAT"),
            ("rpm_source", "VARCHAR(16) DEFAULT ''"),
            ("stale", "INTEGER DEFAULT 0"),
        ):
            if column in names:
                continue
            conn.exec_driver_sql(f"ALTER TABLE metric_value ADD COLUMN {column} {ddl}")
            added.append(column)
        if added:
            _log.info("迁移：metric_value 补列 %s", "、".join(added))


#: metric_value 的完整列序（重建表时按它搬数据，老表缺列就跳过）
_METRIC_VALUE_COLUMNS = (
    "id",
    "channel_id",
    "node_id",
    "code",
    "value",
    "unit",
    "level",
    "computed_at",
    "params_hash",
    "effective_params_json",
    "rpm_used",
    "rpm_source",
    "stale",
    "deleted_at",
)


def _table_columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def _tables(conn) -> set[str]:
    return {
        row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _metric_value_needs_rebuild(conn) -> bool:
    """``node_id`` 缺列，或唯一约束还是老的 (channel/code/时间) → 需要重建。"""
    if "node_id" not in _table_columns(conn, "metric_value"):
        return True
    sql = (
        conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='metric_value'"
        ).scalar()
        or ""
    )
    return "uq_metric_value_node_point" not in sql


def _rebuild_metric_value(conn) -> None:
    """整表重建 metric_value：补 node_id、把唯一约束换成含 node_id 的版本，数据原样搬回。

    方案 A 要求同一时间点能存多个节点的同一指标，SQLite 改不了已存在的唯一约束，
    只能重建（沿用 ``_relax_sensor_node_nullable`` 的做法）。
    """
    from .models import MetricValue

    _log.info("迁移：重建 metric_value（补 node_id + 唯一约束换版）")
    conn.exec_driver_sql("ALTER TABLE metric_value RENAME TO metric_value_old")
    # 先生成列表再删：游标还开着时改 schema 会报 "database table is locked"
    names = [
        row[0]
        for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='metric_value_old'"
        )
    ]
    for name in names:
        if not name.startswith("sqlite_autoindex"):
            conn.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")
    MetricValue.__table__.create(conn)
    carry = [
        col for col in _METRIC_VALUE_COLUMNS if col in _table_columns(conn, "metric_value_old")
    ]
    joined = ", ".join(carry)
    conn.exec_driver_sql(
        f"INSERT INTO metric_value ({joined}) SELECT {joined} FROM metric_value_old"
    )
    conn.exec_driver_sql("DROP TABLE metric_value_old")


def _fill_metric_values_from(conn, source: str) -> int:
    """把临时表里还没搬过去的老行补进正式表（只按 id 去重，不动已存在的行）。"""
    carry = [
        col
        for col in _METRIC_VALUE_COLUMNS
        if col in _table_columns(conn, source) and col in _table_columns(conn, "metric_value")
    ]
    joined = ", ".join(carry)
    result = conn.exec_driver_sql(
        f"INSERT INTO metric_value ({joined}) SELECT {joined} FROM {source}"
        " WHERE id NOT IN (SELECT id FROM metric_value)"
    )
    conn.exec_driver_sql(f"DROP TABLE {source}")
    return result.rowcount or 0


def _add_metric_value_node(engine: Engine) -> None:
    """metric_value 补 ``node_id``（值按节点存）+ 唯一约束换版，幂等。"""
    with engine.begin() as conn:
        tables = _tables(conn)
        if "metric_value_old" in tables:
            # 上一次重建中途失败：先保住已搬的行，再补齐旧表里缺的
            if "metric_value" not in tables:
                conn.exec_driver_sql("ALTER TABLE metric_value_old RENAME TO metric_value")
            else:
                _fill_metric_values_from(conn, "metric_value_old")
            tables = _tables(conn)
        if "metric_value" not in tables:
            return
        if not _metric_value_needs_rebuild(conn):
            return
        _rebuild_metric_value(conn)


#: 已被复合索引取代的单列索引（P3 / S3）：老库迁移时清掉，免得计划器挑中它们
_OBSOLETE_METRIC_VALUE_INDEXES = (
    "ix_metric_value_channel_id",
    "ix_metric_value_code",
    "ix_metric_value_node_id",
    "ix_metric_value_computed_at",
)


def _metric_value_index_defs() -> list[tuple[str, tuple[str, ...]]]:
    """``MetricValue`` 上声明的多列索引（名字 + 列序），单一来源就是 models。"""
    from .models import MetricValue

    return [
        (index.name, tuple(column.name for column in index.columns))
        for index in MetricValue.metadata.tables["metric_value"].indexes
        if index.name and len(index.columns) > 1
    ]


def _add_metric_value_indexes(engine: Engine) -> None:
    """把 ``metric_value`` 的索引同步成 models 声明的那套，幂等。

    表已存在时 ``create_all`` 只跳过整张表、不会补索引，所以这里自己建；
    老库的单列索引（``ix_metric_value_*_id`` 等）已被复合索引覆盖或没有使用场景，
    一并删掉——留着会让计划器挑它们（``code`` 只有几个值，等于每条都扫一大片）。
    先取成列表再执行 DDL：游标还占着 ``sqlite_master`` 时改 schema 会报
    ``database table is locked``（见 ``e9d79f4``）。
    """
    with engine.begin() as conn:
        if "metric_value" not in _tables(conn):
            return
        present = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='metric_value'"
            ).fetchall()
        }
        for name in _OBSOLETE_METRIC_VALUE_INDEXES:
            if name in present:
                conn.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")
        columns = _table_columns(conn, "metric_value")
        for name, wanted in _metric_value_index_defs():
            if name in present or not set(wanted) <= columns:
                continue
            conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS {name} ON metric_value ({', '.join(wanted)})"
            )


def _add_metric_def_params(engine: Engine) -> None:
    """metric_def 补参数声明与定义默认值两列，幂等。"""
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(metric_def)").fetchall()
        if not rows:
            return
        names = {row[1] for row in rows}
        added: list[str] = []
        if "params_schema_json" not in names:
            conn.exec_driver_sql(
                "ALTER TABLE metric_def ADD COLUMN params_schema_json TEXT DEFAULT '[]'"
            )
            added.append("params_schema_json")
        if "params_default_json" not in names:
            conn.exec_driver_sql(
                "ALTER TABLE metric_def ADD COLUMN params_default_json TEXT DEFAULT '{}'"
            )
            added.append("params_default_json")
        if added:
            _log.info("迁移：metric_def 补列 %s", "、".join(added))


def _add_metric_assign_locked(engine: Engine) -> None:
    """metric_assign 补 params_locked（固化开关），幂等。"""
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(metric_assign)").fetchall()
        if not rows:
            return
        if any(row[1] == "params_locked" for row in rows):
            return
        _log.info("迁移：metric_assign 补 params_locked 列")
        conn.exec_driver_sql("ALTER TABLE metric_assign ADD COLUMN params_locked INTEGER DEFAULT 0")


def _add_object_rated_rpm(engine: Engine) -> None:
    """monitor_object 补 ``rated_rpm``（对象额定转速，转速三级里的 rated），幂等。"""
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(monitor_object)").fetchall()
        if not rows or any(row[1] == "rated_rpm" for row in rows):
            return
        _log.info("迁移：monitor_object 补 rated_rpm 列")
        conn.exec_driver_sql("ALTER TABLE monitor_object ADD COLUMN rated_rpm FLOAT")


def _add_dataset_measured_rpm(engine: Engine) -> None:
    """dataset 补 ``measured_rpm``（批次实测转速，转速三级里的 measured），幂等。"""
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(dataset)").fetchall()
        if not rows or any(row[1] == "measured_rpm" for row in rows):
            return
        _log.info("迁移：dataset 补 measured_rpm 列")
        conn.exec_driver_sql("ALTER TABLE dataset ADD COLUMN measured_rpm FLOAT")


#: v_RMS 逐窗当前口径的窗型（写进 params 快照，用来识别「旧口径」历史值）
VRMS_WINDOW_FN = "rect"

#: 数据迁移版本（``PRAGMA user_version``）：1 = v_RMS 旧窗型已标过「待重算」
DATA_VERSION_VRMS_RECT = 1


def mark_legacy_vrms_stale(engine: Engine) -> int:
    """把「不是按当前窗型算出来」的 v_RMS 历史值标 ``stale=1``，返回标记行数。

    复查 v2 问题 5 / 批次 B-1：默认窗型由 hanning 改 rect 后，旧点幅值系统性偏低
    （10 Hz −10.31%），不能再当有效值参与判级。**只标记不重算**——24 万行重算会卡启动，
    重算走页面「重算本节点」或 ``scripts/recompute_stale.py``。

    识别口径：``metric_value.effective_params_json`` 是算这个点时实际生效的参数快照，
    当前口径的行里有 ``"window_fn":"rect"``；没有快照（历史行）或不是 rect 的按旧口径处理。

    跑一次就够：用 SQLite 内置的 ``PRAGMA user_version`` 记「已做过」，之后每次启动
    不再扫表（也避免把后来新造的、故意没写快照的测试数据当成旧口径）。
    """
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(metric_value)").fetchall()
        if not rows:
            return 0
        names = {row[1] for row in rows}
        if not {"stale", "effective_params_json", "code", "deleted_at"} <= names:
            return 0
        version = int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)
        if version >= DATA_VERSION_VRMS_RECT:
            return 0
        result = conn.exec_driver_sql(
            "UPDATE metric_value SET stale = 1"
            " WHERE code = 'v_RMS' AND stale = 0 AND deleted_at IS NULL"
            " AND (effective_params_json IS NULL OR effective_params_json NOT LIKE ?)",
            (f'%"window_fn":"{VRMS_WINDOW_FN}"%',),
        )
        marked = result.rowcount or 0
        conn.exec_driver_sql(f"PRAGMA user_version = {DATA_VERSION_VRMS_RECT}")
    if marked:
        _log.warning(
            "迁移：%d 条 v_RMS 由旧窗型（非 %s）算出，已标记待重算（未自动重算）",
            marked,
            VRMS_WINDOW_FN,
        )
    return marked


def _add_deletion_log_recycle(engine: Engine) -> None:
    """deletion_log 补 ``payload``（子树快照）与 ``expires_at``（保留期），幂等。"""
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(deletion_log)").fetchall()
        if not rows:
            return
        names = {row[1] for row in rows}
        added: list[str] = []
        if "payload" not in names:
            conn.exec_driver_sql("ALTER TABLE deletion_log ADD COLUMN payload TEXT DEFAULT '{}'")
            added.append("payload")
        if "expires_at" not in names:
            conn.exec_driver_sql("ALTER TABLE deletion_log ADD COLUMN expires_at DATETIME")
            added.append("expires_at")
        if added:
            _log.info("迁移：deletion_log 补列 %s", "、".join(added))


#: 软删友好的部分唯一索引（G3）：表 -> (索引名, 键列)
#: 键列以 models 的旧唯一约束为准，只是把 deleted_at 挪进 WHERE
_ACTIVE_UNIQUE_INDEXES: dict[str, tuple[str, tuple[str, ...]]] = {
    "metric_assign": ("uq_metric_assign_active", ("node_id", "metric_code")),
    "metric_limit": ("uq_metric_limit_active", ("node_id", "metric_code")),
}


def _table_sql(conn, table: str) -> str:
    return (
        conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).scalar()
        or ""
    )


def _has_legacy_deleted_at_unique(sql: str, keys: tuple[str, ...]) -> bool:
    """表 DDL 里是否还带 ``UNIQUE(键列..., deleted_at)`` 老约束。"""
    normalized = " ".join(sql.split()).lower()
    return f"unique ({', '.join(keys)}, deleted_at)" in normalized


def _soft_delete_duplicate_active_rows(conn, table: str, keys: tuple[str, ...]) -> int:
    """同键多行都未软删时只留最小 id，其余置 ``deleted_at``（不丢数据），返回软删条数。"""
    group = ", ".join(keys)
    rows = conn.exec_driver_sql(
        f"SELECT {group}, MIN(id) FROM {table} WHERE deleted_at IS NULL"
        f" GROUP BY {group} HAVING COUNT(*) > 1"
    ).fetchall()
    if not rows:
        return 0
    cond = " AND ".join(f"{key} = ?" for key in keys)
    stamp = datetime.now()
    marked = 0
    for row in rows:
        keep_id = row[len(keys)]
        # 先取成列表再改：游标还开着时改表会报 "database table is locked"
        victims = [
            item[0]
            for item in conn.exec_driver_sql(
                f"SELECT id FROM {table} WHERE {cond} AND deleted_at IS NULL AND id <> ?",
                (*row[: len(keys)], keep_id),
            ).fetchall()
        ]
        for victim in victims:
            conn.exec_driver_sql(f"UPDATE {table} SET deleted_at = ? WHERE id = ?", (stamp, victim))
            marked += 1
    return marked


def _rebuild_active_unique_index(conn, table: str, keys: tuple[str, ...]) -> int:
    """重建表去掉 ``UNIQUE(键列, deleted_at)`` 表约束，换成部分唯一索引；数据原样搬回。

    SQLite 不能 DROP 表约束（它是建表语句里的 ``sqlite_autoindex``），只能重建表。
    老约束下没法先改 ``deleted_at``（同键多行会撞 ``(键列, deleted_at)``），所以去重放进
    搬数据的 INSERT ... SELECT：同键未软删的行只让最小 id 保持 ``deleted_at IS NULL``，
    其余在搬的同时置 ``deleted_at``（软删，不丢数据）。返回被软删的条数。
    """
    from .models import Base

    old = f"{table}_old"
    conn.exec_driver_sql(f"ALTER TABLE {table} RENAME TO {old}")
    names = [
        row[0]
        for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (old,)
        ).fetchall()
    ]
    for name in names:
        if not name.startswith("sqlite_autoindex"):
            conn.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")
    Base.metadata.tables[table].create(conn)
    carry = [col for col in _table_columns(conn, table) if col in _table_columns(conn, old)]
    joined = ", ".join(carry)
    if "deleted_at" not in carry:
        conn.exec_driver_sql(f"INSERT INTO {table} ({joined}) SELECT {joined} FROM {old}")
        conn.exec_driver_sql(f"DROP TABLE {old}")
        return 0

    key_match = " AND ".join(f"o2.{key} = o.{key}" for key in keys)
    keep_id = f"(SELECT MIN(o2.id) FROM {old} AS o2 WHERE {key_match} AND o2.deleted_at IS NULL)"
    soft_deleted = (
        conn.exec_driver_sql(
            f"SELECT COUNT(*) FROM {old} AS o WHERE o.deleted_at IS NULL AND o.id <> {keep_id}"
        ).scalar()
        or 0
    )
    select_cols = ", ".join(
        f"CASE WHEN o.deleted_at IS NULL AND o.id <> {keep_id} THEN ? ELSE o.deleted_at END"
        if col == "deleted_at"
        else f"o.{col}"
        for col in carry
    )
    conn.exec_driver_sql(
        f"INSERT INTO {table} ({joined}) SELECT {select_cols} FROM {old} AS o",
        (datetime.now(),),
    )
    conn.exec_driver_sql(f"DROP TABLE {old}")
    return soft_deleted


def _add_active_unique_indexes(engine: Engine) -> None:
    """``metric_assign`` / ``metric_limit`` 改成「未软删才唯一」的部分唯一索引（G3），幂等。

    老库的唯一约束是表约束 ``UNIQUE(node_id, metric_code, deleted_at)``——SQLite 里 NULL 不等于
    NULL，等于没有约束；SQLite 也改不了已存在的表约束，只能重建表（沿用 ``_rebuild_metric_value``
    的做法）。重建前先把「同键且都未软删」的重复行按 id 只保留最小的一条、其余置 ``deleted_at``
    （软删不丢数据），条数记日志。
    """
    with engine.begin() as conn:
        tables = _tables(conn)
        for table, (index_name, keys) in _ACTIVE_UNIQUE_INDEXES.items():
            if table not in tables:
                continue
            present = {
                row[0]
                for row in conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
                ).fetchall()
            }
            if index_name in present:
                continue
            if _has_legacy_deleted_at_unique(_table_sql(conn, table), keys):
                soft_deleted = _rebuild_active_unique_index(conn, table, keys)
                _log.info("迁移：%s 去掉 UNIQUE(..., deleted_at) 表约束，改部分唯一索引", table)
            else:
                # 中间态（没有老约束也没有新索引）：补建前照样去重，免得建索引失败
                soft_deleted = _soft_delete_duplicate_active_rows(conn, table, keys)
                _log.info("迁移：%s 补部分唯一索引 %s", table, index_name)
                conn.exec_driver_sql(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table}"
                    f" ({', '.join(keys)}) WHERE deleted_at IS NULL"
                )
            if soft_deleted:
                _log.warning(
                    "%s: 迁移部分唯一索引时发现同键重复行 %d 条，已按 id 保留最小的一条、"
                    "其余置 deleted_at（未丢数据）",
                    table,
                    soft_deleted,
                )


def init_db(engine: Engine) -> None:
    """建表 + 结构迁移（幂等）。

    G15：迁移执行要能在日志里回看（哪一步真的动了表结构、软删了几行）。
    T2：检测到**需要改结构**时，先在线备份整库再迁移；备份失败就中止迁移并报中文错误
    （绝不允许「没备份就改表」）。
    """
    _log.info("数据库初始化/迁移开始")
    _backup_before_migration(engine)
    _run_migrations(engine)
    _purge_expired_recycle(engine)
    _log.info("数据库初始化/迁移完成")


def _run_migrations(engine: Engine) -> None:
    """真正跑迁移（``init_db`` 与影子库探测共用这一份逻辑，避免两套口径漂移）。"""
    Base.metadata.create_all(engine)
    _add_node_ref_columns(engine)
    # 补 channel_code 必须排在「sensor.node_id 改可空（重建表）」之前：重建用当前 ORM
    # 建表，新表 channel_code 是 NOT NULL，老表没这列时搬数据会直接撞 NOT NULL
    # （复查 v2 阻塞项 1）。
    _add_sensor_channel_code(engine)
    _relax_sensor_node_nullable(engine)
    _relax_channel_reg_object_nullable(engine)
    _free_unassembled_channels(engine)
    _add_task_node_refs(engine)
    _add_metric_value_trace(engine)
    _add_metric_value_node(engine)
    _add_metric_value_indexes(engine)
    _add_metric_def_params(engine)
    _add_metric_assign_locked(engine)
    _add_deletion_log_recycle(engine)
    _add_object_rated_rpm(engine)
    _add_dataset_measured_rpm(engine)
    _add_active_unique_indexes(engine)
    migrate_metric_def_schema(engine)
    migrate_metric_def_band_params(engine)
    migrate_limits_to_metric_nodes(engine)
    migrate_assigns_to_metric_nodes(engine)
    ensure_metric_assign_unique_index(engine)
    # 数据迁移（不改结构）：旧窗型算出的 v_RMS 标 stale，等用户重算（批次 B-1）
    mark_legacy_vrms_stale(engine)


def _purge_expired_recycle(engine: Engine) -> None:
    """应用启动时清理超过保留期（默认 7 天）的节点留痕及其软删行（G6）。

    清理失败不阻断启动：回收站过期数据留着只是占空间，报日志即可。
    """
    from .recycle import purge_expired_logs  # 延迟 import：recycle 依赖 tree，避免环

    try:
        purge_expired_logs(engine)
    except Exception:  # noqa: BLE001 - 启动路径不能因为清理失败而挂掉
        _log.warning("回收站保留期清理失败（不影响启动）", exc_info=True)


# ---------------------------------------------------------------- 迁移前备份（T2）


def _schema_signature(conn) -> dict[tuple[str, str], str]:
    """库结构指纹：``{(类型, 名字): 归一化 DDL}``，只取表和索引等真实对象。"""
    rows = conn.exec_driver_sql(
        "SELECT type, name, sql FROM sqlite_master"
        " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {(str(kind), str(name)): " ".join(str(sql).split()).lower() for kind, name, sql in rows}


def _shadow_engine(engine: Engine) -> tuple[Engine, sqlite3.Connection]:
    """按真库的 DDL 建一个内存影子库（只有结构、没有数据），用来预演迁移。"""
    raw = sqlite3.connect(":memory:")
    try:
        with engine.connect() as conn:
            rows = conn.exec_driver_sql(
                "SELECT type, name, sql FROM sqlite_master"
                " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        # 先建表再建索引/触发器/视图，顺序反了会报 no such table
        for _kind, _name, sql in [row for row in rows if row[0] == "table"]:
            raw.execute(sql)
        for _kind, _name, sql in [row for row in rows if row[0] != "table"]:
            raw.execute(sql)
        raw.commit()
    except Exception:
        raw.close()
        raise
    shadow = create_engine("sqlite://", creator=lambda: raw, poolclass=StaticPool, future=True)
    return shadow, raw


def pending_migrations(engine: Engine) -> list[str]:
    """预演一遍迁移，返回会变的结构项（如 ``table:sensor``）；空库/已最新返回空列表。

    做法：把真库的 DDL 搬进内存影子库跑同一份 ``_run_migrations``，比较前后结构指纹
    ——口径与真实迁移完全一致，不会出现「检测说不用迁移、实际却改表」的偏差。
    真库只读，不会被改动。
    """
    with engine.connect() as conn:
        before = _schema_signature(conn)
    if not before:
        return []  # 全新空库：建表不算迁移，不需要备份
    shadow, raw = _shadow_engine(engine)
    level = _log.level
    _log.setLevel(logging.WARNING)  # 影子库里的「迁移：…」日志不算数，别误导操作员
    try:
        _run_migrations(shadow)
        with shadow.connect() as conn:
            after = _schema_signature(conn)
    finally:
        _log.setLevel(level)
        shadow.dispose()
        raw.close()
    changed = [
        f"{kind}:{name}"
        for (kind, name), sql in sorted(after.items())
        if before.get((kind, name)) != sql
    ]
    dropped = [f"{kind}:{name}" for kind, name in sorted(before) if (kind, name) not in after]
    return changed + dropped


def _engine_db_path(engine: Engine) -> Path | None:
    """引擎指向的库文件；内存库返回 None。"""
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser()


def _backup_before_migration(engine: Engine) -> None:
    """要改结构就先整库备份；备份失败抛 :class:`BackupError` 中止迁移。"""
    path = _engine_db_path(engine)
    if path is None or not path.is_file():
        return
    try:
        pending = pending_migrations(engine)
    except Exception:  # noqa: BLE001 - 探测失败就按「要迁移」处理，宁可多备一份
        _log.warning("迁移前结构检测失败，按「需要迁移」处理（先备份）", exc_info=True)
        pending = ["<结构检测失败>"]
    if not pending:
        return
    try:
        info = backup_database(path)
    except BackupError as exc:
        raise BackupError(f"迁移中止：迁移前自动备份失败（{exc}）") from exc
    _log.info(
        "检测到 %d 项结构变更（%s），迁移前已自动备份：%s（%s）",
        len(pending),
        "、".join(pending[:5]) + ("…" if len(pending) > 5 else ""),
        info.path,
        info.size_text(),
    )


def ensure_metric_assign_unique_index(engine: Engine) -> None:
    """给"活着的"参数卡加唯一约束：同一任务、同一指标节点、同一指标码只能有一行。

    先按 id 保留最新的一条、其余软删，再建部分唯一索引（``deleted_at IS NULL`` 才生效），
    这样回收站里的历史行不会挡住重新绑定。幂等。
    """
    with engine.begin() as conn:
        tables = {row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master")}
        if "metric_assign" not in tables:
            return
        conn.exec_driver_sql(
            "UPDATE metric_assign SET deleted_at = datetime('now', 'localtime') "
            "WHERE deleted_at IS NULL AND id NOT IN ("
            "  SELECT MAX(id) FROM metric_assign WHERE deleted_at IS NULL"
            "  GROUP BY task_id, node_id, metric_code"
            ")"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_metric_assign_active "
            "ON metric_assign(task_id, node_id, metric_code) WHERE deleted_at IS NULL"
        )


def migrate_assigns_to_metric_nodes(engine: Engine) -> int:
    """metric_assign.node_id：通道节点 → 指标节点（参数配置下放到指标节点），幂等。

    2026-11-02：指标参数属于指标，不属于通道。返回搬动的条数。
    找不到对应指标节点的行（通道节点已删 / 指标没装配）软删留痕，不丢数据。
    """
    from sqlalchemy import select, update

    from .models import MetricAssign, TaskNode

    moved = 0
    with Session(engine) as session:
        rows = list(session.execute(select(MetricAssign)).scalars())
        if not rows:
            return 0
        nodes = list(session.execute(select(TaskNode)).scalars())
        by_id = {node.id: node for node in nodes}
        metric_of: dict[tuple[int, str], int] = {}
        for node in nodes:
            if node.node_type != "metric" or node.parent_id is None or node.deleted_at is not None:
                continue
            code = (json.loads(node.attrs_json or "{}") or {}).get("code") or node.name
            metric_of.setdefault((node.parent_id, code), node.id)

        occupied = {
            (row.node_id, row.metric_code)
            for row in rows
            if row.deleted_at is None
            and by_id.get(row.node_id) is not None
            and by_id[row.node_id].node_type == "metric"
        }
        stamp = datetime.now()
        for row in rows:
            if row.deleted_at is not None:
                continue
            node = by_id.get(row.node_id)
            if node is None or node.node_type != "channel":
                continue
            target = metric_of.get((node.id, row.metric_code))
            if target is None or (target, row.metric_code) in occupied:
                session.execute(
                    update(MetricAssign).where(MetricAssign.id == row.id).values(deleted_at=stamp)
                )
                continue
            session.execute(
                update(MetricAssign).where(MetricAssign.id == row.id).values(node_id=target)
            )
            occupied.add((target, row.metric_code))
            moved += 1
        session.commit()
    if moved:
        _log.info("迁移：指标参数卡从通道节点搬到指标节点 %d 条", moved)
    return moved


def migrate_limits_to_metric_nodes(engine: Engine) -> int:
    """把挂在通道节点上的阈值搬到同通道下同名指标节点（指标级），幂等。

    返回搬动的条数。指标节点不存在时保持原样（例如指标还没装配）。
    """
    from sqlalchemy import select, update

    from .models import MetricLimit, TaskNode

    moved = 0
    with Session(engine) as session:
        rows = list(session.execute(select(MetricLimit)).scalars())
        if not rows:
            return 0
        nodes = list(session.execute(select(TaskNode)).scalars())
        by_id = {node.id: node for node in nodes}
        metric_of: dict[tuple[int, str], int] = {}
        for node in nodes:
            if node.node_type != "metric" or node.parent_id is None:
                continue
            code = (json.loads(node.attrs_json or "{}") or {}).get("code") or node.name
            metric_of[(node.parent_id, code)] = node.id

        for row in rows:
            node = by_id.get(row.node_id)
            if node is None or node.node_type == "metric":
                continue
            target = metric_of.get((node.id, row.metric_code))
            if target is None:
                continue
            session.execute(
                update(MetricLimit).where(MetricLimit.id == row.id).values(node_id=target)
            )
            moved += 1
        session.commit()
    if moved:
        _log.info("迁移：把 %d 条通道级阈值搬到同名指标节点", moved)
    return moved


def migrate_metric_def_schema(engine: Engine) -> int:
    """给「参数声明」为空的内置指标补上 ``PARAM_SCHEMA`` / ``PARAM_DEFAULTS``，幂等。

    返回补充的条数。``params_schema_json`` 是后加列（见 ``_add_metric_def_params``），
    老库里的行只拿到默认空声明 ``[]``，界面因此渲染不出参数表单——健康树「监测指标」
    页签绑定参数后只显示「该指标没有声明参数 schema，按系统默认值计算」，
    用户改不了「计算时长 / 计算步长」。

    口径：只补**空声明**且指标码在内置 schema 里的行；已有自定义声明（可能是裁过的
    字段集）与未知指标码原样保留，默认值仅在为空时补。
    """
    from sqlalchemy import select

    from ..params import PARAM_DEFAULTS, PARAM_SCHEMA
    from .models import MetricDef

    def _json_list(raw: str | None) -> list:
        try:
            parsed = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []

    def _json_dict(raw: str | None) -> dict:
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    filled = 0
    with Session(engine) as session:
        for row in session.execute(select(MetricDef)).scalars():
            schema = PARAM_SCHEMA.get(row.code)
            if not schema:
                continue
            if _json_list(row.params_schema_json):
                continue
            row.params_schema_json = json.dumps(schema, ensure_ascii=False)
            if not _json_dict(row.params_default_json):
                row.params_default_json = json.dumps(
                    PARAM_DEFAULTS.get(row.code, {}), ensure_ascii=False
                )
            filled += 1
        if filled:
            session.commit()
            _log.info("迁移：%d 条指标定义补上参数声明（计算时长/步长可改了）", filled)
    return filled


def migrate_metric_def_band_params(engine: Engine) -> int:
    """把 ``PARAM_SCHEMA`` 里**新增**的参数补进已有的内置指标定义（幂等）。

    2026-11-02：a_RMS 也要有滤波设置（band_low / band_high）。老库里的 a_RMS
    声明里没有这两项，界面就不会渲染——这里按 key 补齐，已有项一律不动（用户改过的
    默认值不会被覆盖）。返回补充的参数条数。
    """
    from sqlalchemy import select

    from ..params import PARAM_DEFAULTS, PARAM_SCHEMA
    from .models import MetricDef

    def _json_list(raw: str | None) -> list:
        try:
            parsed = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []

    def _json_dict(raw: str | None) -> dict:
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    added = 0
    with Session(engine) as session:
        for row in session.execute(select(MetricDef)).scalars():
            schema = PARAM_SCHEMA.get(row.code)
            if not schema:
                continue
            current = _json_list(row.params_schema_json)
            have = {item.get("key") for item in current if isinstance(item, dict)}
            missing = [item for item in schema if item["key"] not in have]
            defaults = _json_dict(row.params_default_json)
            default_missing = {
                key: value
                for key, value in PARAM_DEFAULTS.get(row.code, {}).items()
                if key not in defaults
            }
            if not missing and not default_missing:
                continue
            if missing:
                row.params_schema_json = json.dumps(current + missing, ensure_ascii=False)
            if default_missing:
                row.params_default_json = json.dumps(
                    {**defaults, **default_missing}, ensure_ascii=False
                )
            added += len(missing)
        if added:
            session.commit()
            _log.info("迁移：%d 个指标参数补进指标定义声明（a_RMS 带通等）", added)
    return added


def begin_immediate(session: Session) -> bool:
    """写事务开头显式取 SQLite 写锁（``BEGIN IMMEDIATE``），返回是否真的发起了事务。

    为什么需要：``session.begin_nested()`` 先发 ``SAVEPOINT``，SQLite 因此开的是
    **DEFERRED** 事务；批次导入开头先读（取编号、查已有批次），后面写时要把读事务升级成
    写事务，若另一个写者正持锁就得到 ``SQLITE_BUSY_SNAPSHOT``——这个错误**不调用 busy
    handler**，``busy_timeout`` 形同虚设，第二个导入 0.05 s 内就报 ``database is locked``
    （复查 v2 严重项 2）。

    在 ``begin_nested()`` / 任何读语句之前调本函数，写者会在入口按 ``busy_timeout``
    排队，而不是升级时失败。已经在事务里（``in_transaction``）则什么都不做——那是
    调用方已经持有的写事务，重复 ``BEGIN`` 会报 ``cannot start a transaction within
    a transaction``。
    """
    conn = session.connection()
    dbapi = conn.connection.dbapi_connection
    if dbapi is not None and dbapi.in_transaction:
        return False
    conn.exec_driver_sql("BEGIN IMMEDIATE")
    return True


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """会话工厂：提交后对象仍可读（expire_on_commit=False）。"""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """自动提交/回滚的会话上下文。"""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _add_node_ref_columns(engine) -> None:
    """给老库补 task_node.ref_type / ref_id（SQLite ADD COLUMN，幂等）。"""
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "task_node" not in tables:
            return
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(task_node)")}
        if "ref_type" not in cols:
            _log.info("迁移：task_node 补 ref_type 列")
            conn.exec_driver_sql("ALTER TABLE task_node ADD COLUMN ref_type VARCHAR(16) DEFAULT ''")
        if "ref_id" not in cols:
            _log.info("迁移：task_node 补 ref_id 列")
            conn.exec_driver_sql("ALTER TABLE task_node ADD COLUMN ref_id INTEGER")
