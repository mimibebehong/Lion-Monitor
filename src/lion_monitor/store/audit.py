"""参数 / 阈值变更留痕（T3）：写入与查询集中在这里。

为什么单独一个模块：写留痕的地方有阈值、指标参数、指标定义默认值、节点转速四处，
页面与 store 层都调这里，**不在各处散写 change_log**——口径（哪些算变化、old/new 怎么
存、操作人取谁）只有一份。

存法：

- 一条留痕 = 一个字段的一次变化；``old == new``（数值按 float 比）不写
- 值一律按 JSON 存（``5`` 读回还是数字，``{"band_high": 500}`` 读回还是字典）
- 参数（dict）变化合成**一条**，old / new 是只含改动键的差异字典（缺的键记 ``null``）
- ``actor`` 取任务台账的 ``operator``（单人单机，谁开的测试任务就是谁）
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ChangeLog, TestTask

__all__ = [
    "DEFAULT_LIMIT",
    "ENTITY_TEXT",
    "ChangeRow",
    "actor_of",
    "list_changes",
    "record_change",
    "record_fields",
    "record_param_diff",
    "same_value",
    "value_text",
]

#: 实体类型 → 界面上的叫法
ENTITY_TEXT: dict[str, str] = {
    "metric_limit": "阈值",
    "metric_assign": "指标参数",
    "metric_def": "指标定义",
    "tree_node": "健康树节点",
    "metric_value": "指标值（重算）",
}

#: 页面默认最多显示多少条（倒序取最近 N 条）
DEFAULT_LIMIT = 200


@dataclass(frozen=True)
class ChangeRow:
    """一条已解码的留痕（``old_value`` / ``new_value`` 是 JSON 解出来的原值）。"""

    id: int
    entity_type: str
    entity_id: int
    field: str
    old_value: Any
    new_value: Any
    actor: str
    created_at: datetime
    note: str

    @property
    def entity_label(self) -> str:
        """实体中文名（认不出的类型原样给，不猜）。"""
        return ENTITY_TEXT.get(self.entity_type, self.entity_type)

    @property
    def entity_text(self) -> str:
        """界面上的实体列：``阈值 #12``。"""
        return f"{self.entity_label} #{self.entity_id}"


def same_value(left: Any, right: Any) -> bool:
    """数值按 float 比（1000 与 1000.0 算同一个值），其余按等值比。

    口径与 :func:`lion_monitor.params.diff_params` 一致：参数比的是「值变没变」，
    不是「类型变没变」。
    """
    numbers = (int, float)
    numeric = (
        isinstance(left, numbers)
        and isinstance(right, numbers)
        and not isinstance(left, bool)
        and not isinstance(right, bool)
    )
    return float(left) == float(right) if numeric else left == right


def value_text(value: Any) -> str:
    """界面展示：``None`` 写「（空）」，布尔写是/否，字典写紧凑 JSON。"""
    if value is None:
        return "（空）"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(raw: str | None) -> Any:
    text = raw or ""
    if text == "":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 历史/手改过的行不是合法 JSON：原样给出去，别让查询炸掉
        return text


def actor_of(session: Session, task_id: int | None) -> str:
    """留痕的操作人：取任务台账的 ``operator``（没有任务就留空）。"""
    if task_id is None:
        return ""
    task = session.get(TestTask, int(task_id))
    return (task.operator or "") if task is not None else ""


def record_change(
    session: Session,
    *,
    entity_type: str,
    entity_id: int,
    field: str,
    old_value: Any,
    new_value: Any,
    actor: str = "",
    note: str = "",
) -> bool:
    """记一条留痕；值没变（或实体 id 未知）返回 ``False``，不写行。

    不提交事务：与业务改动同一个事务提交（要么都进库，要么都不进）。
    """
    if not entity_id or same_value(old_value, new_value):
        return False
    session.add(
        ChangeLog(
            entity_type=entity_type,
            entity_id=int(entity_id),
            field=field,
            old_value=_dump(old_value),
            new_value=_dump(new_value),
            actor=actor,
            created_at=datetime.now(),
            note=note,
        )
    )
    return True


def record_fields(
    session: Session,
    *,
    entity_type: str,
    entity_id: int,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    actor: str = "",
    note: str = "",
) -> int:
    """逐字段比对 ``before`` / ``after``，只给真正变化的字段各记一条；返回条数。"""
    changed = 0
    for field in sorted(set(before) | set(after)):
        if record_change(
            session,
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            old_value=before.get(field),
            new_value=after.get(field),
            actor=actor,
            note=note,
        ):
            changed += 1
    return changed


def record_param_diff(
    session: Session,
    *,
    entity_type: str,
    entity_id: int,
    field: str,
    old_params: Mapping[str, Any] | None,
    new_params: Mapping[str, Any] | None,
    actor: str = "",
    note: str = "",
) -> bool:
    """参数字典变化：合成一条留痕，old / new 只含改动的键（缺值记 ``null``）。"""
    old = dict(old_params or {})
    new = dict(new_params or {})
    keys = sorted(key for key in set(old) | set(new) if not same_value(old.get(key), new.get(key)))
    if not keys:
        return False
    return record_change(
        session,
        entity_type=entity_type,
        entity_id=entity_id,
        field=field,
        old_value={key: old.get(key) for key in keys},
        new_value={key: new.get(key) for key in keys},
        actor=actor,
        note=note,
    )


def list_changes(
    session: Session,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[ChangeRow]:
    """留痕倒序（新的在前），可按实体类型 / 实体 id 过滤，最多 ``limit`` 条。"""
    stmt = select(ChangeLog)
    if entity_type:
        stmt = stmt.where(ChangeLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(ChangeLog.entity_id == entity_id)
    stmt = stmt.order_by(ChangeLog.created_at.desc(), ChangeLog.id.desc()).limit(limit)
    return [
        ChangeRow(
            id=model.id,
            entity_type=model.entity_type,
            entity_id=model.entity_id,
            field=model.field,
            old_value=_load(model.old_value),
            new_value=_load(model.new_value),
            actor=model.actor or "",
            created_at=model.created_at,
            note=model.note or "",
        )
        for model in session.execute(stmt).scalars()
    ]
