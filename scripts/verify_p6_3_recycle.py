"""P6-3 验收实跑：节点级回收站 + 7 天保留期（G6）。

跑法（项目根目录）::

    .venv/bin/python scripts/verify_p6_3_recycle.py

只写临时目录（``~/workspace/temp/<日期>/p6-3/``），不碰 ``data/lion.db``（前后比对 sha256）；
证据同时写进 ``reports/2026-11-02-p6-3-verification.txt``。任一条不通过即非零退出。
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
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "app"))

from streamlit.testing.v1 import AppTest  # noqa: E402

from lion_monitor.store import (  # noqa: E402
    channel_reg,
    limits,
    metric_assign,
    metric_def,
    object_registry,
    recycle,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402

PY = sys.executable
REAL_DB = ROOT / "data" / "lion.db"
WORK = Path.home() / "workspace" / "temp" / datetime.now().strftime("%Y-%m-%d") / "p6-3"
OUT = ROOT / "reports" / "2026-11-02-p6-3-verification.txt"

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


def log_count(db: Path, entity_type: str, mode: str = "soft") -> int:
    return query(
        db,
        "select count(*) from deletion_log where entity_type=? and mode=?",
        (entity_type, mode),
    )[0][0]


def build_line(session, task):
    """任务根 → 设备 → 监测对象（2 子节点：通道 + 指标）→ 1 授权 + 1 阈值。"""
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
        attrs={"类型": "轴承"},
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
    metric_assign.assign_metric(session, task.id, channel.id, "v_RMS", params={"band_high": 2000})
    limits.set_limit(session, task.id, metric.id, "v_RMS", 1.8, 2.8)
    return {
        "root": root,
        "device": device,
        "component": component,
        "channel": channel,
        "metric": metric,
    }


def flatten(node: dict) -> list[dict]:
    out = [node]
    for child in node.get("children") or []:
        out.extend(flatten(child))
    return out


say("=" * 78)
say("P6-3 验收实跑：节点级回收站 + 7 天保留期（G6）")
say(f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}　项目：{ROOT}")
say("=" * 78)

if WORK.exists():
    shutil.rmtree(WORK)  # 每次跑都从空目录开始，避免上次的库/文件影响条数
WORK.mkdir(parents=True, exist_ok=True)
real_sha_before = sha(REAL_DB) if REAL_DB.is_file() else "(无真库)"
db_path = WORK / "lion.db"
engine = store_db.make_engine(db_path)
store_db.init_db(engine)
factory = store_db.make_session_factory(engine)
session = factory()
task = tasks.create_task(session, name="卷绕产线振动监测")
ids = build_line(session, task)
say("")
say(f"实验库：{db_path}")
node_names = ", ".join(node.name for node in tree.list_nodes(session, task.id))
say(f"任务：{task.code} {task.name}　节点：{node_names}")

# ---------------------------------------------------------------- 验收 1
say("")
say("【验收 1】删除带 2 个子节点 + 1 个指标授权的节点 → deletion_log 1 行，payload 含完整子树")
say(f"删除前 task_node 留痕条数：{log_count(db_path, 'task_node')}")
before_assigns = len(query(db_path, "select id from metric_assign where deleted_at is null"))
before_limits = len(query(db_path, "select id from metric_limit where deleted_at is null"))
counts = tree.soft_delete_node(session, ids["component"].id, operator="小智")
after_logs = log_count(db_path, "task_node")
rows = query(
    db_path,
    "select id, entity_id, entity_name, operator, expires_at, payload"
    " from deletion_log where entity_type='task_node' order by id",
)
say(f"soft_delete_node 返回：{counts}")
say(f"删除后 task_node 留痕条数：{after_logs}")
log_id, entity_id, entity_name, operator, expires_at, payload_raw = rows[-1]
payload = json.loads(payload_raw)
subtree = flatten(payload["node"])
say(f"留痕：id={log_id} entity_id={entity_id} operator={operator}")
say(f"路径（entity_name）：{entity_name}")
say(f"到期时间（expires_at）：{expires_at}")
say(f"payload 子树节点：{[node['name'] for node in subtree]}")
say(
    f"payload 授权：{[row['metric_code'] for row in payload['assigns']]}　"
    f"阈值：{[row['metric_code'] for row in payload['limits']]}"
)
after_assigns = len(query(db_path, "select id from metric_assign where deleted_at is null"))
after_limits = len(query(db_path, "select id from metric_limit where deleted_at is null"))
say(
    f"授权/阈值有效条数（前）：metric_assign {before_assigns}、metric_limit {before_limits}；"
    f"（后）metric_assign {after_assigns}、metric_limit {after_limits}"
)
check(
    "1 删除写 1 行留痕且 payload 含完整子树（节点 3 / 授权 1 / 阈值 1）",
    after_logs == 1
    and [node["name"] for node in subtree] == ["隔膜过辊轴承", "CH1", "v_RMS"]
    and len(payload["assigns"]) == 1
    and len(payload["limits"]) == 1,
    f"留痕 {after_logs} 行，payload 节点 {len(subtree)} 个",
)

# ---------------------------------------------------------------- 验收 2
say("")
say("【验收 2】回收站列出该条，恢复后节点、子树、指标授权全部回来，deleted_at 为空")
items = recycle.list_recycle_items(session)
for item in items:
    say(
        f"回收站行：类型={item.entity_label}　名称={item.entity_name}　"
        f"删除时间={item.deleted_at:%Y-%m-%d %H:%M:%S}　剩余天数={item.remaining_days}　"
        f"级联={item.cascade}"
    )
result = recycle.restore_item(session, items[0].log_id)
say(f"恢复结果：ok={result.ok}　提示={result.message}")
names_after = [node.name for node in tree.list_nodes(session, task.id)]
deleted_flags = query(
    db_path,
    "select id, name, deleted_at from task_node where id in (?, ?, ?) order by id",
    (ids["component"].id, ids["channel"].id, ids["metric"].id),
)
assigns_after = query(
    db_path,
    "select node_id, metric_code, params_json, deleted_at from metric_assign"
    " where deleted_at is null",
)
limits_after = query(
    db_path,
    "select node_id, metric_code, warn, alert, deleted_at from metric_limit"
    " where deleted_at is null",
)
say(f"恢复后树节点：{names_after}")
say(f"节点删除态（id,name,deleted_at）：{deleted_flags}")
say(f"有效授权：{assigns_after}")
say(f"有效阈值：{limits_after}")
left = [(i.entity_label, i.entity_name) for i in recycle.list_recycle_items(session)]
say(f"恢复后回收站剩余：{left}")
restore_logs = query(
    db_path, "select entity_id, mode, payload from deletion_log where mode='restore'"
)
say(f"恢复记录：{restore_logs}")
check(
    "2 回收站列出 → 恢复后节点/子树/授权/阈值全部回来且 deleted_at 为空",
    result.ok
    and names_after == ["卷绕产线振动监测", "卷绕产线", "隔膜过辊轴承", "CH1", "v_RMS"]
    and all(row[2] is None for row in deleted_flags)
    and [(row[0], row[1]) for row in assigns_after] == [(ids["channel"].id, "v_RMS")]
    and [(row[0], row[1]) for row in limits_after] == [(ids["metric"].id, "v_RMS")]
    and recycle.list_recycle_items(session) == []
    and len(restore_logs) == 1,
    f"恢复提示：{result.message}",
)

# ---------------------------------------------------------------- 验收 3
say("")
say("【验收 3】模板整树替换 → 旧节点逐条进 deletion_log（条数 = 被替换节点数）")
payload = tree.export_template(session, task.id)
alive_before = tree.list_nodes(session, task.id)
logs_before = log_count(db_path, "task_node")
max_id_before = query(db_path, "select coalesce(max(id), 0) from deletion_log")[0][0]
tree.import_template(session, task.id, payload)
logs_after = log_count(db_path, "task_node")
replaced_logs = query(
    db_path,
    "select entity_id, entity_name from deletion_log"
    " where entity_type='task_node' and mode='soft' and id > ? order by id",
    (max_id_before,),
)
say(f"替换前活节点：{[node.name for node in alive_before]}（{len(alive_before)} 个）")
say(f"留痕条数：替换前 {logs_before} → 替换后 {logs_after}（新增 {logs_after - logs_before}）")
say(f"本次新增逐条留痕：{replaced_logs}")
check(
    "3 模板整树替换逐节点留痕（新增条数 = 被替换节点数）",
    logs_after - logs_before == len(alive_before) == 5
    and {row[0] for row in replaced_logs} == {node.id for node in alive_before},
    f"新增 {logs_after - logs_before} 条 / 被替换 {len(alive_before)} 个节点",
)

# ---------------------------------------------------------------- 验收 4
say("")
say("【验收 4】保留期：8 天前留痕 + 软删行启动清理后消失；6 天前的不受影响")
fresh_task = tasks.create_task(session, name="保留期实验")
fresh_ids = build_line(session, fresh_task)
tree.soft_delete_node(session, fresh_ids["component"].id, operator="小智")
expired_log = [
    row
    for row in tasks.list_deletion_logs(session)
    if row.entity_type == "task_node" and row.entity_id == fresh_ids["component"].id
][0]
expired_node_ids = [fresh_ids["component"].id, fresh_ids["channel"].id, fresh_ids["metric"].id]


def backdate(log_id_: int, days: int) -> None:
    when = datetime.now() - timedelta(days=days)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "update deletion_log set created_at=?, expires_at=? where id=?",
            (when, when + timedelta(days=recycle.RECYCLE_RETENTION_DAYS), log_id_),
        )
        conn.execute(
            "update task_node set deleted_at=? where id in (?, ?, ?)",
            (when, *expired_node_ids),
        )
        conn.execute(
            "update metric_assign set deleted_at=? where node_id=?",
            (when, fresh_ids["channel"].id),
        )
        conn.execute(
            "update metric_limit set deleted_at=? where node_id=?",
            (when, fresh_ids["metric"].id),
        )


backdate(expired_log.id, 8)

another = tree.create_node(
    session, fresh_task.id, "device", "包装线", parent_id=tree.root_node(session, fresh_task.id).id
)
tree.soft_delete_node(session, another.id, operator="小智")
kept_log = [
    row
    for row in tasks.list_deletion_logs(session)
    if row.entity_type == "task_node" and row.entity_id == another.id
][0]
with sqlite3.connect(db_path) as conn:
    when6 = datetime.now() - timedelta(days=6)
    conn.execute(
        "update deletion_log set created_at=?, expires_at=? where id=?",
        (when6, when6 + timedelta(days=recycle.RECYCLE_RETENTION_DAYS), kept_log.id),
    )
    conn.execute("update task_node set deleted_at=? where id=?", (when6, another.id))

say(f"清理前：过期留痕 {log_count(db_path, 'task_node')} 条（含 8 天前 1 条 + 6 天前 1 条）")
say(f"过期留痕 id={expired_log.id}（8 天前）　保留留痕 id={kept_log.id}（6 天前）")
store_db.init_db(engine)  # 等价于应用启动（init_db → purge）
session.expunge_all()
expired_left = query(db_path, "select id from deletion_log where id=?", (expired_log.id,))
expired_nodes_left = query(
    db_path, "select id from task_node where id in (?, ?, ?)", tuple(expired_node_ids)
)
expired_assign_left = query(
    db_path, "select id from metric_assign where node_id=?", (fresh_ids["channel"].id,)
)
expired_limit_left = query(
    db_path, "select id from metric_limit where node_id=?", (fresh_ids["metric"].id,)
)
kept_left = query(db_path, "select id, created_at from deletion_log where id=?", (kept_log.id,))
kept_node_left = query(db_path, "select id, deleted_at from task_node where id=?", (another.id,))
say(
    f"清理后：过期留痕 {expired_left}　过期节点行 {expired_nodes_left}　"
    f"过期授权行 {expired_assign_left}　过期阈值行 {expired_limit_left}"
)
say(f"清理后：6 天前留痕 {kept_left}　6 天前节点行 {kept_node_left}")
check(
    "4 启动清理真删过期留痕与软删行，6 天前的不受影响",
    expired_left == []
    and expired_nodes_left == []
    and expired_assign_left == []
    and expired_limit_left == []
    and len(kept_left) == 1
    and len(kept_node_left) == 1,
    f"过期留痕剩余 {len(expired_left)} 条，6 天前留痕剩余 {len(kept_left)} 条",
)

# ---------------------------------------------------------------- 附加：页面
say("")
say("【附加】回收站页面（AppTest 真跑）显示节点路径 + 剩余保留天数")
page_db = WORK / "page" / "lion.db"
page_db.parent.mkdir(parents=True, exist_ok=True)
page_engine = store_db.make_engine(page_db)
store_db.init_db(page_engine)
with store_db.make_session_factory(page_engine)() as page_session:
    page_task = tasks.create_task(page_session, name="回收站页面实验")
    page_line = build_line(page_session, page_task)
    tree.soft_delete_node(page_session, page_line["component"].id, operator="小智")
os.environ["LION_DB"] = str(page_db)
import store_ctx  # noqa: E402

store_ctx._session_factory.clear()
at = AppTest.from_file(str(ROOT / "app" / "pages" / "recycle_bin.py"), default_timeout=120)
at.run()
frame = at.dataframe[0].value if at.dataframe else None
say(f"页面异常：{[exc.value for exc in at.exception] or '无'}")
say(f"页面表格列：{list(frame.columns) if frame is not None else '(无表格)'}")
say(f"页面表格首行：{frame.iloc[0].to_dict() if frame is not None else '(无)'}")
say(f"保留期文案出现：{'保留期' in ' '.join(str(i.value) for i in at.caption)}")
check(
    "附加 页面显示节点留痕（路径 + 剩余天数 + 保留期文案）",
    frame is not None
    and "剩余天数" in list(frame.columns)
    and "节点" in [str(value) for value in frame["类型"]]
    and str(frame["剩余天数"].iloc[0]).endswith("天"),
    f"{len(frame)} 行" if frame is not None else "无表格",
)
store_ctx._session_factory.clear()
os.environ.pop("LION_DB", None)

# ---------------------------------------------------------------- 真库未被改动
say("")
say("【附加】真库 data/lion.db 未被本次验收改动")
real_sha_after = sha(REAL_DB) if REAL_DB.is_file() else "(无真库)"
check(
    "附加 真库 sha256 前后一致（实验都在临时目录）",
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
say("  store/models.py         DeletionLog.payload / expires_at 两列")
say("  store/db.py             _add_deletion_log_recycle() 迁移；init_db → _purge_expired_recycle")
say("  store/tree.py           soft_delete_node(log_each_node=) 写子树快照；node_path_text()")
say("  store/tree_template.py  _create_tree() 整树替换逐节点留痕")
say("  store/recycle.py        RECYCLE_RETENTION_DAYS / retention_days / expiry_of")
say("                          _restore_node() 子树恢复；purge_expired() 保留期清理")
say("  app/pages/recycle_bin.py 类型 / 路径 / 删除时间 / 剩余天数 / 级联 / 操作人")
say("")
say("【未做项】")
say("  - 回收站「彻底删除」按钮仍未接界面（保持置灰），本卡只做保留期自动清理")
say("  - 回收站列表只对 task_node 显示剩余天数；数据集/任务留痕不参与 7 天清理（保持原行为）")
say("  - 过期清理只处理 task_node 留痕；metric_value 行保持原样（本项目 SQLite 未开外键强制）")

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
