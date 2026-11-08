"""待办里的「幅值超限」按「监测对象 × 通道 × 指标」聚合（马哥 2026-11-04）。

马哥给的格式：

```
🔴 幅值超限  监测对象 监测通道 监测指标 超限等级 超限时间 超限点数   [看趋势]
⚪ 阈值未设  11 个指标节点还没有生效阈值                            [阈值设置]
```

所以一条待办 = **一个（监测对象, 通道, 指标）组合**，
展示：等级/时间取该组合**最新**那条，超限点数取该组合**累计条数**。
"""

from __future__ import annotations

import pytest

from lion_monitor.features import overview
from lion_monitor.store import datasets, limits, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.metrics import NodeValue

STAMPS = [f"2026-11-01 10:0{i}:00" for i in range(4)]


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "ot.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def built(session, tmp_path):
    """卷绕产线 → 两个对象（各一个通道），每通道 1 个指标 v_RMS。"""
    task = tasks.create_task(session, name="待办聚合")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    out = {"task": task, "objs": {}}
    folder = tmp_path / "ds"
    folder.mkdir()
    ds = datasets.create_dataset(session, task.id, "批次", folder, "2026-11-01 09:00:00")

    for obj_name, ch_name, values in (
        ("隔膜过辊轴承", "CH1", (1.0, 3.0, 6.0, 7.0)),  # warn 2.8 / alert 4.5 → 3 点越限
        ("移栽电机", "CH4", (1.0, 1.0, 1.0, 1.0)),  # 不越限
    ):
        comp = tree.create_node(session, task.id, "component", obj_name, parent_id=device.id)
        ch = tree.create_node(session, task.id, "channel", ch_name, parent_id=comp.id)
        tree.create_node(
            session, task.id, "metric", "v_RMS", parent_id=ch.id, attrs={"code": "v_RMS"}
        )
        limits.set_limit(session, task.id, ch.id, "v_RMS", warn=2.8, alert=4.5)
        file_row = datasets.add_file(
            session, ds.id, folder / f"{ch_name}.dat", ch_name, 32000.0, 64000
        )
        for stamp, value in zip(STAMPS, values, strict=True):
            metric_store.upsert_node_values(
                session,
                file_row.id,
                stamp,
                [NodeValue(node_id=ch.id, code="v_RMS", value=value, unit="mm/s")],
            )
        out["objs"][obj_name] = {
            "comp": comp.id,
            "channel": ch.id,
            "file": file_row.id,
            "values": values,
        }
    return out


def test_over_record_carries_object_name(session, built):
    """每条越限记录要能说清属于哪个监测对象（待办要显示监测对象）。"""
    records = overview.over_records(session, built["task"].id)

    assert records, "应该有越限记录"
    assert {row.object_name for row in records} == {"隔膜过辊轴承"}


def test_over_todos_group_by_object_channel_metric(session, built):
    """同一（对象, 通道, 指标）的多个越限点合成一条待办，带累计点数。"""
    summary = overview.task_overview(session, built["task"].id)

    todos = summary.over_todos
    assert len(todos) == 1, todos
    todo = todos[0]
    assert todo.object_name == "隔膜过辊轴承"
    assert todo.channel == "CH1"
    assert todo.code == "v_RMS"
    assert todo.points == 3  # 6.0(warn) / 7.0(alert) / 3.0(warn) 都越限
    assert todo.level == "alert"  # 最新一条（7.0）是严重
    assert todo.computed_at == STAMPS[3]


def test_over_todos_level_follows_latest_point(session, built):
    """最新点只是警告时，等级要跟着最新点走（不是最严重那条）。"""
    metric_store.upsert_node_values(
        session,
        built["objs"]["隔膜过辊轴承"]["file"],  # Channel 表行 id
        "2026-11-01 10:09:00",
        [
            NodeValue(
                node_id=built["objs"]["隔膜过辊轴承"]["channel"],
                code="v_RMS",
                value=3.0,  # warn 区间
                unit="mm/s",
            )
        ],
    )

    todos = overview.task_overview(session, built["task"].id).over_todos

    assert todos[0].points == 4
    assert todos[0].level == "warn"
    assert todos[0].computed_at == "2026-11-01 10:09:00"


def test_home_todo_text_uses_new_fields():
    """工作台文案按马哥示例：幅值超限 + 对象/通道/指标/等级/时间/点数。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "pages" / "home.py").read_text(
        encoding="utf-8"
    )

    assert "振动超限" in src
    assert "幅值超限" not in src  # 马哥 2026-11-04 改成「振动超限」
    for field in ("监测对象", "监测通道", "监测指标", "超限等级", "超限时间", "超限点数"):
        assert field in src, f"缺少字段 {field}"


def test_todo_rows_have_no_underline():
    """马哥 2026-11-04：待办里的提示不要下划线（Streamlit 的 markdown a 自带下划线）。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "pages" / "home.py").read_text(
        encoding="utf-8"
    )

    assert "text-decoration:none !important" in src
    assert '[data-testid="stMarkdownContainer"] a.todo-row{text-decoration:none !important}' in src


def test_todo_level_text_is_not_colored():
    """马哥 2026-11-04：待办里的「告警」不再染红/橙，恢复常态色。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "pages" / "home.py").read_text(
        encoding="utf-8"
    )

    assert "level_color" not in src, "待办等级仍在按状态配色"
    assert "style='color:{level_color}" not in src
