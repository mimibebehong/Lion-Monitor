"""保存与删除收在同一处（2026-11-02：同类功能不要分散）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import (  # noqa: E402
    channel_reg,
    datasets,
    object_registry,
    sensors,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402


def _make_env(tmp_path, monkeypatch, name: str):
    path = tmp_path / f"{name}.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    import store_ctx

    store_ctx._session_factory.clear()
    return factory


def _app(page: str, key: str):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app" / "pages" / page), default_timeout=180).run()
    at.session_state[key] = {"selection": {"rows": [0], "columns": []}}
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _order(at, *labels):
    names = [button.label for button in at.button]
    positions = []
    for label in labels:
        assert label in names, f"没找到按钮 {label}；现有 {names}"
        positions.append(names.index(label))
    return positions


@pytest.fixture()
def sensors_env(tmp_path, monkeypatch):
    factory = _make_env(tmp_path, monkeypatch, "sensors_grouped")
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="传感器分组")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "辊压机", parent_id=device.id)
        row = channel_reg.create_channel(session, task.id, None, "CH1")
        channel = tree.create_node(
            session,
            task.id,
            "channel",
            row.code,
            parent_id=comp.id,
            ref_type="channel_reg",
            ref_id=row.id,
        )
        sensors.create_sensor(session, task.id, channel.id, model="IEPE 500mV/g")
    yield factory
    import store_ctx

    store_ctx._session_factory.clear()


@pytest.fixture()
def objects_env(tmp_path, monkeypatch):
    factory = _make_env(tmp_path, monkeypatch, "objects_grouped")
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="对象分组")
        obj = object_registry.create_object(session, task.id, "辊压机", "轴承")
        channel_reg.create_channel(session, task.id, obj.id, "CH1")
    yield factory
    import store_ctx

    store_ctx._session_factory.clear()


@pytest.fixture()
def datasets_env(tmp_path, monkeypatch):
    factory = _make_env(tmp_path, monkeypatch, "datasets_grouped")
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="数据集分组")
        datasets.create_dataset(
            session,
            task.id,
            name="第一批",
            source_dir=str(tmp_path / "raw"),
            collected_at="2026-11-02 09:00:00",
        )
    yield factory
    import store_ctx

    store_ctx._session_factory.clear()


def test_sensors_actions_grouped(sensors_env):
    """2026-11-04：保存修改放到最下面，与「删除这台传感器」同一排。"""
    at = _app("sensors.py", "sensor_table")
    owner, save, delete = _order(at, "保存归属", "保存修改", "删除这台传感器")
    assert owner < save < delete, (owner, save, delete)

    src = (ROOT / "app" / "pages" / "sensors.py").read_text(encoding="utf-8")
    window = src[src.index("save_col, del_col = st.columns(") :]
    window = window[: window.index("b1, b2 = st.columns(2)")]
    assert 'save_col.button("保存修改"' in window
    assert "with del_col:" in window
    assert 'width="stretch"' in window


def test_objects_actions_grouped(objects_env):
    at = _app("objects.py", "object_table")
    save, delete, channel_save = _order(at, "保存修改", "删除对象", "保存通道")
    assert save < delete < channel_save  # 对象的删除紧跟在保存后面，别沉到通道区下面


def test_datasets_actions_grouped(datasets_env):
    at = _app("datasets.py", "dataset_table")
    save, delete = _order(at, "保存修改", "删除数据集")
    assert save < delete


# ---------------------------------------------------------------- 指标配置页（2026-11-04）

from lion_monitor.store import metric_def  # noqa: E402


@pytest.fixture()
def metrics_env(tmp_path, monkeypatch):
    factory = _make_env(tmp_path, monkeypatch, "metrics_grouped")
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="指标分组")
        metric_def.create_metric(session, task.id, "v_RMS", unit="mm/s")
        metric_def.create_metric(session, task.id, "a_Peak", unit="g")
    yield factory
    import store_ctx

    store_ctx._session_factory.clear()


def test_metrics_save_and_remove_share_a_row(metrics_env):
    """2026-11-04：去掉勾选框，「从台账移除」与「保存修改」同一排，移除仍二次确认。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app" / "pages" / "metrics.py"), default_timeout=180).run()
    at.session_state["metric_table"] = {"selection": {"rows": [0], "columns": []}}
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    # 1) 勾选框没了
    assert not [box.label for box in at.checkbox if "确认从台账移除" in box.label], [
        box.label for box in at.checkbox
    ]

    # 2) 保存修改与从台账移除同一排、真等宽
    src = (ROOT / "app" / "pages" / "metrics.py").read_text(encoding="utf-8")
    window = src[src.index("save_col, del_col = st.columns(") :]
    window = window[: window.index("else:\n        with st.container(border=True)")]
    assert 'save_col.button("保存修改"' in window
    assert "with del_col:" in window
    assert 'width="stretch"' in window

    # 3) 第一次点只进待确认
    codes_before = _metric_codes(metrics_env)
    [b for b in at.button if b.label == "从台账移除"][0].click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert _metric_codes(metrics_env) == codes_before, "第一次点击不该真的移除"
    armed = [b for b in at.button if "确认移除" in b.label]
    assert armed, [b.label for b in at.button]

    # 4) 第二次才真删
    armed[0].click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert _metric_codes(metrics_env) == ["a_Peak"], _metric_codes(metrics_env)


def _metric_codes(factory) -> list[str]:
    with store_db.session_scope(factory) as session:
        return [row.code for row in metric_def.list_metrics(session, 1)]
