"""Q9：band_low 过低会让 v_RMS 出现荒谬量级（合成数据实例）。

信号：0.5 g @ 1.5 Hz（低频噪声源）+ 0.2 g @ 100 Hz（正常振动）+ 少量白噪声，
采样率 32 kHz、窗长 2 s。band_low=0.1 Hz 时（改造前）v_RMS 达 362 mm/s 量级 ——
与复查报告 Q9 的 556 mm/s 同源：窗里装不下 0.1 Hz 的周期，低频分量被 1/ω 放大。
按窗长抬到 5 Hz 之后应回到个位数 mm/s。
"""

from __future__ import annotations

import numpy as np

from lion_monitor.features.metrics import compute_metrics
from lion_monitor.params import effective_params

FS = 32000.0
DURATION_S = 2.0


def _signal() -> np.ndarray:
    t = np.arange(int(FS * DURATION_S)) / FS
    rng = np.random.default_rng(7)
    return (
        0.5 * np.sin(2 * np.pi * 1.5 * t)
        + 0.2 * np.sin(2 * np.pi * 100.0 * t)
        + 0.01 * rng.standard_normal(t.size)
    )


def _low_band_params() -> dict:
    return {
        "window_s": DURATION_S,
        "step_s": DURATION_S,
        "band_low": 0.1,
        "band_high": 1000.0,
        "window_fn": "hanning",
        "detrend": True,
    }


def test_effective_band_raises_absurd_low_bound():
    got = effective_params(_low_band_params(), FS)

    assert got["band_low"] == 5.0


def test_v_rms_not_absurd_with_low_band_params():
    value = compute_metrics(_signal(), FS, _low_band_params(), codes=["v_RMS"])["v_RMS"]

    assert 0.0 < value < 10.0, value


def test_v_rms_matches_protected_reference():
    """抬高后的结果与手工用 5 Hz 下限算的一致（同一口径，不是另算一套）。"""
    guarded = compute_metrics(_signal(), FS, _low_band_params(), codes=["v_RMS"])["v_RMS"]
    reference = compute_metrics(
        _signal(), FS, {**_low_band_params(), "band_low": 5.0}, codes=["v_RMS"]
    )["v_RMS"]

    assert guarded == reference
