"""批次 B 探针 2：改定义默认值的 stale 口径（B-2）+ 判级跳过 stale（B-3）。

全部在**临时库**里构造，不碰 ``data/lion.db``。输出贴进
``reports/2026-11-04-batch-b-verification.txt``。

用法：``.venv/bin/python scripts/fix_b_probe_stale.py``
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.features import overview  # noqa: E402
from lion_monitor.features.health import node_health  # noqa: E402
from lion_monitor.store import (  # noqa: E402
    audit,
    datasets,
    limits,
    metric_assign,
    metric_def,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store.models import MetricValue  # noqa: E402

STAMP = "2026-11-04 10:00:00"


class World:
    """一个临时库 + 任务 + 树 + 一个数据集/通道文件，用来先后造两类数据。"""

    def __init__(self, path: Path) -> None:
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        self.factory = store_db.make_session_factory(self.engine)
        with self.session() as session:
            self.task = tasks.create_task(session, name="批次B探针", operator="马哥")
            metric_def.seed_defaults(session, self.task.id)
            root = tree.root_node(session, self.task.id)
            device = tree.create_node(
                session, self.task.id, "device", "卷绕产线", parent_id=root.id
            )
            comp = tree.create_node(
                session, self.task.id, "component", "轴承A", parent_id=device.id
            )
            self.channel = tree.create_node(
                session, self.task.id, "channel", "CH1", parent_id=comp.id
            )
            self.vrms = tree.create_node(
                session,
                self.task.id,
                "metric",
                "v_RMS",
                parent_id=self.channel.id,
                attrs={"code": "v_RMS"},
            )
            self.apk = tree.create_node(
                session,
                self.task.id,
                "metric",
                "a_Peak",
                parent_id=self.channel.id,
                attrs={"code": "a_Peak"},
            )
            folder = path.parent / f"{path.stem}_files"
            folder.mkdir(exist_ok=True)
            ds = datasets.create_dataset(
                session, self.task.id, "批次", folder, "2026-11-04 09:00:00"
            )
            self.file = datasets.add_file(
                session, ds.id, folder / "CH1_Task1.dat", "CH1", 32000.0, 64000
            )
            self.root = root.id
            self.device = device.id
            self.comp = comp.id
            #: B-2 里另外建的「跟随定义」通道节点与其授予行 id（probe_b2 填）
            self.ch2 = 0
            self.follower = 0
            session.commit()

    def session(self):
        return self.factory()

    def values(self, rows: list[tuple[float, int]], code: str = "v_RMS") -> None:
        """rows = [(值, stale), ...] 一分钟一个点。"""
        base = datetime.strptime(STAMP, "%Y-%m-%d %H:%M:%S")
        with self.session() as session:
            for index, (value, stale) in enumerate(rows):
                session.add(
                    MetricValue(
                        channel_id=self.file.id,
                        node_id=self.channel.id,
                        code=code,
                        value=value,
                        unit="mm/s",
                        level="unset",
                        computed_at=base + timedelta(minutes=index),
                        stale=stale,
                    )
                )
            session.commit()


def probe_b2(world: World) -> dict:
    """B-2：改定义默认值 → 未固化节点标 stale；改回不重复标；固化节点不动。"""
    out: dict[str, object] = {}
    with world.session() as session:
        unlocked = metric_assign.assign_metric(session, world.task.id, world.channel.id, "v_RMS")
        metric_assign.set_locked(session, unlocked.id, True)
        # 再建一个通道放「跟随定义」的授予
        comp2 = tree.create_node(
            session, world.task.id, "component", "轴承B", parent_id=world.device
        )
        ch2 = tree.create_node(session, world.task.id, "channel", "CH2", parent_id=comp2.id)
        tree.create_node(
            session, world.task.id, "metric", "v_RMS", parent_id=ch2.id, attrs={"code": "v_RMS"}
        )
        follower = metric_assign.assign_metric(session, world.task.id, ch2.id, "v_RMS")
        world.ch2 = ch2.id
        world.follower = follower.id
        session.commit()

    # 两条序列各 3 个点（都按当前口径写成 stale=0）
    world.values([(1.0, 0), (1.0, 0), (1.0, 0)])  # CH1（固化）
    with world.session() as session:
        base = datetime.strptime(STAMP, "%Y-%m-%d %H:%M:%S")
        for index in range(3):
            session.add(
                MetricValue(
                    channel_id=world.file.id,
                    node_id=world.ch2,
                    code="v_RMS",
                    value=1.0,
                    unit="mm/s",
                    level="unset",
                    computed_at=base + timedelta(minutes=index),
                    stale=0,
                )
            )
        session.commit()

    with world.session() as session:
        before = metric_store.count_stale(session, world.task.id)
        definition = metric_def.find_metric(session, world.task.id, "v_RMS")
        assert definition is not None
        updated = metric_def.update_metric(session, definition.id, defaults={"band_high": 300.0})
        after = metric_store.count_stale(session, world.task.id)
        follower_now = metric_assign.effective_params(session, world.follower)["band_high"]
        locked_now = metric_assign.effective_params(session, unlocked.id)["band_high"]
        changes = audit.list_changes(session, entity_type="metric_def", entity_id=definition.id)
        out["改默认值 band_high 1000→300"] = {
            "stale_改前": before,
            "stale_改后": after,
            "update_metric 返回 stale_marked": updated.stale_marked,
            "跟随定义节点生效 band_high": follower_now,
            "固化节点生效 band_high（应仍 1000）": locked_now,
            "change_log 字段": sorted({row.field for row in changes}),
        }

    with world.session() as session:
        definition = metric_def.find_metric(session, world.task.id, "v_RMS")
        assert definition is not None
        again = metric_def.update_metric(session, definition.id, defaults={"band_high": 1000.0})
        out["改回 band_high 300→1000"] = {
            "update_metric 返回 stale_marked（已标过，应 0）": again.stale_marked,
            "stale 总数（仍是 3 条，未重复标记）": metric_store.count_stale(session, world.task.id),
        }
    return out


def probe_b3(world: World) -> dict:
    """B-3：判级跳过 stale、over_count 不含 stale、待重算口径统一。"""
    with world.session() as session:
        limits.set_limit(session, world.task.id, world.channel.id, "v_RMS", 2.8, 4.5, ">")

    out: dict[str, object] = {}
    # 场景 1：全部 stale（且都越限）
    world.values([(9.9, 1), (9.9, 1)], code="v_RMS")
    with world.session() as session:
        item = node_health(session, world.task.id)[world.vrms.id]
        out["场景1 全部 stale+越限"] = {
            "state": item.state,
            "over_count": item.over_count,
            "stale_count": item.stale_count,
            "needs_recompute": item.needs_recompute,
            "latest_value": item.latest_value,
        }
    with world.session() as session:
        for row in session.query(MetricValue).filter(MetricValue.node_id == world.channel.id):
            session.delete(row)
        session.commit()

    # 场景 2：3 个 stale 越限 + 2 个有效越限
    world.values([(9.9, 0), (9.9, 0), (9.9, 1), (9.9, 1), (9.9, 1)])
    with world.session() as session:
        rows = session.query(MetricValue).filter(MetricValue.node_id == world.channel.id).all()
        for index, row in enumerate(rows):
            row.stale = 0 if index < 2 else 1
        session.commit()
        item = node_health(session, world.task.id)[world.vrms.id]
        trend_side = metric_store.count_stale(session, world.task.id)
        records = overview.over_records(session, world.task.id)
        out["场景2 3 stale + 2 有效超限"] = {
            "state": item.state,
            "over_count": item.over_count,
            "stale_count": item.stale_count,
            "健康树 stale 汇总（device）": node_health(session, world.task.id)[
                world.device
            ].stale_count,
            "趋势页 count_stale(task)": trend_side,
            "超限记录条数（不含 stale）": len(records),
        }
    return out


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fix_b_probe_") as tmp:
        base = Path(tmp)
        b2 = probe_b2(World(base / "b2.db"))
        b3 = probe_b3(World(base / "b3.db"))

    lines = ["=== B-2 定义默认值 → stale 探针（临时库）==="]
    for key, value in b2.items():
        lines.append(f"{key}:")
        for name, item in value.items():
            lines.append(f"  {name} = {item}")
    lines.append("")
    lines.append("=== B-3 判级跳过 stale 探针（临时库）===")
    for key, value in b3.items():
        lines.append(f"{key}:")
        for name, item in value.items():
            lines.append(f"  {name} = {item}")
    text = "\n".join(lines)
    print(text)
    target = ROOT / "reports" / "2026-11-04-batch-b-probe-stale.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    print(f"\n已写入 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
