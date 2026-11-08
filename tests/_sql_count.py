"""测试用：统计一段调用里 SQLAlchemy 实际发出的语句（证明「没把整串值拉回 Python」）。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, event

__all__ = ["counted_statements"]


@contextmanager
def counted_statements(engine: Engine) -> Iterator[list[str]]:
    """``with counted_statements(engine) as seen: ...`` —— 退出后 ``seen`` 是语句文本列表。"""
    seen: list[str] = []

    def _before_cursor_execute(_conn, _cursor, statement, _params, _context, _many) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)
