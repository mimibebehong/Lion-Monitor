"""指标台账：指标码 + 单位 + 参数声明（与通道解耦）。

参数体系（方案文档 2.1）：

- ``params_schema_json``：字段声明，UI 按它动态渲染表单，新增指标不改前端代码
- ``params_default_json``：本指标的定义默认值，节点未覆盖时生效
- ``params_json``：历史字段（早期自由 JSON），保留读取兼容
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..constants import METRIC_UNITS
from ..params import PARAM_DEFAULTS, PARAM_SCHEMA, diff_params
from . import audit, tree
from .models import MetricDef, TaskNode, TestTask

__all__ = [
    "code_usage",
    "DEFAULT_METRICS",
    "MetricDefError",
    "MetricDefRow",
    "create_metric",
    "delete_metric",
    "find_metric",
    "list_metrics",
    "seed_defaults",
    "update_metric",
]

#: 内置指标与默认单位（别名，真定义在 ``lion_monitor.constants``）
DEFAULT_METRICS: dict[str, str] = METRIC_UNITS


class MetricDefError(Exception):
    """指标台账业务错误。"""


@dataclass(frozen=True)
class MetricDefRow:
    id: int
    task_id: int
    code: str
    unit: str
    params: dict[str, Any] = field(default_factory=dict)
    schema: list[dict[str, Any]] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    #: 本次改定义默认值时被标「待重算」的指标值行数（页面提示「待重算 N 条」用）
    stale_marked: int = 0


def _json_of(raw: str | None, fallback: Any) -> Any:
    try:
        data = json.loads(raw or "")
    except json.JSONDecodeError:
        return fallback
    return data if isinstance(data, type(fallback)) else fallback


def _to_row(model: MetricDef) -> MetricDefRow:
    return MetricDefRow(
        id=model.id,
        task_id=model.task_id,
        code=model.code,
        unit=model.unit,
        params=_json_of(model.params_json, {}),
        schema=_json_of(model.params_schema_json, []),
        defaults=_json_of(model.params_default_json, {}),
        enabled=bool(model.enabled),
    )


def list_metrics(session: Session, task_id: int) -> list[MetricDefRow]:
    stmt = (
        select(MetricDef)
        .where(MetricDef.task_id == task_id, MetricDef.deleted_at.is_(None))
        .order_by(MetricDef.id)
    )
    return [_to_row(model) for model in session.execute(stmt).scalars()]


def find_metric(session: Session, task_id: int, code: str) -> MetricDefRow | None:
    """按指标码取台账行（没有就返回 None，用于合并生效参数）。"""
    clean = (code or "").strip()
    if not clean:
        return None
    stmt = select(MetricDef).where(
        MetricDef.task_id == task_id,
        MetricDef.code == clean,
        MetricDef.deleted_at.is_(None),
    )
    model = session.execute(stmt).scalars().first()
    return _to_row(model) if model is not None else None


def _active(session: Session, metric_id: int) -> MetricDef:
    model = session.get(MetricDef, metric_id)
    if model is None or model.deleted_at is not None:
        raise MetricDefError(f"指标不存在：{metric_id}")
    return model


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_metric(
    session: Session,
    task_id: int,
    code: str,
    unit: str = "",
    params: dict[str, Any] | None = None,
    schema: list[dict[str, Any]] | None = None,
    defaults: dict[str, Any] | None = None,
) -> MetricDefRow:
    clean = (code or "").strip()
    if not clean:
        raise MetricDefError("指标码不能为空")
    exists = session.execute(
        select(MetricDef.id).where(
            MetricDef.task_id == task_id,
            MetricDef.code == clean,
            MetricDef.deleted_at.is_(None),
        )
    ).first()
    if exists is not None:
        raise MetricDefError(f"指标已存在：{clean}")

    model = MetricDef(
        task_id=task_id,
        code=clean,
        unit=unit or DEFAULT_METRICS.get(clean, ""),
        params_json=_dump(params or {}),
        params_schema_json=_dump(schema or PARAM_SCHEMA.get(clean, [])),
        params_default_json=_dump(defaults or PARAM_DEFAULTS.get(clean, {})),
        enabled=1,
        created_at=datetime.now(),
    )
    session.add(model)
    session.commit()
    return _to_row(model)


def update_metric(
    session: Session,
    metric_id: int,
    unit: str | None = None,
    params: dict[str, Any] | None = None,
    enabled: bool | None = None,
    code: str | None = None,
    schema: list[dict[str, Any]] | None = None,
    defaults: dict[str, Any] | None = None,
) -> MetricDefRow:
    """改指标台账；真正变化的字段各写一条留痕（T3，见 ``store.audit``）。

    改「定义默认值」= 改所有**跟随定义**的节点的生效参数：这些节点里未固化、且没覆盖
    被改键的，历史 ``metric_value`` 置 ``stale=1``（本次只标记不重算，复查 v2 问题 3），
    返回的 ``MetricDefRow.stale_marked`` 是本次标记的条数（页面提示「待重算 N 条」）。
    """
    model = _active(session, metric_id)
    before = {"code": model.code, "unit": model.unit, "enabled": bool(model.enabled)}
    before_code = model.code
    before_params = _json_of(model.params_json, {})
    before_defaults = _json_of(model.params_default_json, {})
    before_schema = _json_of(model.params_schema_json, [])
    renamed = None
    if code is not None:
        clean = code.strip()
        if not clean:
            raise MetricDefError("指标码不能为空")
        exists = session.execute(
            select(MetricDef.id).where(
                MetricDef.task_id == model.task_id,
                MetricDef.code == clean,
                MetricDef.deleted_at.is_(None),
                MetricDef.id != model.id,
            )
        ).first()
        if exists is not None:
            raise MetricDefError(f"指标已存在：{clean}")
        model.code = clean
        renamed = clean
    if unit is not None:
        model.unit = unit
    if params is not None:
        model.params_json = _dump(params)
    if schema is not None:
        model.params_schema_json = _dump(schema)
    if defaults is not None:
        model.params_default_json = _dump(defaults)
    if enabled is not None:
        model.enabled = 1 if enabled else 0

    actor = audit.actor_of(session, model.task_id)
    audit.record_fields(
        session,
        entity_type="metric_def",
        entity_id=model.id,
        before=before,
        after={"code": model.code, "unit": model.unit, "enabled": bool(model.enabled)},
        actor=actor,
        note=model.code,
    )
    for name, old_value, new_raw in (
        ("params_json", before_params, model.params_json),
        ("params_default_json", before_defaults, model.params_default_json),
    ):
        audit.record_param_diff(
            session,
            entity_type="metric_def",
            entity_id=model.id,
            field=name,
            old_params=old_value,
            new_params=_json_of(new_raw, {}),
            actor=actor,
            note=model.code,
        )
    audit.record_change(
        session,
        entity_type="metric_def",
        entity_id=model.id,
        field="params_schema_json",
        old_value=before_schema,
        new_value=_json_of(model.params_schema_json, []),
        actor=actor,
        note=model.code,
    )

    if renamed is not None:
        tree.sync_ref_name(session, "metric_def", model.id, renamed)
    session.commit()

    stale_marked = 0
    if defaults is not None:
        after_defaults = _json_of(model.params_default_json, {})
        if diff_params(before_defaults, after_defaults) or diff_params(
            after_defaults, before_defaults
        ):
            # 延迟 import：metric_assign 依赖本模块（避免循环导入）
            from . import metric_assign

            affected, stale_marked = metric_assign.mark_stale_for_definition_change(
                session, model.task_id, [before_code, model.code], before_defaults
            )
            audit.record_change(
                session,
                entity_type="metric_def",
                entity_id=model.id,
                field="params_default_stale",
                old_value=0,
                new_value=stale_marked,
                actor=actor,
                note=(
                    f"{model.code}：定义默认值变更，{affected} 个跟随定义的节点旧值置待重算"
                    f"（{stale_marked} 条指标值）"
                ),
            )
            session.commit()
    return replace(_to_row(model), stale_marked=stale_marked)


def delete_metric(session: Session, metric_id: int) -> None:
    """软删指标定义；仍装配在健康树上时拒绝（否则树上节点变成悬空引用、
    计算会退回硬默认参数，等于静默改口径）。"""
    model = _active(session, metric_id)
    used = session.scalar(
        select(func.count())
        .select_from(TaskNode)
        .where(
            TaskNode.ref_type == "metric_def",
            TaskNode.ref_id == model.id,
            TaskNode.deleted_at.is_(None),
        )
    )
    if used:
        raise MetricDefError(
            f"该指标仍装配在健康树上（{used} 个节点）：请先在健康树移除这些指标节点，再删除定义"
        )
    model.deleted_at = datetime.now()
    session.commit()


def code_usage(session: Session, code: str) -> list[str]:
    """还有哪些任务的台账在用这个指标码（「任务号 · 任务名」），删算法前拦一道。"""
    stmt = (
        select(TestTask.code, TestTask.name)
        .join(MetricDef, MetricDef.task_id == TestTask.id)
        .where(
            MetricDef.code == code,
            MetricDef.deleted_at.is_(None),
            TestTask.deleted_at.is_(None),
        )
        .order_by(TestTask.id)
    )
    return [f"{row.code} · {row.name}" for row in session.execute(stmt)]


def seed_defaults(session: Session, task_id: int) -> int:
    """补内置三指标（v_RMS / a_Peak / a_RMS），已存在的跳过。

    新建时一并写入参数声明与定义默认值，界面开箱就有可渲染的表单。
    """
    existing = {row.code for row in list_metrics(session, task_id)}
    created = 0
    for code, unit in DEFAULT_METRICS.items():
        if code in existing:
            continue
        session.add(
            MetricDef(
                task_id=task_id,
                code=code,
                unit=unit,
                params_json="{}",
                params_schema_json=_dump(PARAM_SCHEMA.get(code, [])),
                params_default_json=_dump(PARAM_DEFAULTS.get(code, {})),
                enabled=1,
                created_at=datetime.now(),
            )
        )
        created += 1
    session.commit()
    return created
