"""P6-2 验收实跑：全库备份（T2）—— 备份 / 保留策略 / 迁移前自动备份 + 失败中止 / 页面入口。

跑法（项目根目录）::

    .venv/bin/python scripts/verify_p6_2_backup.py

只写临时目录（``~/workspace/temp/<日期>/p6-2/``），从 ``data/lion.db`` 复制副本做实验，
不碰真库；证据同时写进 ``reports/2026-11-02-p6-2-verification.txt``。
任一条不通过即非零退出。
"""

from __future__ import annotations

import hashlib
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

from lion_monitor.store import backup as store_backup  # noqa: E402
from lion_monitor.store.db import init_db, make_engine, pending_migrations  # noqa: E402

PY = sys.executable
REAL_DB = ROOT / "data" / "lion.db"
WORK = Path.home() / "workspace" / "temp" / datetime.now().strftime("%Y-%m-%d") / "p6-2"
OUT = ROOT / "reports" / "2026-11-02-p6-2-verification.txt"
CLI = ROOT / "scripts" / "backup_db.py"

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


def columns(db: Path, table: str) -> list[str]:
    with sqlite3.connect(db) as conn:
        return [row[1] for row in conn.execute(f"pragma table_info({table})")]


def count(db: Path, table: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(f"select count(*) from {table}").fetchone()[0]


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PY, str(CLI), *args], capture_output=True, text=True, cwd=ROOT, check=False
    )


say("=" * 78)
say("P6-2 验收实跑：全库备份 + 迁移前自动备份（T2）")
say(f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}　项目：{ROOT}")
say("=" * 78)

if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)

say("")
say("【准备】从真库 data/lion.db 复制副本（真库只读，实验都在副本上做）")
real_sha_before = sha(REAL_DB)
say(f"真库 sha256 = {real_sha_before[:32]}…　大小 {REAL_DB.stat().st_size} 字节")
say(f"真库 dataset 行数 = {count(REAL_DB, 'dataset')}")
say(f"真库 metric_value 列 = {', '.join(columns(REAL_DB, 'metric_value'))}")

base = WORK / "src" / "lion.db"
base.parent.mkdir(parents=True)
shutil.copy2(REAL_DB, base)

# ---------------------------------------------------------------- 1 备份 + 行数一致
say("")
say("【验收 1】跑 scripts/backup_db.py → 生成文件；备份库 dataset 行数与真库一致")
bk1 = WORK / "bk1"
result = run_cli("--db", str(base), "--backup-dir", str(bk1), "--keep", "10")
say(f"$ .venv/bin/python scripts/backup_db.py --db {base} --backup-dir {bk1} --keep 10")
say(f"exit={result.returncode}")
say(result.stdout.rstrip() or "(stdout 为空)")
if result.stderr.strip():
    say(f"stderr: {result.stderr.strip()}")
files1 = sorted(bk1.glob("lion-*.db"))
src_rows = count(base, "dataset")
bk_rows = count(files1[0], "dataset") if files1 else -1
integrity = "ok"
if files1:
    with sqlite3.connect(files1[0]) as conn:
        integrity = conn.execute("pragma integrity_check").fetchone()[0]
check(
    "1 备份文件生成且能独立读出行数一致",
    len(files1) == 1 and src_rows == bk_rows and integrity == "ok",
    f"文件 {files1[0].name if files1 else '无'}　"
    f"大小 {files1[0].stat().st_size if files1 else 0} 字节　"
    f"dataset {src_rows} → {bk_rows}　integrity_check={integrity}",
)
say(
    "  等价核对命令（本机没装 sqlite3 命令行，用 python 一条命令等价）："
    f"python -c \"import sqlite3;print(sqlite3.connect(r'{files1[0] if files1 else ''}')"
    ".execute('select count(*) from dataset').fetchone())\""
)

# ---------------------------------------------------------------- 2 保留策略
say("")
say("【验收 2】连续跑 12 次（--keep 3）→ 只剩 3 个文件，且是最新 3 份")
bk2 = WORK / "bk2"
made: list[Path] = []
for _ in range(12):
    ran = run_cli("--db", str(base), "--backup-dir", str(bk2), "--keep", "3")
    assert ran.returncode == 0, ran.stderr
    made.append(Path(ran.stdout.splitlines()[0].split("：", 1)[1].strip()))
left = sorted(bk2.glob("lion-*.db"))
say(f"12 次备份生成的文件名（按时间）：{', '.join(path.name for path in made)}")
say(f"保留后目录里剩：{', '.join(path.name for path in left)}")
check(
    "2 保留策略只留最新 3 份",
    len(left) == 3 and set(left) == set(made[-3:]),
    f"目录文件数 {len(left)}；最新一份 = {made[-1].name}",
)

# ---------------------------------------------------------------- 3 迁移前自动备份
say("")
say("【验收 3a】模拟迁移：副本跑 init_db（缺 effective_params_json 列 → 需要迁移）→ 迁移前自动备份")
mig = WORK / "mig"
mig.mkdir()
mig_db = mig / "lion.db"
shutil.copy2(REAL_DB, mig_db)
before_cols = columns(mig_db, "metric_value")
say(f"迁移前列（metric_value）：{', '.join(before_cols)}")
engine = make_engine(mig_db)
say(f"pending_migrations = {pending_migrations(engine)}")
init_db(engine)
engine.dispose()
mig_bk = sorted((mig / "backups").glob("lion-*.db"))
after_cols = columns(mig_db, "metric_value")
bk_cols = columns(mig_bk[0], "metric_value") if mig_bk else []
say(f"迁移后列（metric_value）：{', '.join(after_cols)}")
say(f"备份文件里的列（迁移前结构）：{', '.join(bk_cols)}")
check(
    "3a 迁移前生成备份，且备份里是迁移前结构",
    len(mig_bk) == 1
    and "effective_params_json" in after_cols
    and "effective_params_json" not in bk_cols,
    f"备份 {mig_bk[0].name if mig_bk else '无'}　"
    f"备份大小 {mig_bk[0].stat().st_size if mig_bk else 0} 字节",
)

say("")
say("【验收 3b】把备份目录设成不可写（指向一个普通文件）→ 迁移中止，库结构未被修改")
abort = WORK / "abort"
abort.mkdir()
abort_db = abort / "lion.db"
shutil.copy2(REAL_DB, abort_db)
blocked = abort / "blocked-as-file"
blocked.write_text("这不是目录", encoding="utf-8")
old_env = os.environ.get("LION_BACKUP_DIR")
os.environ["LION_BACKUP_DIR"] = str(blocked)
error_text = ""
try:
    init_db(make_engine(abort_db))
except store_backup.BackupError as exc:
    error_text = str(exc)
finally:
    if old_env is None:
        os.environ.pop("LION_BACKUP_DIR", None)
    else:
        os.environ["LION_BACKUP_DIR"] = old_env
abort_cols = columns(abort_db, "metric_value")
say(f"报错信息：{error_text or '(没有抛错)'}")
say(f"中止后列（metric_value）：{', '.join(abort_cols)}")
added = "是（不该）" if "effective_params_json" in abort_cols else "否"
check(
    "3b 备份失败 → 迁移中止且结构未改动",
    "迁移中止" in error_text and "effective_params_json" not in abort_cols,
    f"effective_params_json 是否被加上：{added}",
)

# ---------------------------------------------------------------- 4 页面入口
say("")
say("【验收 4】页面「立即备份数据库」点击后真产出文件（AppTest）")
page_db = WORK / "page" / "lion.db"
page_db.parent.mkdir(parents=True)
shutil.copy2(REAL_DB, page_db)
os.environ["LION_DB"] = str(page_db)
os.environ.pop("LION_BACKUP_DIR", None)
import store_ctx  # noqa: E402

store_ctx._session_factory.clear()
at = AppTest.from_file(str(ROOT / "app" / "pages" / "backup.py"))
at.run()
labels = [button.label for button in at.button]
say(f"页面按钮：{labels}")
say(f"点击前备份目录存在：{(page_db.parent / 'backups').exists()}")
at.button(key="backup_now").click().run()
page_bk = sorted((page_db.parent / "backups").glob("lion-*.db"))
success = " | ".join(str(item.value) for item in at.success)
say(f"页面提示：{success or '(无)'}")
check(
    "4 页面点击后生成备份文件（不是只显示文案）",
    not at.exception and len(page_bk) == 1 and page_bk[0].name in success,
    f"文件 {page_bk[0].name if page_bk else '无'}　"
    f"大小 {page_bk[0].stat().st_size if page_bk else 0} 字节",
)

# ---------------------------------------------------------------- 真库未被改动
say("")
say("【附加】真库 data/lion.db 未被本次验收改动")
real_sha_after = sha(REAL_DB)
check(
    "附加 真库 sha256 前后一致（实验都在副本上）",
    real_sha_before == real_sha_after,
    f"前后 sha256 {real_sha_before[:16]}… / {real_sha_after[:16]}…",
)

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
