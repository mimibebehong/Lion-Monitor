"""把「待重算」（``metric_value.stale=1``）的历史点按**当前参数**重算，分批可中断。

背景（复查 v2 问题 5 / 计划批次 B）：v_RMS 默认窗型由 hanning 改成 rect 后，启动迁移只会
把旧口径的点**标**成待重算，不会自动重算——24 万行重算会把启动卡死。页面「重算本节点」
适合单个节点；整库切换口径或大批量重算用这个脚本。

用法::

    .venv/bin/python scripts/recompute_stale.py --dry-run          # 只看有多少、分几批
    .venv/bin/python scripts/recompute_stale.py --limit 20000      # 每批最多 2 万行
    .venv/bin/python scripts/recompute_stale.py --task 3 --limit 5000

口径与页面按钮完全一致：都走 ``features.recompute.recompute_node``（同一份重算逻辑，
同一份 ``change_log`` 留痕）。Ctrl-C 会在当前「节点 × 指标」处理完后停下并打印小结。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import func, select  # noqa: E402

from lion_monitor.features import recompute  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store.models import Channel, Dataset, MetricValue  # noqa: E402

#: 每批最多处理的待重算行数（--limit 默认值）
DEFAULT_LIMIT = 20_000


class _Stopper:
    """Ctrl-C 只置标志位：不打断正在写库的那一组，处理完当前组再退出。"""

    def __init__(self) -> None:
        self.stopped = False

    def install(self) -> None:
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, _signum, _frame) -> None:  # noqa: ANN001
        if self.stopped:
            raise SystemExit("再次 Ctrl-C：立即退出（当前组可能只写了一半，下次重算会覆盖）")
        self.stopped = True
        print("\n收到 Ctrl-C：做完当前「节点 × 指标」就停（再按一次立即退出）", flush=True)


def _pending_groups(session, task_id: int | None):
    """待重算的「通道节点 × 指标码」清单（按待重算行数降序）。"""
    stmt = (
        select(
            MetricValue.node_id.label("node_id"),
            MetricValue.code.label("code"),
            Dataset.task_id.label("task_id"),
            func.count().label("rows"),
        )
        .select_from(MetricValue)
        .join(Channel, MetricValue.channel_id == Channel.id)
        .join(Dataset, Channel.dataset_id == Dataset.id)
        .where(
            Dataset.deleted_at.is_(None),
            Channel.deleted_at.is_(None),
            MetricValue.deleted_at.is_(None),
            MetricValue.stale == 1,
            MetricValue.node_id.is_not(None),
        )
        .group_by(MetricValue.node_id, MetricValue.code, Dataset.task_id)
        .order_by(func.count().desc(), MetricValue.node_id)
    )
    if task_id is not None:
        stmt = stmt.where(Dataset.task_id == task_id)
    return list(session.execute(stmt))


def _batches(groups, limit: int):
    """把组按「累计行数 ≤ limit」切批；单组超限也自成一批（不拆组）。"""
    batch: list = []
    rows = 0
    for group in groups:
        if batch and rows + int(group.rows) > limit:
            yield batch
            batch, rows = [], 0
        batch.append(group)
        rows += int(group.rows)
    if batch:
        yield batch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把待重算（stale=1）的指标值按当前参数重算")
    parser.add_argument(
        "--db", default=os.environ.get("LION_DB", ""), help="库文件（默认 data/lion.db）"
    )
    parser.add_argument("--task", type=int, default=None, help="只处理某个任务 id")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"每批最多重算多少行（默认 {DEFAULT_LIMIT}；按「节点×指标」成组，不拆组）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser() if args.db else store_db.resolve_db_path(ROOT, "")
    if not Path(db_path).exists():
        print(f"库文件不存在：{db_path}", file=sys.stderr)
        return 2

    engine = store_db.make_engine(db_path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)

    with factory() as session:
        groups = _pending_groups(session, args.task)
        total_rows = sum(int(group.rows) for group in groups)
        batches = list(_batches(groups, max(1, args.limit)))
        scope = f"任务 {args.task}" if args.task is not None else "全部任务"
        print(f"库：{db_path}")
        print(
            f"范围：{scope}；待重算 {total_rows} 行 / {len(groups)} 组"
            f"（节点×指标）/ {len(batches)} 批"
        )
        if not groups:
            print("没有待重算的点，收工。")
            return 0
        for index, batch in enumerate(batches, start=1):
            rows = sum(int(group.rows) for group in batch)
            names = "、".join(sorted({f"{group.node_id}:{group.code}" for group in batch}))
            print(f"  批次 {index}/{len(batches)}：{rows} 行（{names}）")
        if args.dry_run:
            print("--dry-run：只列计划，未写库。")
            return 0

    stopper = _Stopper()
    stopper.install()
    done_groups = 0
    done_rows = 0
    failed: list[str] = []
    for index, batch in enumerate(batches, start=1):
        if stopper.stopped:
            break
        rows = sum(int(group.rows) for group in batch)
        print(f"\n[批次 {index}/{len(batches)}] {rows} 行", flush=True)
        for group in batch:
            if stopper.stopped:
                break
            with factory() as session:
                result = recompute.recompute_node(
                    session, int(group.task_id), int(group.node_id), str(group.code)
                )
            done_groups += 1
            done_rows += result.recomputed
            status = "ok" if not result.problems else "问题：" + "；".join(result.problems)
            print(
                f"  任务 {group.task_id} · 节点 {result.node_name} · {group.code}："
                f"重算 {result.recomputed} 条（作废 {result.retired}），"
                f"待重算 {result.stale_before} → {result.stale_after}；{status}",
                flush=True,
            )
            if result.problems:
                failed.append(f"任务 {group.task_id} · {result.node_name} · {group.code}")

    with factory() as session:
        left = sum(
            metric_store.count_stale(session, task_id) for task_id in _task_ids(session, args.task)
        )
    print(
        f"\n完成：{done_groups} 组、写入/覆盖 {done_rows} 条；"
        f"当前仍待重算 {left} 行"
        + ("；有问题的组：" + "、".join(failed) if failed else "")
        + ("（已按 Ctrl-C 提前停止）" if stopper.stopped else "")
    )
    return 0 if not failed else 1


def _task_ids(session, task_id: int | None) -> list[int]:
    """要统计「待重算」余量的任务 id 列表（没给 --task 就覆盖全部未删数据集的所属任务）。"""
    if task_id is not None:
        return [task_id]
    rows = session.execute(
        select(Dataset.task_id).where(Dataset.deleted_at.is_(None)).distinct()
    ).scalars()
    return [int(value) for value in rows]


if __name__ == "__main__":
    raise SystemExit(main())
