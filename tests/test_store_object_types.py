"""对象类型扩充：齿轮 / 轴 / 行星齿轮（2026-11-02 要求）。

同时钉住一件事：对象类型只能有一份定义。历史上 ``store.objects`` 和
``store.object_registry`` 各抄了一份 ``OBJECT_TYPES``，改一处漏一处就会「下拉里能选、
保存时报类型不合法」。
"""

from __future__ import annotations

import pytest

from lion_monitor.store import object_registry as reg
from lion_monitor.store import objects as legacy
from lion_monitor.store import tasks
from lion_monitor.store.db import init_db, make_engine, make_session_factory

NEW_TYPES = ("齿轮", "轴", "行星齿轮")
OLD_TYPES = ("轴承", "电机", "气缸", "辊", "泵", "风机")


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="卷绕产线振动监测")


def test_object_types_contain_new_and_old():
    assert set(NEW_TYPES) | set(OLD_TYPES) == set(reg.OBJECT_TYPES)
    # 原有顺序不动，新类型追加在后（下拉里老用户的位置不变）
    assert reg.OBJECT_TYPES[: len(OLD_TYPES)] == OLD_TYPES


def test_legacy_objects_module_shares_one_definition():
    assert legacy.OBJECT_TYPES == reg.OBJECT_TYPES


@pytest.mark.parametrize("kind", NEW_TYPES)
def test_create_object_accepts_new_type(session, task, kind):
    row = reg.create_object(session, task.id, f"对象-{kind}", kind)

    assert row.obj_type == kind
    assert [r.obj_type for r in reg.list_objects(session, task.id)] == [kind]


def test_update_object_switches_to_new_type(session, task):
    row = reg.create_object(session, task.id, "隔膜过辊轴承", "轴承")

    updated = reg.update_object(session, row.id, obj_type="行星齿轮")

    assert updated.obj_type == "行星齿轮"


def test_no_other_module_copies_the_type_list():
    """类型清单只能存在于 object_registry：别处再抄一份就会出现「能选不能存」。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in [*root.glob("src/**/*.py"), *root.glob("app/**/*.py")]:
        if path.name == "object_registry.py":
            continue
        if '"轴承", "电机", "气缸"' in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
