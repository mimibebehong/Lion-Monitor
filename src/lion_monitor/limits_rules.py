"""阈值规则的**单一来源**：方向口径、保存校验、判级。

口径（马哥 2026-10-31 拍板，替代先前「一律 warn < alert」的硬校验）：

- ``warn`` 是**级别更轻**的门槛，不是「数值更小」的门槛；
- ``upper``（越大越差）：要求 ``warn < alert``；
  值 ``>= warn`` → 警告，值 ``>= alert`` → 报警；
- ``lower``（越小越差）：要求 ``warn > alert``；
  值 ``<= warn`` → 警告，值 ``<= alert`` → 报警。

库里 ``metric_limit.direction`` 存 ``>`` / ``<``（老数据），模板里写 ``upper`` / ``lower``。
本模块不碰数据库、不依赖 pandas，任何要校验或判级的地方都调这里，禁止各写一套。
"""

from __future__ import annotations

import math

__all__ = [
    "DIRECTION_MARKER",
    "DIRECTION_TEXT",
    "LimitError",
    "grade",
    "marker_of",
    "normalize_direction",
    "problem_of",
    "template_of",
    "text_of",
    "validate_limit",
]

#: 方向 → 库里存的运算符标记
DIRECTION_MARKER = {"upper": ">", "lower": "<"}

#: 方向 → 界面上的叫法（阈值设置页下拉/表头用）
DIRECTION_TEXT = {"upper": "上限（越大越差）", "lower": "下限（越小越差）"}

#: 方向 → 模板里的写法
_TEMPLATE_NAME = {"upper": "upper", "lower": "lower"}

#: 各种写法 → 规范方向
_ALIASES = {
    "upper": "upper",
    ">": "upper",
    "gt": "upper",
    "上限": "upper",
    "lower": "lower",
    "<": "lower",
    "lt": "lower",
    "下限": "lower",
}

#: 各方向的合法大小关系与说明（错误文案统一从这张表生成）
_REQUIREMENT = {
    "upper": (
        "warn < alert",
        "warn 填较轻的门槛（数值较小），alert 填更严重的门槛（数值较大）",
    ),
    "lower": (
        "warn > alert",
        "warn 填较轻的门槛（数值较大），alert 填更严重的门槛（数值较小）",
    ),
}


class LimitError(Exception):
    """阈值业务错误（方向不合法 / 数值关系不合法 / 未知方向）。"""


def normalize_direction(direction: object, *, strict: bool = False) -> str:
    """把各种写法归一成 ``upper`` / ``lower``。

    读路径（``strict=False``）认不出的方向按 ``upper`` 处理，避免老库脏数据把页面炸掉；
    写路径务必 ``strict=True``，未知方向直接报错，不做猜测。
    """
    key = str(direction or "").strip().lower()
    kind = _ALIASES.get(key) or _ALIASES.get(str(direction or "").strip())
    if kind is not None:
        return kind
    if strict:
        raise LimitError(
            f"阈值方向不支持：{direction}（可用「上限（越大越差）」/「下限（越小越差）」，"
            "库里存 upper / lower 或 > / <）"
        )
    return "upper"


def marker_of(direction: object) -> str:
    """方向 → 库里存的标记（``>`` / ``<``）。"""
    return DIRECTION_MARKER[normalize_direction(direction)]


def text_of(direction: object) -> str:
    """方向 → 界面叫法。"""
    return DIRECTION_TEXT[normalize_direction(direction)]


def template_of(direction: object) -> str:
    """方向 → 模板里的写法（``upper`` / ``lower``）。"""
    return _TEMPLATE_NAME[normalize_direction(direction)]


def requirement_of(direction: object) -> str:
    """该方向要求的大小关系（如 ``warn > alert``）。"""
    return _REQUIREMENT[normalize_direction(direction, strict=True)][0]


def validate_limit(direction: object, warn: float, alert: float) -> None:
    """按方向校验 warn / alert，不合法抛 :class:`LimitError`。

    **0 表示该档未设**（马哥 2026-11-04：可以只设 warn 或只设 alert）：

    - 两侧都 0 → 等于没设阈值，报错（调用方应走删除）；
    - 只设一侧 → 无需比较大小，直接通过；
    - 两侧都有值 → 上限要 ``warn < alert``、下限要 ``warn > alert``。
    """
    kind = normalize_direction(direction, strict=True)
    if not warn and not alert:
        raise LimitError("warn 与 alert 至少填一个；两列都留空等于删除该行阈值")
    if not warn or not alert:
        return  # 单边设置：另一档不判
    rule, meaning = _REQUIREMENT[kind]
    ok = warn < alert if kind == "upper" else warn > alert
    if not ok:
        raise LimitError(
            f"方向「{DIRECTION_TEXT[kind]}」要求 {rule}：{meaning}；"
            f"收到 warn={warn:g}, alert={alert:g}"
        )


def problem_of(direction: object, warn: float, alert: float) -> str | None:
    """老数据体检：违反当前方向口径就返回说明，合法返回 ``None``（不改数值）。"""
    try:
        validate_limit(direction, warn, alert)
    except LimitError as exc:
        return str(exc)
    return None


def grade(value: float, direction: object, warn: float, alert: float) -> str:
    """单点判级 → ``normal`` / ``warn`` / ``alert`` / ``unset``（先判 alert，命中即 alert）。

    ``warn`` / ``alert`` 为 **0 = 该档未设**（马哥 2026-11-04 允许只设一侧）：
    只设 warn 时永远不产生 alert，只设 alert 时没有 warn 档。

    方向按 ``normalize_direction`` 宽松解析（老数据 ``<`` 直接映射到 lower）。
    注意：历史上 ``warn`` / ``alert`` 关系反向的老行，判级语义保持不变（先判 alert），
    只由 :func:`problem_of` 标记出来，不改数、不重算。

    **非有限值（NaN / ±Inf）不参与比较，直接返回 ``unset``**（复查 G10）：
    拿 NaN 去比大小永远为假，会把坏值判成「正常/绿色」；调用方拿到 ``unset`` 应显示
    「数据异常」而不是「正常」（见 ``features.health.point_level``）。
    """
    if not math.isfinite(value):
        return "unset"
    kind = normalize_direction(direction)
    # 0 = 该档未设（单边设置）：未设的一档永不命中，也不当成门槛
    if kind == "lower":
        if alert and value <= alert:
            return "alert"
        if warn and value <= warn:
            return "warn"
        return "normal"
    if alert and value >= alert:
        return "alert"
    if warn and value >= warn:
        return "warn"
    return "normal"
