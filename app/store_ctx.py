"""界面对接存储层：数据库会话、建表、当前测试任务。

页面统一用法::

    import store_ctx

    with store_ctx.open_session() as session:
        rows = tasks.list_tasks(session)

写操作在 ``with`` 块内完成即提交，异常自动回滚。
库文件默认 ``<工程根>/data/lion.db``，可用环境变量 ``LION_DB`` 覆盖。

空库**不再自动播种示例任务**（G7 / Q5）：现场库不能被假数据污染。要示例树时
由用户在健康树页显式点「恢复内置示例树」（该按钮调 ``store.bootstrap``）。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from lion_monitor.store import tasks  # noqa: E402
from lion_monitor.store.db import (  # noqa: E402
    init_db,
    make_engine,
    make_session_factory,
    resolve_db_path,
    session_scope,
)

__all__ = [
    "TASK_KEY",
    "current_task",
    "db_path",
    "open_session",
    "set_current_task",
    "task_options",
]

#: 当前任务在 session_state 里的键
TASK_KEY = "current_task_id"


def db_path() -> Path:
    """库文件位置（环境变量 LION_DB 优先）。"""
    return resolve_db_path(PROJECT_ROOT, os.environ.get("LION_DB", ""))


@st.cache_resource(show_spinner=False)
def _session_factory() -> sessionmaker[Session]:
    """全局唯一的会话工厂：只建表，不播种（Q5：空库保持 0 任务）。"""
    engine = make_engine(db_path())
    init_db(engine)
    return make_session_factory(engine)


@contextmanager
def open_session() -> Iterator[Session]:
    """取一个会话，块内异常回滚。"""
    with session_scope(_session_factory()) as session:
        yield session


def task_options() -> Sequence[tasks.TaskRow]:
    """可选任务列表（新的在前，不含已删除）。"""
    with open_session() as session:
        return tasks.list_tasks(session)


def current_task() -> tasks.TaskRow | None:
    """当前任务：session_state 里指定的优先，否则取最新；空库返回 None。"""
    rows = task_options()
    picked = tasks.pick_task(rows, st.session_state.get(TASK_KEY))
    if picked is not None:
        st.session_state[TASK_KEY] = picked.id
    return picked


def set_current_task(task_id: int) -> None:
    """切换当前任务。"""
    st.session_state[TASK_KEY] = task_id
