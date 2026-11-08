"""监测对象页：删除二合一 + 通道区去重 + 同类功能同排（马哥 2026-11-03）。

三条要求：

1. 选中对象时，右侧「确认删除该对象（勾选）」与「删除对象（按钮）」二合一 ——
   改为与任务页/数据集页/重算一致的**二次点击确认**：第一次变红「确认删除？」不改数据，
   第二次才执行；
2. 通道设置里「通道展示」（纯 dataframe）与「管理已有通道」重复 —— 只保留管理功能块，
   并把它放到通道展示原来的位置（新增通道之上）；
3. 同一功能块的保存 / 删除放到同一排、真等宽。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "pages" / "objects.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import channel_reg, object_registry, tasks  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402


class Env:
    def __init__(self, tmp_path, monkeypatch):
        path = tmp_path / "obj.db"
        monkeypatch.setenv("LION_DB", str(path))
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        with self.session() as session:
            self.task = tasks.create_task(session, name="对象页动作")
            self.bearing = object_registry.create_object(
                session, self.task.id, "隔膜过辊轴承", "轴承"
            )
            self.motor = object_registry.create_object(
                session, self.task.id, "入片牵引电机", "电机"
            )
            channel_reg.create_channel(session, self.task.id, self.bearing.id, "CH1", "驱动端")
            channel_reg.create_channel(session, self.task.id, self.bearing.id, "CH2", "非驱动端")

    def session(self):
        return store_db.session_scope(store_db.make_session_factory(self.engine))

    def objects(self):
        with self.session() as session:
            return object_registry.list_objects(session, self.task.id)

    def channels(self):
        with self.session() as session:
            return channel_reg.list_channels(session, self.task.id)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    instance = Env(tmp_path, monkeypatch)
    import store_ctx

    store_ctx._session_factory.clear()
    yield instance
    store_ctx._session_factory.clear()


def _app(picked: int | None = 0):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(PAGE), default_timeout=120).run()
    if picked is not None:
        at.session_state["object_table"] = {"selection": {"rows": [picked], "columns": []}}
        at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


# ------------------------------------------------------------------ 1.1 二次点击删除


def test_delete_object_has_no_confirm_checkbox(env):
    """勾选框要去掉，改成按钮自己确认。"""
    at = _app()

    boxes = [box.label for box in at.checkbox]
    assert "确认删除该对象" not in boxes, boxes


def test_delete_object_needs_two_clicks(env):
    """第一次点只进待确认，第二次才真删。"""
    at = _app()
    assert len(env.objects()) == 2

    first = [b for b in at.button if b.label == "删除对象"]
    assert first, [b.label for b in at.button]
    first[0].click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    assert len(env.objects()) == 2, "第一次点击不该真的删除"
    armed = [b for b in at.button if "确认删除" in b.label]
    assert armed, [b.label for b in at.button]

    armed[0].click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert len(env.objects()) == 1


def test_delete_object_uses_danger_style_when_armed(env):
    """待确认时按钮走统一的红色危险样式（与任务页/数据集页一致）。"""
    at = _app()
    [b for b in at.button if b.label == "删除对象"][0].click().run()

    html = "\n".join(block.value for block in at.markdown)
    assert "del_obj_btn_" in html
    assert "d93a3a" in html.lower()


# ------------------------------------------------------------------ 1.2 通道区去重


def test_channel_section_has_no_plain_display_table(env):
    """通道设置里不再有纯展示的通道表格，只留左侧对象台账这一张表。"""
    at = _app()

    assert len(at.dataframe) == 1, f"页面出现了多余表格：{len(at.dataframe)}"


def test_channel_manager_sits_where_display_table_was(env):
    """管理已有通道要排在新增通道之前（占据原通道展示的位置）。"""
    src = PAGE.read_text(encoding="utf-8")
    i_section = src.index("📡 通道设置")
    i_manage = src.index("管理已有通道")
    i_add = src.index("新增通道")

    assert i_section < i_manage < i_add, (i_section, i_manage, i_add)


# ------------------------------------------------------------------ 1.3 保存/删除同排


def _row_window(src: str, cols_line: str, end_marker: str) -> str:
    """取「A, B = st.columns(2…)」之后到 end_marker 之前的代码窗口。"""
    match = re.search(cols_line, src)
    assert match, f"找不到 {cols_line}"
    end = src.index(end_marker, match.end())
    return src[match.end() : end]


def test_object_save_and_delete_share_a_row():
    """对象详情：保存修改 与 删除对象 同一排、真等宽。"""
    src = PAGE.read_text(encoding="utf-8")
    window = _row_window(src, r"c_save, c_del = st\.columns\(2[^\n]*\)\n", "📡 通道设置")

    assert 'c_save.button("保存修改"' in window
    assert 'width="stretch"' in window
    assert "with c_del:" in window
    assert '"确认删除？" if obj_armed else "删除对象"' in window


def test_channel_save_and_delete_share_a_row():
    """通道管理：保存通道 与 删除通道 同一排、真等宽。"""
    src = PAGE.read_text(encoding="utf-8")
    window = _row_window(src, r"c_ch_save, c_ch_del = st\.columns\(2[^\n]*\)\n", "新增通道")

    assert 'c_ch_save.button("保存通道"' in window
    assert "with c_ch_del:" in window
    assert '"确认删除？" if ch_armed else "删除通道"' in window


def test_channel_delete_needs_two_clicks(env):
    """通道删除同样二合一（不再有勾选框）。"""
    at = _app()

    boxes = [box.label for box in at.checkbox]
    assert not [label for label in boxes if "确认删除通道" in label], boxes

    first = [b for b in at.button if b.label == "删除通道"]
    assert first, [b.label for b in at.button]
    first[0].click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert len(env.channels()) == 2, "第一次点击不该真的删除"

    armed = [b for b in at.button if "确认删除" in b.label and "通道" not in b.label]
    assert armed, [b.label for b in at.button]
    armed[0].click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert len(env.channels()) == 1

    # 删掉通道后下拉选项变少，页面不能因为旧选中值崩掉
    assert [exc.value for exc in at.exception] == []


# ------------------------------------------------------------------ 新增通道走传感器台账下拉


def _with_tree_and_sensors(env):
    """装配「隔膜过辊轴承」与「张力气缸」到健康树，并造两台传感器。

    - CH4：自由传感器（台账有、树里没装）
    - CH5：已作为通道装配在「张力气缸」下（一台通道可再到别的对象下装配）
    """
    from lion_monitor.store import sensors, tree

    with env.session() as session:
        root = tree.root_node(session, env.task.id)
        device = tree.create_node(session, env.task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(
            session,
            env.task.id,
            "component",
            env.bearing.name,
            parent_id=device.id,
            ref_type="monitor_object",
            ref_id=env.bearing.id,
        )
        sensors.create_sensor(
            session, env.task.id, node_id=None, model="IEPE 500mV/g", channel_code="CH4"
        )
        other = object_registry.create_object(session, env.task.id, "张力气缸", "气缸")
        other_node = tree.create_node(
            session,
            env.task.id,
            "component",
            other.name,
            parent_id=device.id,
            ref_type="monitor_object",
            ref_id=other.id,
        )
        reg = channel_reg.create_channel(session, env.task.id, other.id, "CH5", "垂直")
        node5 = tree.create_node(
            session,
            env.task.id,
            "channel",
            "CH5",
            parent_id=other_node.id,
            ref_type="channel_reg",
            ref_id=reg.id,
        )
        sensors.create_sensor(
            session, env.task.id, node_id=node5.id, model="IEPE 500mV/g", channel_code="CH5"
        )
        env.comp_node_id = comp.id
    return env


def test_add_channel_picks_from_sensor_ledger():
    """新增通道不再手填通道号：改成「已有传感器」下拉。"""
    src = PAGE.read_text(encoding="utf-8")

    assert '"已有传感器"' in src
    assert '"新通道号"' not in src
    assert '"新通道备注"' not in src


def test_sensor_dropdown_lists_sensors_not_yet_under_object(env):
    """候选 = 传感器台账里、还没装到当前对象下的那些；标签不写归属对象名。"""
    _with_tree_and_sensors(env)
    at = _app()

    box = [item for item in at.selectbox if item.label == "已有传感器"]
    assert box, [item.label for item in at.selectbox]
    options = list(box[0].options)
    assert any("CH4" in opt for opt in options), options
    assert any("CH5" in opt for opt in options), options
    assert not any("张力气缸" in opt for opt in options), f"下拉标签不该写归属：{options}"


def test_add_channel_assembles_channel_under_object(env):
    """选中 CH4 点新增 → 通道台账有 CH4 且归属本对象，树里多一个 CH4 通道节点。"""
    from lion_monitor.store import tree

    _with_tree_and_sensors(env)
    at = _app()

    box = [item for item in at.selectbox if item.label == "已有传感器"][0]
    box.select(next(opt for opt in box.options if "CH4" in opt)).run()
    [b for b in at.button if b.label == "新增通道"][0].click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    rows = {row.code: row.object_id for row in env.channels()}
    assert rows.get("CH4") == env.bearing.id, rows

    with env.session() as session:
        nodes = tree.list_nodes(session, env.task.id)
    under = [n.name for n in nodes if n.parent_id == env.comp_node_id and n.node_type == "channel"]
    assert "CH4" in under, under

    # 装完之后本对象的「管理已有通道」里要能看到它（否则用户以为没生效）
    manage = [item for item in at.selectbox if item.label == "管理已有通道"][0]
    assert any("CH4" in opt for opt in manage.options), list(manage.options)


def test_add_channel_supports_channel_shared_by_two_objects(env):
    """CH5 已在「张力气缸」下装配，仍可再到本对象下装配（一台通道监测多个对象）。"""
    from lion_monitor.store import tree

    _with_tree_and_sensors(env)
    at = _app()

    box = [item for item in at.selectbox if item.label == "已有传感器"][0]
    box.select(next(opt for opt in box.options if "CH5" in opt)).run()
    [b for b in at.button if b.label == "新增通道"][0].click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    with env.session() as session:
        nodes = tree.list_nodes(session, env.task.id)
    ch5_parents = {n.parent_id for n in nodes if n.node_type == "channel" and n.name == "CH5"}
    assert env.comp_node_id in ch5_parents, ch5_parents
    assert len(ch5_parents) >= 2, ch5_parents


def test_add_channel_requires_object_assembled(env):
    """对象没装配到健康树时不能挂通道：按钮禁用并说明原因。"""
    from lion_monitor.store import sensors

    with env.session() as session:
        sensors.create_sensor(
            session, env.task.id, node_id=None, model="IEPE 500mV/g", channel_code="CH4"
        )
    at = _app()

    add = [b for b in at.button if b.label == "新增通道"]
    assert add and add[0].disabled, [b.label for b in at.button]
    assert any("装配" in str(item.value) for item in at.caption)
