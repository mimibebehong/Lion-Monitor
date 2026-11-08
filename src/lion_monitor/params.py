"""指标参数体系（纯函数层，不碰数据库）。

三层用途：

1. **schema 声明**（``PARAM_SCHEMA``）：每个指标声明自己的参数字段与默认值，UI 按声明动态渲染；
2. **合并与指纹**（``merge_params`` / ``diff_params`` / ``params_hash``）：逐层合并生效参数、
   只存覆盖项、给每个指标值留参数指纹（含指标码 + 采样率 + 夹紧后的生效带通）；
3. **校验与门控**（``validate_params`` / ``needs_speed`` / ``resolve_rpm``）：保存前拦住非法参数，
   无转速时把「需要转速」的指标挡在选择之外。

合并优先级（方案文档 3.1）::

    系统硬默认（PARAM_DEFAULTS）
      <  指标定义默认（metric_def.params_default_json）
      <  节点覆盖（metric_assign.params_json，仅当 params_locked=0）
      <  固化值（params_locked=1 时该 JSON 即全量生效值）

已定默认值（2026-10-31，改前先问）：v_RMS 带通 10–1000 Hz；计算时长 2 s、步长 2 s
（不重叠，末段不足一窗丢弃）；不设全局默认转速。时域指标（a_Peak / a_RMS）自 S4W-2 起
与频域**同口径**：同一套「计算时长 / 步长」，同一把 `window_plan` 切窗。

频域窗型（复查 v2 问题 5，2026-11-04）：v_RMS 默认 **rect**。hanning 主瓣宽 2 Hz，
2 s 窗下 10 Hz 分量正好落在带通下限、被切掉一半，幅值低估 10.31%；rect 误差 0.0%。
hanning 仍在可选项里，留给频谱图 / 包络谱这类需要低泄漏的场景。
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .constants import VELOCITY_BAND

__all__ = [
    "BANDWIDTH_KEYS",
    "BAND_CLAMP_RATIO",
    "BAND_LOW_FLOOR_HZ",
    "BAND_MIN_PERIODS",
    "HASH_LEN",
    "PARAM_DEFAULTS",
    "PARAM_SCHEMA",
    "SCHEMA_TYPES",
    "band_low_min_by_window",
    "clamp_band_low",
    "default_params",
    "diff_params",
    "effective_params",
    "effective_params_json",
    "merge_params",
    "needs_speed",
    "params_hash",
    "prepare_params",
    "resolve_rpm",
    "schema_defaults",
    "validate_params",
    "window_period_problem",
]

#: 支持的控件类型（UI 按这个分支渲染）
SCHEMA_TYPES = ("float", "int", "bool", "enum")

#: 参数分组：basic 常改（默认展开）、advanced 带宽/算法类（默认折叠）
GROUPS = ("basic", "advanced")

#: 带宽类参数——改动它们才会让已算的指标值失效（UI 拿它判断「需重算」）
BANDWIDTH_KEYS = ("band_low", "band_high")

#: 带通上限的奈奎斯特安全系数：实际生效上限 = fs/2 × 该系数（计算层与指纹层共用）
BAND_CLAMP_RATIO = 0.95

#: 带通下限硬下限（Hz，Q9）：低于它保存直接拒绝——0.1 Hz + 2 s 窗会让 v_RMS 冲出量级
BAND_LOW_FLOOR_HZ = 2.0

#: 一个窗至少要装的最低频周期数（低于它 band_low 自动抬到 ``BAND_MIN_PERIODS / window_s``）
BAND_MIN_PERIODS = 10.0

#: 哈希截断长度（sha1 前 12 位足够区分，UI 上也好看）
HASH_LEN = 12

#: 指标参数声明（字段：key/label/group/type/unit/default/min/max/options/help/scaling）
#: ``scaling="speed"`` 的频率项表示该指标依赖转速（无转速时禁用）。
PARAM_SCHEMA: dict[str, list[dict[str, Any]]] = {
    "v_RMS": [
        {
            "key": "window_s",
            "label": "计算时长",
            "group": "basic",
            "type": "float",
            "unit": "s",
            "default": 2.0,
            "min": 0.05,
            "help": "单个计算点所用原始数据长度（已定 2 s）",
        },
        {
            "key": "step_s",
            "label": "计算步长",
            "group": "basic",
            "type": "float",
            "unit": "s",
            "default": 2.0,
            "min": 0.05,
            "help": "默认与时长相同 = 不重叠；末段不足一窗丢弃",
        },
        {
            "key": "band_low",
            "label": "带通下限",
            "group": "advanced",
            "type": "float",
            "unit": "Hz",
            "default": VELOCITY_BAND[0],
            "min": BAND_LOW_FLOOR_HZ,
            "help": (
                f"硬下限 {BAND_LOW_FLOOR_HZ:g} Hz；窗长不够 10 个周期时会自动抬高并提示；"
                "带通在频域置零（带外能量直接清零）"
            ),
        },
        {
            "key": "band_high",
            "label": "带通上限",
            "group": "advanced",
            "type": "float",
            "unit": "Hz",
            "default": VELOCITY_BAND[1],
            "help": "默认 10–1000 Hz（已定）；不得超过 fs/2，计算时按 fs/2 × 0.95 自动夹紧",
        },
        {
            "key": "window_fn",
            "label": "窗函数",
            "group": "advanced",
            "type": "enum",
            "options": ["rect", "hanning", "hamming", "blackman", "kaiser"],
            # 复查 v2 问题 5：2 s 窗 → 0.5 Hz 分辨率，hanning 主瓣宽 2 Hz，10 Hz 正好压在
            # band_low=10 Hz 上，主瓣 8–10 Hz 那半被带通切掉 → 10 Hz 分量低估 10.31%。
            # 幅值类指标（v_RMS）默认 rect；hanning 留给频谱图 / 包络谱按需选。
            "default": "rect",
            "help": (
                "幅值类指标默认 rect（不加窗）：低频正好落在带通下限时，加窗的主瓣展宽"
                "会把带边缘能量切掉、幅值偏低；频谱 / 包络谱要低泄漏时再选 hanning"
            ),
        },
        {
            "key": "detrend",
            "label": "去直流",
            "group": "advanced",
            "type": "bool",
            "default": True,
        },
    ],
    "a_Peak": [
        {
            "key": "window_s",
            "label": "计算时长",
            "group": "basic",
            "type": "float",
            "unit": "s",
            "default": 2.0,
            "min": 0.05,
            "help": "单个计算点所用原始数据长度（已定 2 s，与频域同口径）",
        },
        {
            "key": "step_s",
            "label": "计算步长",
            "group": "basic",
            "type": "float",
            "unit": "s",
            "default": 2.0,
            "min": 0.05,
            "help": "默认与时长相同 = 不重叠；末段不足一窗丢弃",
        },
        {
            "key": "detrend",
            "label": "去直流",
            "group": "advanced",
            "type": "bool",
            "default": True,
        },
    ],
    "a_RMS": [
        {
            "key": "window_s",
            "label": "计算时长",
            "group": "basic",
            "type": "float",
            "unit": "s",
            "default": 2.0,
            "min": 0.05,
            "help": "单个计算点所用原始数据长度（已定 2 s，与频域同口径）",
        },
        {
            "key": "step_s",
            "label": "计算步长",
            "group": "basic",
            "type": "float",
            "unit": "s",
            "default": 2.0,
            "min": 0.05,
            "help": "默认与时长相同 = 不重叠；末段不足一窗丢弃",
        },
        {
            "key": "band_low",
            "label": "带通下限",
            "group": "advanced",
            "type": "float",
            "unit": "Hz",
            "default": VELOCITY_BAND[0],
            "min": BAND_LOW_FLOOR_HZ,
            "help": (
                f"加速度带通下限，硬下限 {BAND_LOW_FLOOR_HZ:g} Hz；"
                "窗长不够 10 个周期时自动抬高；滤波在频域置零（零相位）"
            ),
        },
        {
            "key": "band_high",
            "label": "带通上限",
            "group": "advanced",
            "type": "float",
            "unit": "Hz",
            "default": VELOCITY_BAND[1],
            "help": "默认 10–1000 Hz；不得超过 fs/2，计算时按 fs/2 × 0.95 自动夹紧",
        },
        {
            "key": "detrend",
            "label": "去直流",
            "group": "advanced",
            "type": "bool",
            "default": True,
        },
    ],
}

#: 系统硬默认（由 schema 派生，避免两处各写一份默认值）
PARAM_DEFAULTS: dict[str, dict[str, Any]] = {
    code: {item["key"]: item["default"] for item in schema} for code, schema in PARAM_SCHEMA.items()
}

#: key → 声明（跨指标取第一份；同名参数在不同指标里含义一致，默认值也一致）
_SCHEMA_INDEX: dict[str, dict[str, Any]] = {}
for _schema in PARAM_SCHEMA.values():
    for _item in _schema:
        _SCHEMA_INDEX.setdefault(_item["key"], _item)

#: 跨指标的默认值（做交叉校验时给缺项兜底）
_ALL_DEFAULTS: dict[str, Any] = {key: item["default"] for key, item in _SCHEMA_INDEX.items()}


def schema_defaults(schema: list[dict[str, Any]]) -> dict[str, Any]:
    """从 schema 抽出「键 → 默认值」。"""
    return {item["key"]: item["default"] for item in schema if "default" in item}


def default_params(code: str) -> dict[str, Any]:
    """取某指标的系统硬默认（返回副本，调用方随便改）。"""
    return dict(PARAM_DEFAULTS.get(code, {}))


def merge_params(*layers: dict[str, Any] | None) -> dict[str, Any]:
    """逐层合并；``None`` 与空层忽略，后面的层覆盖前面的层。"""
    merged: dict[str, Any] = {}
    for layer in layers:
        if not layer:
            continue
        merged.update(layer)
    return merged


def _same_value(left: Any, right: Any) -> bool:
    """数值按 float 比（1000 与 1000.0 算同一个值），其余按等值比。"""
    numbers = (int, float)
    numeric = (
        isinstance(left, numbers)
        and isinstance(right, numbers)
        and not isinstance(left, bool)
        and not isinstance(right, bool)
    )
    return float(left) == float(right) if numeric else left == right


def diff_params(current: dict[str, Any] | None, base: dict[str, Any] | None) -> dict[str, Any]:
    """覆盖 diff：只留与基准不同的项（节点只存覆盖项，方案文档 2.2）。"""
    base = base or {}
    changed: dict[str, Any] = {}
    for key, value in (current or {}).items():
        if key in base and _same_value(value, base[key]):
            continue
        changed[key] = value
    return changed


def _as_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def band_low_min_by_window(window_s: Any) -> float | None:
    """窗长决定的带通下限底线：``BAND_MIN_PERIODS / window_s``（窗长未知返回 None）。"""
    window = _as_number(window_s)
    if window is None or window <= 0:
        return None
    return round(BAND_MIN_PERIODS / window, 9)


def clamp_band_low(params: dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    """按「一个窗至少装 ``BAND_MIN_PERIODS`` 个最低频周期」抬高 ``band_low``。

    返回 (抬高后的参数副本, 中文提示)；不需要抬高时提示为 ``None``。
    只抬不降：``band_low`` 已经够低（例如默认 10 Hz）时原样返回。
    """
    result = dict(params or {})
    low = _as_number(result.get("band_low"))
    if low is None:
        return result, None
    floor = band_low_min_by_window(result.get("window_s"))
    if floor is None or floor <= low:
        return result, None
    result["band_low"] = floor
    window = _as_number(result.get("window_s"))
    note = (
        f"带通下限已按窗长自动抬高：{low:g} Hz → {floor:g} Hz"
        f"（{window:g} s 的窗至少装 {BAND_MIN_PERIODS:g} 个最低频周期）"
    )
    return result, note


def effective_params(
    params: dict[str, Any] | None, sample_rate: float | None = None
) -> dict[str, Any]:
    """**夹紧后**实际生效的参数（返回副本，不改入参）。

    - 带通上限超过奈奎斯特安全线（``fs/2 × BAND_CLAMP_RATIO``）时按计算层同一口径夹到
      该值——计算用的是夹紧值，指纹与落库快照也必须是这一份，否则会出现
      「同一算法、不同指纹」（S6）。采样率未知时不做上限夹紧。
    - 带通下限（Q9）：低于硬下限 ``BAND_LOW_FLOOR_HZ`` 或装不下 10 个周期时按窗长抬高，
      历史/定义默认里漏进来的过小值不会算出 556 mm/s 量级。
    """
    result, _ = clamp_band_low(params)
    low = _as_number(result.get("band_low"))
    if low is not None and low < BAND_LOW_FLOOR_HZ:
        result["band_low"] = BAND_LOW_FLOOR_HZ
    high = _as_number(result.get("band_high"))
    rate = _as_number(sample_rate)
    if high is None or not rate or rate <= 0:
        return result
    limit = round(rate / 2.0 * BAND_CLAMP_RATIO, 9)
    if high > limit:
        result["band_high"] = limit
    return result


def effective_params_json(params: dict[str, Any] | None, sample_rate: float | None = None) -> str:
    """生效参数的紧凑 JSON（落库快照用；键排序，跨进程稳定）。"""
    return json.dumps(
        effective_params(params, sample_rate),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def params_hash(
    params: dict[str, Any] | None, code: str = "", sample_rate: float | None = None
) -> str:
    """生效参数的稳定指纹：指标码 + 采样率 + **夹紧后**参数，sha1 前 12 位（S6）。

    - 夹紧后的生效参数相同 → 指纹相同（``band_high`` 16000 与 30000 在 fs=32000 下
      都被夹到 15200，指纹必须一致）
    - 同一参数换个采样率或换个指标码 → 指纹不同（可用带宽与算法都变了）
    - 键排序 + 紧凑 JSON，跨进程 / 跨运行一致

    空参数返回空串（表示「没有参数快照」），便于和历史行区分。
    """
    if not params:
        return ""
    effective = effective_params(params, sample_rate)
    payload = json.dumps(
        {"code": code, "fs": _as_number(sample_rate), "params": effective},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:HASH_LEN]


def window_period_problem(params: dict[str, Any] | None) -> str | None:
    """窗长装不下 10 个最低频周期时的中文原因（保存时可被「自动抬高」替代）。"""
    merged = merge_params(_ALL_DEFAULTS, params)
    low = _as_number(merged.get("band_low"))
    window = _as_number(merged.get("window_s"))
    if not low or low <= 0 or window is None:
        return None
    need = BAND_MIN_PERIODS / low
    if window >= need:
        return None
    return (
        f"计算时长 {window:g} s 太短：带通下限 {low:g} Hz 至少要 {need:.2f} s"
        f"（{BAND_MIN_PERIODS:g} 个周期）"
    )


def validate_params(params: dict[str, Any] | None, sample_rate: float | None = None) -> list[str]:
    """保存前校验，返回问题清单（空列表 = 可以保存）。

    规则（方案文档 4.1）：

    - ``band_high ≤ fs/2``（fs 未知时跳过这条）
    - ``window_s ≥ 10 / band_low``（一个窗至少装 10 个最低频率周期）
    - ``step_s ≤ window_s``
    - 另外按 schema 声明的 min/max/options 拦住越界值与非法枚举
    """
    merged = merge_params(_ALL_DEFAULTS, params)
    problems: list[str] = []

    for key, item in _SCHEMA_INDEX.items():
        if key not in (params or {}):
            continue
        value = merged[key]
        label = item["label"]
        if item["type"] == "enum":
            options = item.get("options") or []
            if value not in options:
                problems.append(f"{label}取值「{value}」不在可选项（{'/'.join(options)}）内")
            continue
        if item["type"] == "bool":
            continue
        number = _as_number(value)
        if number is None:
            problems.append(f"{label}必须是数字（当前「{value}」）")
            continue
        low_bound, high_bound = item.get("min"), item.get("max")
        if low_bound is not None and number < float(low_bound):
            problems.append(f"{label} {number:g} 小于允许下限 {float(low_bound):g}")
        if high_bound is not None and number > float(high_bound):
            problems.append(f"{label} {number:g} 大于允许上限 {float(high_bound):g}")

    if "band_low" in (params or {}) or "band_high" in (params or {}):
        low = _as_number(merged.get("band_low"))
        high = _as_number(merged.get("band_high"))
        if low is not None and high is not None:
            if low >= high:
                problems.append(f"带通下限 {low:g} Hz 必须小于带通上限 {high:g} Hz")
            elif sample_rate:
                nyquist = float(sample_rate) / 2.0
                if high > nyquist:
                    problems.append(
                        f"带通上限 {high:g} Hz 超过奈奎斯特频率 {nyquist:g} Hz"
                        "（采集卡的采样率不够）"
                    )

    short_window = window_period_problem(params)
    if short_window is not None:
        problems.append(short_window)

    window = _as_number(merged.get("window_s"))
    step = _as_number(merged.get("step_s"))
    if step is not None and window is not None and step > window:
        problems.append(f"计算步长 {step:g} s 不能大于计算时长 {window:g} s")

    return problems


def prepare_params(
    params: dict[str, Any] | None, sample_rate: float | None = None
) -> tuple[dict[str, Any], list[str], str | None]:
    """保存前的参数准备（Q9）：先按硬规则拦原始值，再按窗长自动抬高带通下限。

    顺序很重要：硬下限（2 Hz）、带宽顺序、奈奎斯特、步长这些**不救**，原样报错；
    「窗长装不下 10 个周期」这一条不报错，改为把 ``band_low`` 抬到 ``10 / window_s``
    并回一句中文提示（界面直接显示，落库的是抬高后的值）。

    返回 ``(待保存参数, 问题清单, 自动抬高提示)``；问题清单非空时不要保存。
    """
    raw = dict(params or {})
    short_window = window_period_problem(raw)
    problems = [text for text in validate_params(raw, sample_rate) if text != short_window]
    if problems:
        return raw, problems, None
    raised, note = clamp_band_low(raw)
    if note is None:
        return raised, [], None
    return raised, validate_params(raised, sample_rate), note


def needs_speed(schema: list[dict[str, Any]] | None) -> bool:
    """指标的 schema 里是否有 ``scaling="speed"`` 的频率项（有则依赖转速）。"""
    return any(item.get("scaling") == "speed" for item in (schema or []))


def resolve_rpm(
    node_rpm: float | None = None,
    measured_rpm: float | None = None,
    rated_rpm: float | None = None,
) -> tuple[float | None, str | None]:
    """转速三级：节点手填 > 批次实测 > 对象额定；都没有返回 (None, None)，绝不猜。"""
    for rpm, source in (
        (node_rpm, "node"),
        (measured_rpm, "measured"),
        (rated_rpm, "rated"),
    ):
        value = _as_number(rpm)
        if value is not None and math.isfinite(value) and value > 0:
            return value, source
    return None, None
