"""指标计算：加速度峰值/有效值与速度有效值（v_RMS）。

口径（与方案文档一致）：

- ``a_Peak = max|x|``（g）
- ``a_RMS = sqrt(mean(x²))``（g）
- ``v_RMS``：加速度 10–1000 Hz 带通 → 积分成速度（g→mm/s² 乘 ``G_TO_MM_S2``）→ RMS（mm/s）

带通、去直流从 ``params`` 取；``params=None`` 时用 ``PARAM_DEFAULTS``，
行为与改造前逐位一致（见 ``tests/test_features_metrics_params.py`` 的回归金标准）。

切窗（S4W-1 / S4W-2）：全部内置指标按 ``window_s`` / ``step_s`` 切分记录、逐窗算
（``window_plan`` + ``compute_metric_series``），一次导入产出多个时点；末段不足一窗丢弃。
**整段都不足一窗时一个值都不出**（G12，2026-11-02 现场定：不足指标计算窗口的数据直接判无效），
原因由 :func:`short_sample_reason` 给出（点数 / 窗长 / 采样率 / 所需点数）。
时间偏移取**窗中心**（相对记录起点，秒）。

- 频域 ``v_RMS``：带通 + 频域积分逐窗算，窗函数（``window_fn``）作用在带通、切首尾之后，
  并按系数 RMS 归一化，所以窗本身不改变量级；``rect`` = 不加窗，是**默认**（复查 v2 问题 5：
  hanning 主瓣宽 2 Hz，10 Hz 分量压在带通下限上时低估 10.31%）。hanning 留给频谱 / 包络谱。
- 时域 ``a_Peak`` / ``a_RMS``：逐窗先去直流（``detrend``）再取 max|x| / sqrt(mean(x²))；
  ``window_fn`` 是频域参数，时域逐窗值不受它影响（加窗会把窗内边缘的峰值削掉）。

单值接口 :func:`compute_metrics` 保持整段口径（回归金标准），只有逐窗路径切窗。

底层只用 numpy：带通是**频域滤波**（``rfft`` → 带外频点置零 → ``irfft``），速度快、
天然零相位、无 IIR 阶数与暂态（S4D，2026-11-02 现场定：不再用 ``butter`` + ``sosfiltfilt``）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from ..constants import G_TO_MM_S2, METRIC_UNITS, VELOCITY_BAND
from ..params import default_params, effective_params
from ..store import metrics as metric_store
from . import metric_plugins
from .filters import bandpass_freq

__all__ = [
    "G_TO_MM_S2",
    "METRIC_UNITS",
    "VELOCITY_BAND",
    "WINDOWED_CODES",
    "compute_metric_series",
    "compute_metrics",
    "short_sample_reason",
    "store_metrics",
    "velocity_series",
    "window_plan",
    "window_samples",
]

#: 逐窗产点的指标：S4W-2 起时域指标也切窗，故全部内置指标都在此列（同一套时点）
WINDOWED_CODES: tuple[str, ...] = tuple(METRIC_UNITS)


def _band_of(
    params: dict[str, Any] | None, sample_rate: float | None = None
) -> tuple[float, float]:
    """生效带通区间：params 里有就用，缺项回落默认值，再按采样率/窗长夹紧。"""
    base = default_params("v_RMS")
    merged = {**base, **(params or {})}
    effective = effective_params(
        {
            "band_low": merged["band_low"],
            "band_high": merged["band_high"],
            "window_s": merged.get("window_s"),
        },
        sample_rate,
    )
    return float(effective["band_low"]), float(effective["band_high"])


def _band_filter(
    x: np.ndarray, sample_rate: float, band: tuple[float, float] | None = None
) -> np.ndarray | None:
    """带通（默认 10–1000 Hz）：频域滤波——``rfft`` → 带外频点置零 → ``irfft``。

    频域置零是**循环卷积**：零相位（无延迟）、无 IIR 暂态、返回长度与输入一致，
    也没有「阶数」这个设置项（S4D）。带通不可用或样本太短返回 ``None``。

    上限按 ``effective_params``（fs/2×0.95）夹紧——与 ``params_hash`` /
    落库快照同一口径，绝不各写一份。
    """
    low, high = band or _band_of(None)
    effective = effective_params({"band_low": low, "band_high": high}, sample_rate)
    low, high = float(effective["band_low"]), float(effective["band_high"])
    if low >= high or x.size < 64:
        return None
    return bandpass_freq(x, sample_rate, low, high)


def _integrate_to_velocity(
    filtered: np.ndarray, sample_rate: float, band: tuple[float, float] | None = None
) -> np.ndarray:
    """加速度 → 速度（g→mm/s），频域积分：不会累积漂移，也不会放大带外噪声。"""
    spectrum = np.fft.rfft(filtered)
    freqs = np.fft.rfftfreq(filtered.size, d=1.0 / sample_rate)
    omega = 2.0 * np.pi * freqs
    low, high = band or _band_of(None)
    band_mask = (freqs >= low) & (freqs <= high)
    velocity_spec = np.zeros_like(spectrum)
    velocity_spec[band_mask] = spectrum[band_mask] * G_TO_MM_S2 / (1j * omega[band_mask])
    return np.fft.irfft(velocity_spec, n=filtered.size)


def velocity_series(
    samples: np.ndarray, sample_rate: float, params: dict[str, Any] | None = None
) -> np.ndarray:
    """速度时域序列（mm/s）：带通 + 频域积分，与 ``v_RMS`` 同口径。

    返回长度与输入一致（不做边缘裁剪），供原始波形上屏用；
    样本太少或采样率不够带通时返回全零。
    """
    x = np.asarray(samples, dtype="float64").ravel()
    if sample_rate <= 0:
        raise ValueError(f"采样率不合法：{sample_rate}")
    band = _band_of(params, sample_rate)
    filtered = _band_filter(x - x.mean(), float(sample_rate), band)
    if filtered is None:
        return np.zeros_like(x)
    return _integrate_to_velocity(filtered, float(sample_rate), band)


def _window_coefficients(kind: str, length: int) -> np.ndarray:
    """窗函数系数；``rect`` = 全 1（不加窗）。不认识的窗函数直接报错，绝不静默当 rect。"""
    name = (kind or "rect").strip().lower()
    if name in ("rect", "rectangle", "boxcar", "none"):
        return np.ones(length, dtype="float64")
    if name in ("hanning", "hann"):
        return np.hanning(length)
    if name == "hamming":
        return np.hamming(length)
    if name == "blackman":
        return np.blackman(length)
    if name == "kaiser":
        return np.kaiser(length, 8.6)
    raise ValueError(f"不认识的窗函数：{kind}（可选 hanning/hann/hamming/blackman/kaiser/rect）")


def _apply_window(x: np.ndarray, kind: str) -> np.ndarray:
    """加窗并按系数 RMS 归一化——窗本身不改变信号量级，只改泄漏/边缘权重。"""
    coefficients = _window_coefficients(kind, x.size)
    scale = float(np.sqrt(np.mean(coefficients**2)))
    if scale <= 0:
        return x
    return x * coefficients / scale


def window_samples(sample_rate: float, params: dict[str, Any] | None = None) -> int:
    """一窗需要的样本点数 = ``round(window_s × fs)``（至少 1）。

    ``window_s`` 取该指标生效参数（缺项回落默认 2 s）——短样本判定与切窗用**同一个**数，
    绝不各写一份「2 s × fs」。
    """
    merged = {**default_params("v_RMS"), **(params or {})}
    return max(1, int(round(float(merged["window_s"]) * float(sample_rate))))


def short_sample_reason(
    length: int, sample_rate: float, params: dict[str, Any] | None = None
) -> str:
    """样本不足一窗 → 具体原因；够一窗（含恰好一窗）返回空串（G12）。

    文案带点数 / 窗长 / 采样率 / 所需点数，例如
    ``样本 32 点，不足一窗（2 s @ 32000 Hz 需 64000 点）``——
    页面与日志直接引用，不做二次拼接。
    """
    if length <= 0:
        return "采样点数为 0"
    need = window_samples(sample_rate, params)
    if length >= need:
        return ""
    window = float({**default_params("v_RMS"), **(params or {})}["window_s"])
    return (
        f"样本 {int(length)} 点，不足一窗（{window:g} s @ {float(sample_rate):g} Hz 需 {need} 点）"
    )


def window_plan(
    length: int, sample_rate: float, params: dict[str, Any] | None = None
) -> list[tuple[int, int]]:
    """按 ``window_s`` / ``step_s`` 切窗的样本区间 ``[(start, stop), ...]``。

    - 末段不足一窗丢弃（现场口径：2 s / 2 s 不重叠）
    - 整段不足一窗 → **空列表**：该类指标不出值，由 :func:`short_sample_reason`
      给出具体原因判定为无效（G12，撤销 S4W-1 的「整段当 1 个窗」兜底）
    - ``step_s`` 大于 ``window_s`` 时按窗长兜底（保存校验已拦，这里防呆）
    """
    merged = {**default_params("v_RMS"), **(params or {})}
    rate = float(sample_rate)
    window = window_samples(rate, merged)
    step = max(1, int(round(float(merged["step_s"]) * rate)))
    if step > window:
        step = window
    total = int(length)
    if total < window:
        return []
    return [(start, start + window) for start in range(0, total - window + 1, step)]


def _band_rms(x: np.ndarray, sample_rate: float, params: dict[str, Any] | None = None) -> float:
    """带通后的加速度 RMS（g）：频域置零 → 有效值。

    2026-11-02：a_RMS 也要有滤波设置（band_low / band_high）。
    带通不可用（样本太短 / 频带夹到为空）时退回不滤波的 RMS，绝不静默出 0。
    """
    band = _band_of(params, sample_rate)
    filtered = _band_filter(x, sample_rate, band)
    if filtered is None:
        return float(np.sqrt(np.mean(x**2)))
    return float(np.sqrt(np.mean(filtered**2)))


def _velocity_rms(
    x: np.ndarray,
    sample_rate: float,
    params: dict[str, Any] | None = None,
    window_fn: str | None = None,
) -> float:
    """带通 + 频域积分求速度 RMS（频域积分不会累积漂移，也不会放大带外噪声）。

    ``window_fn`` 只在逐窗路径传（窗内加窗）；整段单值路径传 ``None`` 保持改造前口径。
    """
    band = _band_of(params, sample_rate)
    filtered = _band_filter(x, sample_rate, band)
    if filtered is None:
        return 0.0
    # 切掉首尾各 0.5 s：频域置零是循环卷积，段首尾接缝处的 Gibbs 振铃经 1/ω 放大后
    # 会污染边界（S4D 之前这一步挡的是 butter 的 IIR 暂态，位置与量级不变）
    edge = min(int(sample_rate * 0.5), filtered.size // 4)
    if edge:
        filtered = filtered[edge:-edge]
    if filtered.size < 64:
        return 0.0
    if window_fn is not None:
        filtered = _apply_window(filtered, window_fn)
    return float(np.sqrt(np.mean(_integrate_to_velocity(filtered, sample_rate, band) ** 2)))


def compute_metrics(
    samples: np.ndarray,
    sample_rate: float,
    params: dict[str, Any] | None = None,
    codes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, float]:
    """算指标；``params`` 是已合并的生效参数（None = 默认），``codes`` 限定只算哪些。

    样本为空/采样率不合法直接报错。
    """
    x = np.asarray(samples, dtype="float64").ravel()
    if x.size == 0:
        raise ValueError("样本为空，无法计算指标")
    if sample_rate <= 0:
        raise ValueError(f"采样率不合法：{sample_rate}")

    plugins, _plugin_errors = metric_plugins.load_plugins()
    wanted = list(codes) if codes is not None else list(METRIC_UNITS)
    unknown = [code for code in wanted if code not in METRIC_UNITS and code not in plugins]
    if unknown:
        raise ValueError(
            f"不认识的指标码：{' / '.join(unknown)}"
            f"（内置：{' / '.join(METRIC_UNITS)}；自定义算法请放到 {metric_plugins.plugin_dir()}）"
        )

    detrend = bool({**default_params("a_Peak"), **(params or {})}.get("detrend", True))
    if detrend:
        x = x - x.mean()

    values: dict[str, float] = {}
    if "a_Peak" in wanted:
        values["a_Peak"] = float(np.max(np.abs(x)))
    if "a_RMS" in wanted:
        values["a_RMS"] = _band_rms(x, float(sample_rate), params)
    if "v_RMS" in wanted:
        values["v_RMS"] = _velocity_rms(x, float(sample_rate), params)
    for code in wanted:
        if code in METRIC_UNITS:
            continue
        values[code] = metric_plugins.compute_plugin_value(
            code, x, float(sample_rate), params or {}
        )
    return values


def _time_value(code: str, segment: np.ndarray) -> float:
    """窗内时域统计（``segment`` 已按 ``detrend`` 去直流）：峰值 / 有效值。"""
    if code == "a_Peak":
        return float(np.max(np.abs(segment)))
    return float(np.sqrt(np.mean(segment**2)))


def compute_metric_series(
    samples: np.ndarray,
    sample_rate: float,
    params: dict[str, Any] | None = None,
    codes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, list[tuple[float, float]]]:
    """按窗逐点算指标：``{code: [(相对记录起点的秒偏移, 值), ...]}``。

    - 全部内置指标（:data:`WINDOWED_CODES`）按同一把 ``window_s`` / ``step_s`` 切窗逐点，
      偏移取窗中心；末段不足一窗丢弃；**整段不足一窗时该指标一个值都不出**（G12，
      原因见 :func:`short_sample_reason`，由调用方记录）。
    - 逐窗先去直流（``detrend``），频域指标再带通 + 积分（并消费窗函数），
      a_RMS 同样先带通（band_low / band_high）再取有效值，
      a_Peak 直接取该窗的 max|x|（不带通）。
    """
    x = np.asarray(samples, dtype="float64").ravel()
    if x.size == 0:
        raise ValueError("样本为空，无法计算指标")
    if sample_rate <= 0:
        raise ValueError(f"采样率不合法：{sample_rate}")

    plugins, _plugin_errors = metric_plugins.load_plugins()
    wanted = list(codes) if codes is not None else list(METRIC_UNITS)
    unknown = [code for code in wanted if code not in METRIC_UNITS and code not in plugins]
    if unknown:
        raise ValueError(
            f"不认识的指标码：{' / '.join(unknown)}"
            f"（内置：{' / '.join(METRIC_UNITS)}；自定义算法请放到 {metric_plugins.plugin_dir()}）"
        )

    detrend = bool({**default_params("a_Peak"), **(params or {})}.get("detrend", True))
    # 逐窗 v_RMS 的窗型来自生效参数（默认 rect，见 params.PARAM_SCHEMA）：报废的/空的
    # 窗型一律按 rect 兜底——宁可不加窗，也不许静默用 hanning 把带边缘幅值削掉
    # （复查 v2 问题 5：10 Hz 分量曾因此低估 10.31%）。
    window_fn = (
        str({**default_params("v_RMS"), **(params or {})}.get("window_fn") or "rect")
        if "v_RMS" in wanted
        else "rect"
    )
    rate = float(sample_rate)
    plan = window_plan(x.size, rate, params)
    series: dict[str, list[tuple[float, float]]] = {code: [] for code in wanted}

    for start, stop in plan:
        segment = x[start:stop]
        if detrend:
            segment = segment - segment.mean()
        offset = (start + stop) / 2.0 / rate
        for code in wanted:
            if code == "v_RMS":
                value = _velocity_rms(segment, rate, params, window_fn=window_fn)
            elif code == "a_RMS":
                value = _band_rms(segment, rate, params)
            elif code in METRIC_UNITS:
                value = _time_value(code, segment)
            else:
                # 自定义算法插件：窗已去直流，参数原样传（含 window_s / step_s）
                value = metric_plugins.compute_plugin_value(code, segment, rate, params or {})
            series[code].append((offset, float(value)))
    return series


def store_metrics(
    session: Session,
    channel_id: int,
    values: dict[str, float],
    computed_at: str,
    param_hashes: dict[str, str] | str | None = None,
    rpm_used: float | None = None,
    rpm_source: str = "",
    stale: bool = False,
) -> int:
    """把一组指标值落库（同通道 × 同一时间点旧值覆盖），并带参数快照。

    ``param_hashes`` 可按指标码分别给（每个指标的参数不同）；``rpm_used``/
    ``rpm_source`` 记录本次计算所用转速（node / measured / rated）。
    """
    return metric_store.upsert_values(
        session,
        channel_id,
        values,
        METRIC_UNITS,
        computed_at,
        param_hashes=param_hashes,
        rpm_used=rpm_used,
        rpm_source=rpm_source,
        stale=stale,
    )
