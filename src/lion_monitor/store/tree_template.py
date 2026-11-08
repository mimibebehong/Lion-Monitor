"""健康树模板 v2：业务键引用 + 指标参数 + 阈值区段 + 导入预览与策略。

**v1 的问题**

1. 只导出结构 + attrs + 自增 ``ref_id``，指标参数（``params_json`` / ``params_locked``）全丢，
   导入到新任务后节点参数回到默认值；
2. ``ref_id`` 是库内自增 id，跨库 / 跨任务导入必然错挂或报「引用不存在」；
3. 阈值（``metric_limit``）完全不在模板里。

**v2 口径**

- 文件头：``{"schema","version":2,"exported_at","task","root","thresholds"?}``
- 引用按业务键：通道 → 通道号（``CH1``）；监测对象 → 「名称 + 部位」；指标 → 指标码
- 导入时按业务键查本任务台账；查不到的**全部收集**成未匹配清单，一次性报错（不猜不错挂）
- metric 节点带 ``params``（节点覆盖项，未固化时就是 diff）、``locked``、``rpm``
- 阈值区段按「节点路径 + 指标码」寻址，数据来自 ``metric_limit`` 现表

**v2 扩展字段（S3，2026-11-02）—— 生效值快照 + 参数策略**

- 导出：文件头加 ``params_snapshot: true``；已绑定的 metric 节点加 ``effective_params``
  （该节点实际生效的完整参数快照，固定口径 ``metric_assign.compose_params``）
- 导入：``ImportOptions.param_strategy`` 三选一 —— ``override``（默认，按生效值搬运）/
  ``locked_only``（只搬源里已固化的项）/ ``follow_default``（旧行为，只搬覆盖 diff）
- 预览：``ImportPreview.param_diffs`` 逐项列出「跟随默认会变成什么 → 模板生效值」
- 兼容：文件仍声明 ``version: 2``；没有 ``params_snapshot`` / ``effective_params`` 的旧 v2 文件
  一律按 ``follow_default`` 处理并在 ``notice`` 里提示；v1 文件照旧走旧逻辑
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import limits_rules
from ..params import default_params, diff_params
from . import channel_reg, metric_assign, metric_def, object_registry, tree
from .models import MetricAssign, MetricLimit

__all__ = [
    "DIRECTION_IN",
    "DIRECTION_OUT",
    "ImportOptions",
    "ImportPreview",
    "ImportReport",
    "LEGACY_NOTE",
    "PARAM_STRATEGIES",
    "ParamDiff",
    "SNAPSHOT_MISSING_NOTE",
    "SUPPORTED_VERSIONS",
    "THRESHOLD_STRATEGIES",
    "TemplateError",
    "UnmatchedRef",
    "VERSION",
    "export_template",
    "import_template",
    "preview_import",
    "validate_template",
]

SCHEMA = tree.TEMPLATE_SCHEMA
VERSION = 2
SUPPORTED_VERSIONS = (1, 2)

#: 参数导入策略（S3）：按生效值覆盖 / 只搬固化项 / 只搬覆盖 diff（旧行为）
PARAM_STRATEGIES = ("override", "locked_only", "follow_default")

#: 旧 v2 文件（没有生效值快照）的提示语
SNAPSHOT_MISSING_NOTE = (
    "该模板没有生效参数快照（旧版 v2 导出）：参数按「只搬覆盖差异」处理，"
    "目标任务默认与源任务不同时会静默变化，建议在源任务重新导出"
)


#: 阈值方向：库里存 ``>`` / ``<``，模板里写成人看得懂的 upper / lower
#: 方向词表由 :mod:`lion_monitor.limits_rules` 派生，这里只做模板读写映射，不另立一套口径
_MARKERS = (">", "<")
DIRECTION_OUT = {marker: limits_rules.template_of(marker) for marker in _MARKERS}
DIRECTION_IN = {
    name: marker for marker in _MARKERS for name in (limits_rules.template_of(marker), marker)
}

#: 阈值导入策略
THRESHOLD_STRATEGIES = ("overwrite", "fill_missing", "skip")

#: v1 模板的提示语（能读，但引用是自增 id，跨任务易错挂）
LEGACY_NOTE = "旧版模板（v1）：引用按自增 id 解析，能读但跨任务/跨库易错挂，建议在源任务重新导出 v2"


class TemplateError(tree.TreeError):
    """模板 v2 校验 / 解析错误（继承 TreeError，页面统一按 TreeError 捕获）。"""


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class UnmatchedRef:
    """模板里一个业务键在当前任务台账中找不到。"""

    path: tuple[str, ...]
    node_type: str
    field: str
    key: str
    reason: str

    @property
    def text(self) -> str:
        where = " / ".join(self.path) or "（根）"
        return f"{where}：{self.key}（{self.reason}）"


@dataclass(frozen=True)
class ParamDiff:
    """预览里一处参数漂移：跟默认会得到的值 → 模板里的生效值。"""

    path: tuple[str, ...]
    metric_code: str
    param: str
    target_value: Any
    template_value: Any

    @property
    def text(self) -> str:
        where = " / ".join(self.path) or "（根）"
        return (
            f"{where} × {self.metric_code}："
            f"{self.param} {_value_text(self.target_value)} → {_value_text(self.template_value)}"
        )


def _value_text(value: Any) -> str:
    """预览文案里的取值（数值去掉多余的 .0；None 表示目标任务里没有这一项）。"""
    if value is None:
        return "（无）"
    if isinstance(value, bool):
        return "开" if value else "关"
    if isinstance(value, (int, float)):
        return f"{float(value):g}"
    return str(value)


@dataclass(frozen=True)
class ImportPreview:
    """导入前预览：节点 / 指标绑定 / 阈值条数 + 未匹配台账清单 + 参数漂移明细。"""

    version: int
    task_name: str
    exported_at: str
    node_counts: dict[str, int] = field(default_factory=dict)
    metric_bindings: int = 0
    threshold_rows: int = 0
    unmatched: tuple[UnmatchedRef, ...] = ()
    legacy: bool = False
    notice: str = ""
    param_diffs: tuple[ParamDiff, ...] = ()
    has_snapshot: bool = False

    @property
    def blocked(self) -> bool:
        return bool(self.unmatched)

    @property
    def node_total(self) -> int:
        return sum(self.node_counts.values())

    @property
    def param_diff_count(self) -> int:
        return len(self.param_diffs)

    @property
    def param_diff_nodes(self) -> int:
        return len({(item.path, item.metric_code) for item in self.param_diffs})


@dataclass(frozen=True)
class ImportOptions:
    """导入范围与策略。"""

    structure: bool = True
    params: bool = True
    thresholds: bool = True
    threshold_strategy: str = "overwrite"
    param_strategy: str = "override"

    def __post_init__(self) -> None:
        if self.threshold_strategy not in THRESHOLD_STRATEGIES:
            allowed = " / ".join(THRESHOLD_STRATEGIES)
            raise TemplateError(f"阈值策略不合法：{self.threshold_strategy}（可选 {allowed}）")
        if self.param_strategy not in PARAM_STRATEGIES:
            allowed = " / ".join(PARAM_STRATEGIES)
            raise TemplateError(f"参数策略不合法：{self.param_strategy}（可选 {allowed}）")


@dataclass(frozen=True)
class ImportReport:
    """导入结果：新建节点、绑定指标、按生效值搬运的参数、写阈值、跳过及原因。"""

    version: int
    node_counts: dict[str, int] = field(default_factory=dict)
    metrics_bound: int = 0
    thresholds_written: int = 0
    thresholds_skipped: int = 0
    params_overridden: int = 0
    skipped: tuple[str, ...] = ()
    notice: str = ""

    @property
    def node_total(self) -> int:
        return sum(self.node_counts.values())


# ---------------------------------------------------------------- 导出


def _json_dict(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _export_binding(
    assigns: dict[tuple[int, str], MetricAssign],
    channel_id: int | None,
    code: str,
    session: Session,
    task_id: int,
) -> dict[str, Any]:
    """指标参数绑定按「通道节点 + 指标码」取（绑定行实际挂在指标节点上）。

    已绑定的节点额外写 ``effective_params``（完整生效快照，S3）：模板跨任务导入时
    目标任务的指标定义默认可能不同，只带覆盖 diff 会静默漂移。
    """
    row = assigns.get((channel_id, code)) if channel_id is not None else None
    if row is None:
        return {"bound": False, "params": {}, "locked": False}
    return {
        "bound": True,
        "params": _json_dict(row.params_json),
        "locked": bool(row.params_locked),
        "effective_params": metric_assign.compose_params(
            code,
            metric_assign.definition_defaults(session, task_id, code),
            _json_dict(row.params_json),
            bool(row.params_locked),
        ),
    }


def _export_node(
    row: dict[str, Any],
    nodes: dict[int, tree.NodeRow],
    refs: tuple[dict, dict, dict],
    assigns: dict[tuple[int, str], MetricAssign],
    session: Session,
    task_id: int,
) -> dict[str, Any]:
    objects, channels, metrics = refs
    node_type = row["type"]
    node: dict[str, Any] = {
        "type": node_type,
        "name": row["name"],
        "attrs": dict(row.get("attrs") or {}),
    }
    ref_id = row.get("ref_id")
    if node_type == "component":
        obj = objects.get(ref_id) if ref_id is not None else None
        if obj is not None:
            node["object"] = {"name": obj.name, "part": obj.part}
    elif node_type == "channel":
        code = channels.get(ref_id) if ref_id is not None else None
        node["channel"] = code or row["name"]
    elif node_type == "metric":
        attr_code = str((row.get("attrs") or {}).get("code") or "").strip()
        code = (metrics.get(ref_id) if ref_id is not None else "") or attr_code or row["name"]
        node["metric"] = code
        channel_id = nodes[row["id"]].parent_id if row["id"] in nodes else None
        node.update(_export_binding(assigns, channel_id, code, session, task_id))
        node["rpm"] = tree.node_rpm(list(nodes.values()), row["id"])
    node["children"] = [
        _export_node(child, nodes, refs, assigns, session, task_id)
        for child in row.get("children") or []
    ]
    return node


def export_template(
    session: Session, task_id: int, include_thresholds: bool = True
) -> dict[str, Any]:
    """把整棵树导出成 v2 模板（引用走业务键，metric 节点带参数，阈值可选）。"""
    snap = tree.tree_snapshot(session, task_id)
    if snap is None:
        raise TemplateError("当前任务还没有健康树，无法导出")
    nodes = {row.id: row for row in tree.list_nodes(session, task_id)}
    root_id = next((row.id for row in nodes.values() if row.parent_id is None), None)
    refs = (
        {row.id: row for row in object_registry.list_objects(session, task_id)},
        {row.id: row.code for row in channel_reg.list_channels(session, task_id)},
        {row.id: row.code for row in metric_def.list_metrics(session, task_id)},
    )
    metric_parent = {row.id: row.parent_id for row in nodes.values() if row.node_type == "metric"}
    assigns = {
        (metric_parent.get(row.node_id, row.node_id), row.metric_code): row
        for row in session.execute(
            select(MetricAssign).where(
                MetricAssign.task_id == task_id, MetricAssign.deleted_at.is_(None)
            )
        ).scalars()
    }
    task = tree.root_node(session, task_id)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "version": VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "task": task.name,
        # 生效值快照标记（S3）：旧 v2 文件没有这个键 → 导入端按 follow_default 处理并提示
        "params_snapshot": True,
        "root": _export_node(snap, nodes, refs, assigns, session, task_id),
    }
    if include_thresholds:
        payload["thresholds"] = _export_thresholds(session, task_id, nodes, root_id)
    return payload


def _node_path(
    node: tree.NodeRow, nodes: dict[int, tree.NodeRow], root_id: int | None
) -> list[str]:
    """节点路径（不含根节点）：阈值区段寻址用。"""
    chain = [int(part) for part in node.path.split("/") if part]
    return [nodes[item].name for item in chain if item in nodes and item != root_id]


def _export_thresholds(
    session: Session,
    task_id: int,
    nodes: dict[int, tree.NodeRow],
    root_id: int | None,
) -> list[dict[str, Any]]:
    """阈值区段：直接读 ``metric_limit`` 现表（不现算），悬空行（节点已删）不导出。"""
    rows: list[dict[str, Any]] = []
    stmt = (
        select(MetricLimit)
        .where(MetricLimit.task_id == task_id, MetricLimit.deleted_at.is_(None))
        .order_by(MetricLimit.id)
    )
    for model in session.execute(stmt).scalars():
        node = nodes.get(model.node_id)
        if node is None:
            continue
        rows.append(
            {
                "path": _node_path(node, nodes, root_id),
                "metric_code": model.metric_code,
                "warn": float(model.warn),
                "alert": float(model.alert),
                "direction": limits_rules.template_of(model.direction),
                "enabled": bool(model.enabled),
            }
        )
    return rows


# ---------------------------------------------------------------- 模板结构校验


def _walk(node: dict[str, Any], path: tuple[str, ...] = ()):
    """深度遍历模板，产出 (节点, 该节点的完整路径)。

    路径**不含根节点**：设备 = ``("卷绕产线",)``，通道 = ``("卷绕产线","隔膜过辊轴承","CH1")``。
    阈值区段里的 ``path`` 就是这个口径。
    """
    yield node, path
    for child in node.get("children") or []:
        yield from _walk(child, (*path, child["name"]))


def _count(node: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item, _path in _walk(node):
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    return counts


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _check_node_v2(node: Any, parent_type: str | None) -> None:
    if not isinstance(node, dict):
        raise TemplateError("模板节点必须是对象")
    node_type = node.get("type")
    if node_type not in tree.PARENT_OF:
        allowed = " / ".join(tree.PARENT_OF)
        raise TemplateError(f"节点类型不合法：{node_type}（允许 {allowed}）")
    expected = tree.PARENT_OF[node_type]
    if expected != parent_type:
        want = tree.NODE_TYPE_TEXT[expected] if expected else "无（只能作为根）"
        raise TemplateError(f"层级不合法：{tree.NODE_TYPE_TEXT[node_type]}的父节点应为 {want}")
    if not _as_text(node.get("name")):
        raise TemplateError("名称不能为空：模板里存在空名称节点")
    attrs = node.get("attrs")
    if attrs is not None and not isinstance(attrs, dict):
        raise TemplateError("attrs 必须是对象")

    obj = node.get("object")
    if obj is not None:
        if node_type != "component":
            raise TemplateError(f"只有监测对象节点能写 object：{node.get('name')}")
        if not isinstance(obj, dict) or not _as_text(obj.get("name")):
            raise TemplateError(f"object 必须是含 name 的对象：{node.get('name')}")
        if obj.get("part") is not None and not isinstance(obj.get("part"), str):
            raise TemplateError(f"object.part 必须是字符串：{node.get('name')}")
    for key, allowed in (("channel", "channel"), ("metric", "metric")):
        value = node.get(key)
        if value is None:
            continue
        if node_type != allowed:
            raise TemplateError(
                f"只有{tree.NODE_TYPE_TEXT[allowed]}节点能写 {key}：{node.get('name')}"
            )
        if not _as_text(value):
            raise TemplateError(f"{key} 不能为空：{node.get('name')}")

    if node_type == "metric":
        params = node.get("params") or {}
        if not isinstance(params, dict):
            raise TemplateError(f"metric 节点的 params 必须是对象：{node.get('name')}")
        locked = node.get("locked", False)
        if not isinstance(locked, bool):
            raise TemplateError(f"metric 节点的 locked 必须是布尔：{node.get('name')}")
        snapshot = node.get("effective_params")
        if snapshot is not None and not isinstance(snapshot, dict):
            raise TemplateError(f"metric 节点的 effective_params 必须是对象：{node.get('name')}")

        rpm = node.get("rpm")
        if rpm is not None:
            if isinstance(rpm, bool) or not isinstance(rpm, (int, float)) or float(rpm) <= 0:
                raise TemplateError(f"metric 节点的 rpm 必须是正数或 null：{node.get('name')}")

    for child in node.get("children") or []:
        _check_node_v2(child, node_type)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _check_thresholds(rows: Any) -> list[dict[str, Any]]:
    """阈值区段校验：寻址、数值、方向别名都查一遍（不落库）。"""
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise TemplateError("thresholds 必须是数组（可以是空数组）")
    clean: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        where = f"阈值第 {index} 条"
        if not isinstance(row, dict):
            raise TemplateError(f"{where}：必须是对象")
        path = row.get("path")
        if (
            not isinstance(path, list)
            or not path
            or not all(isinstance(part, str) and part.strip() for part in path)
        ):
            raise TemplateError(f"{where}：path 必须是非空的节点名称数组")
        code = _as_text(row.get("metric_code"))
        if not code:
            raise TemplateError(f"{where}：metric_code 不能为空")
        warn = _as_number(row.get("warn"))
        alert = _as_number(row.get("alert"))
        if warn is None or alert is None:
            raise TemplateError(f"{where}：warn / alert 必须是数字")
        if warn < 0 or alert < 0:
            raise TemplateError(f"{where}：阈值不能为负（warn={warn}, alert={alert}）")
        # 0 = 该档不判（2026-11-04 允许只设 warn 或只设 alert），
        # 两侧都 0 交由 validate_limit 报「至少填一个」
        try:
            kind = limits_rules.normalize_direction(row.get("direction") or "upper", strict=True)
            limits_rules.validate_limit(kind, warn, alert)
        except limits_rules.LimitError as exc:
            raise TemplateError(f"{where}：{exc}") from exc
        marker = limits_rules.marker_of(kind)
        enabled = row.get("enabled", True)
        if not isinstance(enabled, bool):
            raise TemplateError(f"{where}：enabled 必须是布尔")
        clean.append(
            {
                "path": tuple(str(part).strip() for part in path),
                "metric_code": code,
                "warn": warn,
                "alert": alert,
                "direction": marker,
                "enabled": enabled,
            }
        )
    return clean


def _check_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TemplateError("模板内容必须是 JSON 对象")
    if payload.get("schema") != SCHEMA:
        raise TemplateError(f"模板标识不匹配：需要 {SCHEMA}")
    version = payload.get("version")
    if version not in SUPPORTED_VERSIONS:
        allowed = " / ".join(str(item) for item in SUPPORTED_VERSIONS)
        raise TemplateError(f"模板版本不支持：{version}（支持 {allowed}）")
    if version != VERSION:
        raise TemplateError(f"内部错误：v{version} 不归 v2 校验器处理")
    root = payload.get("root")
    if not isinstance(root, dict):
        raise TemplateError("模板缺少 root 节点")
    _check_node_v2(root, None)
    _check_thresholds(payload.get("thresholds"))
    return root


def validate_template(payload: dict[str, Any]) -> dict[str, int]:
    """只校验 v2 模板结构，返回各类节点条数；不碰数据库。"""
    return _count(_check_payload(payload))


# ---------------------------------------------------------------- 台账解析


def _unmatched_text(items: list[UnmatchedRef]) -> str:
    lines = "\n".join(f"  · {row.text}" for row in items)
    return (
        f"模板里有 {len(items)} 处引用在本任务台账中找不到（未匹配清单），"
        f"已停止导入（不猜、不错挂）：\n{lines}"
    )


def _resolve_refs(
    session: Session, task_id: int, root: dict[str, Any]
) -> tuple[dict[tuple[str, ...], tuple[str, int | None]], list[UnmatchedRef]]:
    """把模板业务键解析成本任务台账 id；解析不了的收集成清单。"""
    objects = {
        (row.name, row.part): row.id for row in object_registry.list_objects(session, task_id)
    }
    channels = {row.code: row.id for row in channel_reg.list_channels(session, task_id)}
    metrics = {row.code: row.id for row in metric_def.list_metrics(session, task_id)}

    resolved: dict[tuple[str, ...], tuple[str, int | None]] = {}
    unmatched: list[UnmatchedRef] = []

    for node, path in _walk(root):
        node_type = node["type"]
        if node_type == "component" and node.get("object"):
            obj = node["object"]
            key = (str(obj.get("name") or "").strip(), str(obj.get("part") or "").strip())
            row_id = objects.get(key)
            if row_id is None:
                unmatched.append(
                    UnmatchedRef(
                        path=path,
                        node_type=node_type,
                        field="object",
                        key=f"{key[0]} / {key[1] or '（无部位）'}",
                        reason="监测对象台账里没有同名同部位的对象",
                    )
                )
            else:
                resolved[path] = ("monitor_object", row_id)
        elif node_type == "channel" and node.get("channel"):
            code = str(node["channel"]).strip()
            row_id = channels.get(code)
            if row_id is None:
                unmatched.append(
                    UnmatchedRef(
                        path=path,
                        node_type=node_type,
                        field="channel",
                        key=code,
                        reason="通道台账里没有这个通道号",
                    )
                )
            else:
                resolved[path] = ("channel_reg", row_id)
        elif node_type == "metric" and node.get("metric"):
            code = str(node["metric"]).strip()
            row_id = metrics.get(code)
            if row_id is None:
                unmatched.append(
                    UnmatchedRef(
                        path=path,
                        node_type=node_type,
                        field="metric",
                        key=code,
                        reason="指标台账里没有这个指标码",
                    )
                )
            else:
                resolved[path] = ("metric_def", row_id)
    return resolved, unmatched


# ---------------------------------------------------------------- 预览


def _param_diffs(session: Session, task_id: int, root: dict[str, Any]) -> tuple[ParamDiff, ...]:
    """预览参数漂移：模板生效值 vs 「只搬覆盖 diff 时」目标任务的生效值。

    后者就是 ``follow_default`` 策略的落库结果（目标定义默认 + 模板 ``params``｜固化快照），
    两者不同的项就是旧行为下会**静默变化**的参数。
    """
    diffs: list[ParamDiff] = []
    for node, path in _walk(root):
        if node["type"] != "metric":
            continue
        snapshot = node.get("effective_params")
        if not isinstance(snapshot, dict) or not snapshot:
            continue
        code = str(node.get("metric") or "").strip()
        if not code:
            continue
        follows = metric_assign.compose_params(
            code,
            metric_assign.definition_defaults(session, task_id, code),
            dict(node.get("params") or {}),
            bool(node.get("locked")),
        )
        for key in sorted(snapshot):
            after = snapshot[key]
            if not diff_params({key: after}, {key: follows.get(key)}):
                continue
            diffs.append(
                ParamDiff(
                    path=path,
                    metric_code=code,
                    param=key,
                    target_value=follows.get(key),
                    template_value=after,
                )
            )
    return tuple(diffs)


def preview_import(session: Session, task_id: int, payload: dict[str, Any]) -> ImportPreview:
    """导入前预览：v2 给条数 + 未匹配台账清单 + 参数漂移；v1 只看结构并给旧版提示。不写库。"""
    if tree.payload_version(payload) == 1:
        counts = tree.validate_template(payload)
        return ImportPreview(
            version=1,
            task_name=str((payload.get("root") or {}).get("name") or ""),
            exported_at=str(payload.get("exported_at") or ""),
            node_counts=counts,
            legacy=True,
            notice=LEGACY_NOTE,
        )
    root = _check_payload(payload)
    resolved, unmatched = _resolve_refs(session, task_id, root)
    binds = sum(1 for node, _path in _walk(root) if node["type"] == "metric" and node.get("bound"))
    rows = payload.get("thresholds") or []
    has_snapshot = bool(payload.get("params_snapshot"))
    return ImportPreview(
        version=int(payload.get("version") or VERSION),
        task_name=str(payload.get("task") or root["name"]),
        exported_at=str(payload.get("exported_at") or ""),
        node_counts=_count(root),
        metric_bindings=binds,
        threshold_rows=len(rows) if isinstance(rows, list) else 0,
        unmatched=tuple(unmatched),
        notice="" if has_snapshot else SNAPSHOT_MISSING_NOTE,
        param_diffs=_param_diffs(session, task_id, root) if has_snapshot else (),
        has_snapshot=has_snapshot,
    )


# ---------------------------------------------------------------- 导入


def _create_tree(
    session: Session,
    task_id: int,
    root: dict[str, Any],
    resolved: dict[tuple[str, ...], tuple[str, int | None]],
) -> tuple[dict[str, int], dict[tuple[str, ...], int]]:
    """整树替换导入，返回 (各类型条数, 路径→节点 id)。

    G6：旧树不是静默批量删——每个被替换的节点各写一条 ``deletion_log``（子树快照），
    回收站里能逐条看到路径并恢复。
    """
    for row in tree.list_nodes(session, task_id):
        if row.parent_id is None:
            tree.soft_delete_node(session, row.id, log_each_node=True)

    counts: dict[str, int] = {}
    paths: dict[tuple[str, ...], int] = {}

    def _create(node: dict[str, Any], parent_id: int | None, path: tuple[str, ...]) -> None:
        ref_type, ref_id = resolved.get(path, ("", None))
        created = tree.create_node(
            session,
            task_id,
            node["type"],
            node["name"],
            parent_id=parent_id,
            attrs=dict(node.get("attrs") or {}),
            ref_type=ref_type,
            ref_id=ref_id,
        )
        counts[created.node_type] = counts.get(created.node_type, 0) + 1
        paths[path] = created.id
        for child in node.get("children") or []:
            _create(child, created.id, (*path, child["name"]))

    _create(root, None, ())
    return counts, paths


def _existing_paths(
    session: Session, task_id: int, root: dict[str, Any]
) -> dict[tuple[str, ...], int]:
    """不替换树时：按「名称路径」把模板节点落到现有树上（落不到的跳过）。"""
    nodes = tree.list_nodes(session, task_id)
    children: dict[int | None, list[tree.NodeRow]] = {}
    for row in nodes:
        children.setdefault(row.parent_id, []).append(row)
    root_row = next((row for row in nodes if row.parent_id is None), None)
    if root_row is None:
        return {}

    paths: dict[tuple[str, ...], int] = {}

    def _match(parent_id: int, path: tuple[str, ...], node: dict[str, Any]) -> None:
        kids = children.get(parent_id, [])
        hit = next(
            (row for row in kids if row.name == node["name"] and row.node_type == node["type"]),
            None,
        )
        if hit is None:
            return
        paths[path] = hit.id
        for child in node.get("children") or []:
            _match(hit.id, (*path, child["name"]), child)

    # 根节点按任务对齐（名字未必相同），子节点按名称逐个匹配
    paths[()] = root_row.id
    for child in root.get("children") or []:
        _match(root_row.id, (child["name"],), child)
    return paths


def _find_assign(session: Session, task_id: int, node_id: int, code: str) -> MetricAssign | None:
    return metric_assign.find_assignment(session, task_id, node_id, code)


def _apply_params(
    session: Session,
    task_id: int,
    root: dict[str, Any],
    paths: dict[tuple[str, ...], int],
    strategy: str = "follow_default",
) -> tuple[int, int, list[str]]:
    """把 metric 节点的参数 / locked / rpm 落到节点上。

    返回 ``(绑定条数, 按生效值搬运的条数, 跳过原因)``。

    参数策略（S3）：

    - ``override``：模板带 ``effective_params`` 的节点按生效值搬运 —— 落库的是「目标定义默认 →
      模板生效值」的 diff，目标侧再叠加自己的定义默认后仍等于模板生效值；
    - ``locked_only``：只对模板里 ``locked=True`` 的节点按生效值搬运，其余走旧行为；
    - ``follow_default``：旧行为，只搬 ``params``（覆盖 diff），生效值跟随目标任务默认。
    """
    bound = 0
    overridden = 0
    skipped: list[str] = []
    nodes = {row.id: row for row in tree.list_nodes(session, task_id)}
    for node, path in _walk(root):
        if node["type"] != "metric":
            continue
        where = " / ".join(path)
        node_id = paths.get(path)
        if node_id is None:
            skipped.append(f"{where}：目标树里没有对应指标节点，参数未落库")
            continue
        if node.get("rpm"):
            if tree.node_rpm(list(nodes.values()), node_id) is None:
                attrs = dict(nodes[node_id].attrs)
                attrs[tree.RPM_ATTR] = f"{float(node['rpm']):g}"
                tree.update_node(session, node_id, attrs=attrs)
                nodes = {row.id: row for row in tree.list_nodes(session, task_id)}
        if not node.get("bound"):
            continue
        channel_id = paths.get(path[:-1])
        if channel_id is None:
            skipped.append(f"{where}：目标树里没有对应通道节点，参数未落库")
            continue
        code = str(node.get("metric") or "").strip()
        if not code:
            skipped.append(f"{where}：指标节点没有指标码，参数未落库")
            continue
        params = dict(node.get("params") or {})
        locked = bool(node.get("locked"))
        snapshot = node.get("effective_params")
        use_snapshot = (
            (strategy == "override" or (strategy == "locked_only" and locked))
            and isinstance(snapshot, dict)
            and bool(snapshot)
        )
        if use_snapshot:
            base = (
                default_params(code)
                if locked
                else metric_assign.definition_defaults(session, task_id, code)
            )
            params = diff_params(snapshot, base)
            overridden += 1
        existing = _find_assign(session, task_id, channel_id, code)
        if existing is None:
            metric_assign.assign_metric(
                session, task_id, channel_id, code, params=params, params_locked=locked
            )
        else:
            if bool(existing.params_locked) != locked:
                metric_assign.set_locked(session, existing.id, locked)
            metric_assign.update_params(session, existing.id, params)
        bound += 1
    return bound, overridden, skipped


def _find_limit(session: Session, node_id: int, code: str) -> MetricLimit | None:
    return (
        session.execute(
            select(MetricLimit).where(
                MetricLimit.node_id == node_id,
                MetricLimit.metric_code == code,
                MetricLimit.deleted_at.is_(None),
            )
        )
        .scalars()
        .first()
    )


def _upsert_limit(session: Session, task_id: int, row: dict[str, Any], node_id: int) -> None:
    """写一行阈值（存在即覆盖）。

    不再自写一套大小关系校验：口径统一走 :func:`lion_monitor.limits_rules.validate_limit`
    （上限 warn < alert、下限 warn > alert）。这里保留直接写模型是为了带上模板里的
    ``enabled`` 标志（``limits.set_limit`` 只写启用行）。
    """
    limits_rules.validate_limit(row["direction"], row["warn"], row["alert"])
    model = _find_limit(session, node_id, row["metric_code"])
    if model is None:
        model = MetricLimit(task_id=task_id, node_id=node_id, metric_code=row["metric_code"])
        session.add(model)
    model.warn = float(row["warn"])
    model.alert = float(row["alert"])
    model.direction = limits_rules.marker_of(row["direction"])
    model.enabled = 1 if row["enabled"] else 0
    model.updated_at = datetime.now()
    session.commit()


def _apply_thresholds(
    session: Session,
    task_id: int,
    rows: list[dict[str, Any]],
    paths: dict[tuple[str, ...], int],
    strategy: str,
) -> tuple[int, int, list[str]]:
    """按策略写阈值，返回 (写入条数, 跳过条数, 跳过原因)。"""
    if not rows:
        return 0, 0, []
    missing = [
        f"  · {' / '.join(row['path'])} × {row['metric_code']}"
        for row in rows
        if row["path"] not in paths
    ]
    if missing:
        raise TemplateError(
            f"阈值区段有 {len(missing)} 条在目标树里找不到对应节点，已停止导入（不猜、不错挂）：\n"
            + "\n".join(missing)
        )
    if strategy == "skip":
        return 0, len(rows), [f"阈值策略=跳过：{len(rows)} 条未写"]

    written = 0
    skipped = 0
    reasons: list[str] = []
    for row in rows:
        node_id = paths[row["path"]]
        where = f"{' / '.join(row['path'])} × {row['metric_code']}"
        if strategy == "fill_missing" and _find_limit(session, node_id, row["metric_code"]):
            skipped += 1
            reasons.append(f"{where}：已有阈值，按「只补缺失」跳过")
            continue
        _upsert_limit(session, task_id, row, node_id)
        written += 1
    return written, skipped, reasons


def import_template(
    session: Session, task_id: int, payload: dict[str, Any], options: ImportOptions | None = None
) -> ImportReport:
    """v2 模板导入：先整体解析台账（未匹配一次性报错），再按范围与策略落库。"""
    options = options or ImportOptions()
    root = _check_payload(payload)
    resolved, unmatched = _resolve_refs(session, task_id, root)
    if unmatched:
        raise TemplateError(_unmatched_text(unmatched))

    # 没有生效值快照的旧 v2 文件：一律按「只搬覆盖 diff」，并提示可能静默漂移（S3）
    param_strategy = options.param_strategy
    notice = ""
    if not payload.get("params_snapshot"):
        param_strategy = "follow_default"
        notice = SNAPSHOT_MISSING_NOTE

    skipped: list[str] = []
    if options.structure:
        counts, paths = _create_tree(session, task_id, root, resolved)
    else:
        counts, paths = {}, _existing_paths(session, task_id, root)
        for _node, path in _walk(root):
            if path not in paths:
                skipped.append(
                    f"{' / '.join(path) or '（根）'}：现有树里找不到同名同类型节点，未处理"
                )

    bound = overridden = 0
    if options.params:
        bound, overridden, param_skipped = _apply_params(
            session, task_id, root, paths, param_strategy
        )
        skipped.extend(param_skipped)

    written = threshold_skipped = 0
    if options.thresholds:
        rows = _check_thresholds(payload.get("thresholds"))
        written, threshold_skipped, reasons = _apply_thresholds(
            session, task_id, rows, paths, options.threshold_strategy
        )
        skipped.extend(reasons)

    return ImportReport(
        version=VERSION,
        node_counts=counts,
        metrics_bound=bound,
        thresholds_written=written,
        thresholds_skipped=threshold_skipped,
        params_overridden=overridden,
        skipped=tuple(skipped),
        notice=notice,
    )
