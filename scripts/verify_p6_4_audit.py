"""P6-4 验收实跑：参数 / 阈值变更审计留痕（T3）。

跑法（项目根目录）::

    .venv/bin/python scripts/verify_p6_4_audit.py

只写临时目录（``~/workspace/temp/<日期>/p6-4/``），不碰 ``data/lion.db``（前后比对 sha256，
迁移实验都在真库副本上做）；证据同时写进 ``reports/2026-11-02-p6-4-verification.txt``。
任一条不通过即非零退出。
"""

from __future__ import annotations

# 本脚本要在 sys.path 插入 src/app 之后再 import 项目模块，故整文件豁免 E402
# ruff: noqa: E402
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "app"))

from streamlit.testing.v1 import AppTest  # noqa: E402

from lion_monitor.store import (  # noqa: E402
    audit,
    channel_reg,
    limits,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store.models import MetricLimit  # noqa: E402

PY = sys.executable
REAL_DB = ROOT / "data" / "lion.db"
WORK = Path.home() / "workspace" / "temp" / datetime.now().strftime("%Y-%m-%d") / "p6-4"
OUT = ROOT / "reports" / "2026-11-02-p6-4-verification.txt"

lines: list[str] = []
results: list[tuple[str, bool]] = []


def say(text: str = "") -> None:
    print(text)
    lines.append(text)


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok))
    say(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def query(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, params).fetchall()


def change_rows(db: Path, entity_type: str | None = None) -> list[tuple]:
    sql = "select entity_type, entity_id, field, old_value, new_value, actor, note from change_log"
    if entity_type:
        sql += " where entity_type=?"
        return query(db, sql + " order by id", (entity_type,))
    return query(db, sql + " order by id")


def build_line(session, task) -> dict:
    """任务根 → 设备 → 监测对象 → 通道 → 指标，外加 1 个指标授权。"""
    obj = object_registry.create_object(session, task.id, "隔膜过辊轴承", "轴承", part="隔膜过辊")
    ch_reg = channel_reg.create_channel(session, task.id, obj.id, "CH1")
    metric_def.seed_defaults(session, task.id)
    defs = {row.code: row for row in metric_def.list_metrics(session, task.id)}
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    component = tree.create_node(
        session,
        task.id,
        "component",
        "隔膜过辊轴承",
        parent_id=device.id,
        attrs={"部位": "隔膜过辊"},
        ref_id=obj.id,
    )
    channel = tree.create_node(
        session, task.id, "channel", "CH1", parent_id=component.id, ref_id=ch_reg.id
    )
    metric = tree.create_node(
        session,
        task.id,
        "metric",
        "v_RMS",
        parent_id=channel.id,
        attrs={"code": "v_RMS", "单位": "mm/s"},
        ref_id=defs["v_RMS"].id,
    )
    assign = metric_assign.assign_metric(session, task.id, channel.id, "v_RMS")
    return {"channel": channel, "metric": metric, "assign": assign}


say("=" * 78)
say("P6-4 验收实跑：参数 / 阈值变更审计留痕（T3）")
say(f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}　项目：{ROOT}")
say("=" * 78)

if WORK.exists():
    shutil.rmtree(WORK)  # 每次从空目录开始，避免上次的库影响条数
WORK.mkdir(parents=True, exist_ok=True)
real_sha_before = sha(REAL_DB) if REAL_DB.is_file() else "(无真库)"
db_path = WORK / "lion.db"
engine = store_db.make_engine(db_path)
store_db.init_db(engine)
factory = store_db.make_session_factory(engine)
session = factory()
task = tasks.create_task(session, name="卷绕产线振动监测", operator="mark")
ids = build_line(session, task)
say("")
say(f"实验库：{db_path}")
say(
    f"任务：{task.code} {task.name}（操作人 {task.operator}）　"
    f"节点：{', '.join(node.name for node in tree.list_nodes(session, task.id))}"
)
say(f"change_log 建表后条数：{len(change_rows(db_path))}")

# ---------------------------------------------------------------- 验收 1
say("")
say("【验收 1】阈值 warn=5/alert=10 → warn=6/alert=12 → change_log 2 行，含 old/new 与 actor")
# 老数据：阈值行直接落库（不经过 set_limit，因此没有「创建」留痕，条数只反映这次改动）
session.add(
    MetricLimit(
        task_id=task.id,
        node_id=ids["metric"].id,
        metric_code="v_RMS",
        warn=5.0,
        alert=10.0,
        direction=">",
        enabled=1,
    )
)
session.commit()
say(f"改动前 change_log 条数：{len(change_rows(db_path))}")
row = limits.set_limit(session, task.id, ids["metric"].id, "v_RMS", 6.0, 12.0)
rows = change_rows(db_path, "metric_limit")
say(f"set_limit 返回：warn={row.warn} alert={row.alert} direction={row.direction}")
for item in rows:
    say(f"  留痕：{item}")
parsed = [(item[2], json.loads(item[3]), json.loads(item[4]), item[5]) for item in rows]
check(
    "1 阈值改动写 2 行（warn 5→6 / alert 10→12），带 old/new 与操作人",
    len(parsed) == 2
    and {field: (old, new) for field, old, new, _actor in parsed}
    == {"warn": (5.0, 6.0), "alert": (10.0, 12.0)}
    and {actor for *_, actor in parsed} == {"mark"},
    f"{len(parsed)} 行 / 字段 {[field for field, *_ in parsed]}",
)

# ---------------------------------------------------------------- 验收 2
say("")
say("【验收 2】保存与旧值相同的参数 → change_log 0 行新增")
before_ids = len(change_rows(db_path))
same = {"band_high": 500.0, "band_low": 20.0}
metric_assign.update_params(session, ids["assign"].id, same)
after_first = len(change_rows(db_path))
metric_assign.update_params(session, ids["assign"].id, same)
after_second = len(change_rows(db_path))
say(f"change_log 条数：改动前 {before_ids} → 首次保存 {after_first} → 同值再保存 {after_second}")
check(
    "2 同值参数再保存不写留痕",
    after_second == after_first == before_ids + 1,
    f"首次 +{after_first - before_ids} 行，同值再存 +{after_second - after_first} 行",
)

# ---------------------------------------------------------------- 验收 3
say("")
say("【验收 3】参数覆盖保存 → 1 行，entity_type=metric_assign，old/new 是只含改动键的 dict 差异")
metric_assign.update_params(session, ids["assign"].id, {"band_high": 800.0, "band_low": 20.0})
rows = change_rows(db_path, "metric_assign")
say(f"metric_assign 留痕条数：{len(rows)}")
for item in rows:
    say(f"  留痕：{item}")
latest = rows[-1]
old_diff = json.loads(latest[3])
new_diff = json.loads(latest[4])
say(f"最新一条：field={latest[2]}　old={old_diff}　new={new_diff}　actor={latest[5]}　note={latest[6]}")
check(
    "3 参数覆盖保存只记改动键（band_high 500→800，band_low 不动）",
    latest[0] == "metric_assign"
    and latest[2] == "params_json"
    and old_diff == {"band_high": 500.0}
    and new_diff == {"band_high": 800.0}
    and latest[5] == "mark",
    f"old={old_diff} new={new_diff}",
)

# ---------------------------------------------------------------- 其余写入点
say("")
say("【附加 1】其余写入点也留痕：固化开关、指标定义默认值、节点转速")
metric_assign.set_locked(session, ids["assign"].id, True)
ledger = metric_def.find_metric(session, task.id, "v_RMS")
assert ledger is not None
metric_def.update_metric(session, ledger.id, defaults={**ledger.defaults, "band_high": 900.0})
metric_assign.set_node_rpm(session, ids["channel"].id, 1480.0)
assign_rows = change_rows(db_path, "metric_assign")
def_rows = change_rows(db_path, "metric_def")
node_rows = change_rows(db_path, "tree_node")
for item in assign_rows + def_rows + node_rows:
    say(f"  留痕：{item}")
check(
    "附加 1 固化/指标定义默认值/节点转速三个写入点都有留痕",
    any(item[2] == "params_locked" for item in assign_rows)
    and any(item[2] == "params_default_json" for item in def_rows)
    and any(item[2] == "转速" for item in node_rows),
    f"metric_assign {len(assign_rows)} 行 / metric_def {len(def_rows)} 行 / tree_node {len(node_rows)} 行",
)

# ---------------------------------------------------------------- 验收 4
say("")
say("【验收 4】阈值设置页「变更历史」区读出这些留痕（AppTest 真跑）")
page_db = WORK / "page" / "lion.db"
page_db.parent.mkdir(parents=True, exist_ok=True)
page_engine = store_db.make_engine(page_db)
store_db.init_db(page_engine)
with store_db.make_session_factory(page_engine)() as page_session:
    page_task = tasks.create_task(page_session, name="变更历史页面实验", operator="mark")
    page_ids = build_line(page_session, page_task)
    page_session.add(
        MetricLimit(
            task_id=page_task.id,
            node_id=page_ids["metric"].id,
            metric_code="v_RMS",
            warn=5.0,
            alert=10.0,
            direction=">",
            enabled=1,
        )
    )
    page_session.commit()
    limits.set_limit(page_session, page_task.id, page_ids["metric"].id, "v_RMS", 6.0, 12.0)
    metric_assign.update_params(page_session, page_ids["assign"].id, {"band_high": 500.0})
os.environ["LION_DB"] = str(page_db)
import store_ctx  # noqa: E402

store_ctx._session_factory.clear()
at = AppTest.from_file(str(ROOT / "app" / "pages" / "thresholds.py"), default_timeout=120)
at.run()
history = None
for element in at.dataframe:
    if "旧值 → 新值" in list(element.value.columns):
        history = element.value
say(f"页面异常：{[exc.value for exc in at.exception] or '无'}")
say(f"「变更历史」表列：{list(history.columns) if history is not None else '(没找到表格)'}")
if history is not None:
    for _, line in history.iterrows():
        say(
            f"  {line['时间']}　{line['实体']}　{line['对象']}　{line['字段']}　"
            f"{line['旧值 → 新值']}　{line['操作人']}"
        )
    at.selectbox(key="chg_entity").set_value("指标参数").run()
    filtered = None
    for element in at.dataframe:
        if "旧值 → 新值" in list(element.value.columns):
            filtered = element.value
    say(f"筛选「指标参数」后行数：{len(filtered) if filtered is not None else '(没找到表格)'}")
else:
    filtered = None
check(
    "4 页面「变更历史」读出阈值 2 条 + 参数 1 条，且能按实体类型过滤",
    history is not None
    and len(history) == 3
    and set(history["旧值 → 新值"].tolist()[1:]) == {"5 → 6", "10 → 12"}
    and history.iloc[0]["实体"].startswith("指标参数 #")
    and filtered is not None
    and len(filtered) == 1,
    f"{len(history)} 行" if history is not None else "无表格",
)
store_ctx._session_factory.clear()
os.environ.pop("LION_DB", None)

# ---------------------------------------------------------------- 真库副本迁移
say("")
say("【附加 2】老库迁移：真库副本上 init_db 会补 change_log 表（迁移前先整库备份）")
if REAL_DB.is_file():
    copy_path = WORK / "real-copy" / "lion.db"
    copy_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True) as source:
        with sqlite3.connect(copy_path) as target:
            source.backup(target)  # WAL 库也能拿到一致快照
    copy_engine = store_db.make_engine(copy_path)
    pending = store_db.pending_migrations(copy_engine)
    say(f"副本 pending_migrations：{pending}")
    tables_before = {
        name for (name,) in query(copy_path, "select name from sqlite_master where type='table'")
    }
    counts_before = {
        table: query(copy_path, f"select count(*) from {table}")[0][0]
        for table in ("test_task", "task_node", "metric_assign", "metric_limit", "deletion_log")
        if table in tables_before
    }
    say(f"迁移前表：{sorted(tables_before)}")
    say(f"迁移前各表行数：{counts_before}")
    store_db.init_db(copy_engine)
    tables_after = {
        name for (name,) in query(copy_path, "select name from sqlite_master where type='table'")
    }
    columns = {row[1] for row in query(copy_path, "pragma table_info(change_log)")}
    counts_after = {
        table: query(copy_path, f"select count(*) from {table}")[0][0] for table in counts_before
    }
    pending_after = store_db.pending_migrations(copy_engine)
    backups = list((copy_path.parent / "backups").glob("lion-*.db"))
    say(f"迁移后 change_log 列：{sorted(columns)}")
    say(f"迁移后各表行数：{counts_after}　重复 init_db 的 pending：{pending_after}")
    say(f"迁移前备份：{[item.name for item in backups]}")
    check(
        "附加 2 真库副本补出 change_log（原有行数不变、幂等、迁移前已备份）",
        "change_log" in tables_after
        and columns
        >= {
            "id",
            "entity_type",
            "entity_id",
            "field",
            "old_value",
            "new_value",
            "actor",
            "created_at",
            "note",
        }
        and counts_after == counts_before
        and pending_after == []
        and bool(backups),
        f"change_log 列 {len(columns)} 个，备份 {len(backups)} 份",
    )
    copy_engine.dispose()
else:
    check("附加 2 真库副本补出 change_log", False, "data/lion.db 不存在，跳过实测")

# ---------------------------------------------------------------- 真库未被改动
say("")
say("【附加 3】真库 data/lion.db 未被本次验收改动")
real_sha_after = sha(REAL_DB) if REAL_DB.is_file() else "(无真库)"
check(
    "附加 3 真库 sha256 前后一致（实验都在临时目录）",
    real_sha_before == real_sha_after,
    f"前后 sha256 {real_sha_before[:16]}… / {real_sha_after[:16]}…",
)

# ---------------------------------------------------------------- 回归与静态检查
say("")
say("【回归】pytest 全量 + ruff check / format --check（真实输出）")


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([PY, "-m", *args], capture_output=True, text=True, cwd=ROOT, check=False)


pytest_proc = run("pytest")
say("$ .venv/bin/python -m pytest")
say("\n".join(pytest_proc.stdout.strip().splitlines()[-3:]))
check(
    "回归 pytest 全绿",
    pytest_proc.returncode == 0,
    f"exit={pytest_proc.returncode}",
)

ruff_check = subprocess.run(
    [str(ROOT / ".venv" / "bin" / "ruff"), "check", "src", "app", "tests"],
    capture_output=True,
    text=True,
    cwd=ROOT,
    check=False,
)
say("$ .venv/bin/ruff check src app tests")
say(ruff_check.stdout.strip() or ruff_check.stderr.strip())
check("回归 ruff check 通过", ruff_check.returncode == 0, f"exit={ruff_check.returncode}")

ruff_fmt = subprocess.run(
    [str(ROOT / ".venv" / "bin" / "ruff"), "format", "--check", "src", "app", "tests"],
    capture_output=True,
    text=True,
    cwd=ROOT,
    check=False,
)
say("$ .venv/bin/ruff format --check src app tests")
say(ruff_fmt.stdout.strip() or ruff_fmt.stderr.strip())
check("回归 ruff format 通过", ruff_fmt.returncode == 0, f"exit={ruff_fmt.returncode}")

# ---------------------------------------------------------------- 关键代码位置
say("")
say("【关键代码位置】")
say("  store/models.py          ChangeLog（change_log 表：entity_type/entity_id/field/old/new/actor/note）")
say("  store/audit.py           record_change / record_fields / record_param_diff / list_changes / value_text")
say("  store/limits.py          set_limit 写阈值留痕（warn/alert/direction 只记真正变化的字段）")
say("  store/metric_assign.py   update_params / set_locked 写参数留痕（dict 差异合成一条）")
say("  store/metric_def.py      update_metric 写指标定义留痕（defaults / schema / unit / enabled / code）")
say("  store/tree.py            update_node 写节点转速留痕（含健康树属性表单改转速的路径）")
say("  app/pages/thresholds.py  「变更历史」区：实体过滤 / 倒序 / 最多 200 条")
say("")
say("【未做项】")
say("  - 只留痕不重算：改参数/阈值后历史指标值不自动重算（Q4：重算留手动入口，本轮不做）")
say("  - 阈值「删除」与模板导入直写 metric_limit 的路径不在本卡写入点清单内，不留痕")
say("  - change_log 无 task_id 列：页面按实体类型过滤（单人单机单任务场景），未做跨任务隔离")
say("  - 趋势图标注「哪天改的阈值」未做（留痕已可查，图上标注属后续）")

say("")
passed = sum(1 for _name, ok in results if ok)
say(f"结论：{passed}/{len(results)} 项通过")
say(f"临时目录：{WORK}")
if passed != len(results):
    say("有验收项未通过，见上面的 FAIL 行。")

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"\n证据已写入：{OUT}")
raise SystemExit(0 if passed == len(results) else 1)
