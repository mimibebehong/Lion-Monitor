"""批次 B-1（复查 v2 问题 5）：v_RMS 逐窗路径的默认窗型 = ``rect``。

根因（已实测，见 ``reports/2026-11-04-batch-b-probe-vrms.json``）：
2 s 窗 → 频率分辨率 0.5 Hz，hanning 主瓣宽 2 Hz；10 Hz 正好压在 ``band_low = 10 Hz``
下边界，主瓣 8–10 Hz 那一半被带通切掉 → 10 Hz 分量 v_RMS 低估 **10.31%**。
``rect``（等价于不加窗）没有主瓣展宽，低频幅值保真；hanning 仍保留在可选项里
给频谱图 / 包络谱用（低泄漏是刚需），只是不再做幅值类默认。

对应代码：``params.PARAM_SCHEMA["v_RMS"].window_fn.default``。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.signal import butter, sosfiltfilt

from lion_monitor.constants import G_TO_MM_S2
from lion_monitor.features.metrics import compute_metric_series
from lion_monitor.params import default_params
from lion_monitor.store import db as store_db

FS = 32000.0
AMP_G = 0.8


def _sine(freq_hz: float, seconds: float = 2.0, amp_g: float = AMP_G) -> np.ndarray:
    t = np.arange(int(round(seconds * FS))) / FS
    return amp_g * np.sin(2 * np.pi * freq_hz * t)


def _theory_mm_s(freq_hz: float, amp_g: float = AMP_G) -> float:
    """正弦加速度（g）→ 速度 RMS（mm/s）理论值 = A·9806.65 / (√2·2πf)。"""
    return amp_g * G_TO_MM_S2 / (math.sqrt(2) * 2 * math.pi * freq_hz)


def _series(
    samples: np.ndarray, window_fn: str | None = None, window_s: float = 2.0
) -> list[float]:
    """按给定窗型算逐窗 v_RMS 序列；``window_fn=None`` 表示用 schema 默认值。"""
    params = {**default_params("v_RMS"), "window_s": window_s, "step_s": window_s}
    if window_fn is not None:
        params["window_fn"] = window_fn
    points = compute_metric_series(samples, FS, params=params, codes=["v_RMS"])["v_RMS"]
    return [value for _offset, value in points]


def _vrms(samples: np.ndarray, window_fn: str | None = None, window_s: float = 2.0) -> float:
    values = _series(samples, window_fn, window_s)
    assert len(values) == 1, f"2 s 数据应恰好 1 个窗，实际 {len(values)}"
    return values[0]


def test_default_window_fn_is_rect():
    """默认窗型必须是 rect（幅值类指标不许被窗主瓣削掉带边缘能量）。"""
    assert default_params("v_RMS")["window_fn"] == "rect"


def test_10hz_amplitude_error_below_1_percent():
    """10 Hz / 0.8 g 正弦：改前（hanning）−10.31%，改后必须 < 1%。"""
    value = _vrms(_sine(10.0))
    expect = _theory_mm_s(10.0)
    assert value == pytest.approx(expect, rel=0.01)


@pytest.mark.parametrize("freq_hz", [20.0, 50.0, 100.0])
def test_mid_frequency_amplitude_not_worse_than_before(freq_hz: float):
    """20 / 50 / 100 Hz：改后误差仍 < 1%（改前这几点本来就 ≤ 0.13%）。"""
    value = _vrms(_sine(freq_hz))
    assert value == pytest.approx(_theory_mm_s(freq_hz), rel=0.01)


def test_sweep_10_to_500hz_error_below_2_percent():
    """10–500 Hz 逐点扫频（5 Hz 步长、带内 99 点）：默认窗型误差 < 2%。"""
    worst = 0.0
    for freq_hz in range(10, 501, 5):
        expect = _theory_mm_s(float(freq_hz))
        value = _vrms(_sine(float(freq_hz)))
        worst = max(worst, abs(value - expect) / expect)
    assert worst < 0.02, f"扫频最大误差 {worst * 100:.3f}% 超过 2%"


def test_below_band_component_still_attenuated():
    """5 Hz 在带通下限以下：rect 不该把带外分量放进来（仍被压到只剩极小残留）。"""
    value = _vrms(_sine(5.0))
    assert value < 0.05 * _theory_mm_s(5.0)


def _broadband(seconds: float = 8.0) -> np.ndarray:
    """固定种子宽带噪声：0.5 g 白噪 + 0.05 g@10 Hz + 0.3 g@300 Hz。"""
    rng = np.random.default_rng(20261104)
    t = np.arange(int(round(seconds * FS))) / FS
    return (
        0.5 * rng.standard_normal(t.size)
        + 0.05 * np.sin(2 * np.pi * 10.0 * t)
        + 0.3 * np.sin(2 * np.pi * 300.0 * t)
    )


def test_broadband_noise_totals_stable_with_window_length_under_rect():
    """rect 下噪声总量不随窗长漂移（泄漏没有额外放大）——与 8 s 长窗参考差 < 2%。

    真实实测（``reports/2026-11-04-batch-b-probe-vrms.json``）：2 s rect vs 8 s rect
    +1.23%；同一信号 hanning 差 +6.94%（窗把带内能量摊向高频、1/ω 加权后整体偏低）。
    """
    data = _broadband()
    short = float(np.mean(_series(data, "rect", 2.0)))
    reference = float(np.mean(_series(data, "rect", 8.0)))
    assert abs(short - reference) / reference < 0.02


def test_hanning_still_biases_broadband_noise_low():
    """hanning 对宽带噪声同样系统性偏低（同一 8 s 参考差 > 5%）——所以默认不能用它。"""
    data = _broadband()
    reference = float(np.mean(_series(data, "rect", 8.0)))
    hanning = float(np.mean(_series(data, "hanning", 8.0)))
    assert hanning < reference * 0.95


def test_band_limited_noise_total_close_to_long_window_reference():
    """50–1000 Hz 带限噪声：rect 2 s 与 8 s 参考差 < 1%（带边缘外没有能量时几乎无差别）。"""
    rng = np.random.default_rng(7)
    t = np.arange(int(round(8.0 * FS))) / FS
    white = rng.standard_normal(t.size)
    sos = butter(6, [50.0, 1000.0], btype="bandpass", fs=FS, output="sos")
    band = sosfiltfilt(sos, white)
    band = band / math.sqrt(float(np.mean(band**2))) * 0.5 + 0.05 * np.sin(2 * np.pi * 10.0 * t)
    short = float(np.mean(_series(band, "rect", 2.0)))
    reference = float(np.mean(_series(band, "rect", 8.0)))
    assert abs(short - reference) / reference < 0.01


def test_time_domain_metrics_stay_unwindowed():
    """a_Peak / a_RMS 口径不变：逐窗值不受 window_fn 影响（与 v_RMS 分离）。"""
    data = _broadband(2.0)
    rect = compute_metric_series(
        data, FS, params={**default_params("a_RMS"), "window_fn": "rect"}, codes=["a_RMS"]
    )["a_RMS"]
    hann = compute_metric_series(
        data, FS, params={**default_params("a_RMS"), "window_fn": "hanning"}, codes=["a_RMS"]
    )["a_RMS"]
    assert [value for _o, value in rect] == [value for _o, value in hann]


# ---------------------------------------------------------------- 启动迁移：旧口径标 stale

_RECT_SNAPSHOT = (
    '{"band_high":1000.0,"band_low":10.0,"detrend":true,"step_s":2.0,'
    '"window_fn":"rect","window_s":2.0}'
)
_HANN_SNAPSHOT = _RECT_SNAPSHOT.replace('"rect"', '"hanning"')


def _insert_value(
    engine, *, code: str, snapshot: str, stale: int = 0, deleted: bool = False
) -> int:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO metric_value (channel_id, node_id, code, value, unit, level,"
            " computed_at, params_hash, effective_params_json, rpm_source, stale, deleted_at)"
            " VALUES (1, NULL, ?, 1.0, 'mm/s', 'unset', '2026-11-04 09:00:00', '', ?, '', ?, ?)",
            (code, snapshot, stale, "2026-11-04 10:00:00" if deleted else None),
        )
        return int(conn.exec_driver_sql("SELECT max(id) FROM metric_value").scalar())


def _stale_flags(engine, ids: list[int]) -> dict[int, int]:
    placeholders = ",".join("?" * len(ids))
    with engine.begin() as conn:
        rows = conn.exec_driver_sql(
            f"SELECT id, stale FROM metric_value WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
    return {int(row[0]): int(row[1]) for row in rows}


def _reset_data_version(engine) -> None:
    """把 ``PRAGMA user_version`` 归零，模拟「升级前的老库」。"""
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA user_version = 0")


def test_startup_migration_marks_old_caliber_vrms_stale(tmp_path):
    """老库启动迁移：旧窗型 / 无快照的 v_RMS 标 stale；rect 点、别的指标、软删行不动。"""
    engine = store_db.make_engine(tmp_path / "mig.db")
    store_db.init_db(engine)
    ids = {
        "rect": _insert_value(engine, code="v_RMS", snapshot=_RECT_SNAPSHOT),
        "hann": _insert_value(engine, code="v_RMS", snapshot=_HANN_SNAPSHOT),
        "legacy": _insert_value(engine, code="v_RMS", snapshot=""),
        "deleted": _insert_value(engine, code="v_RMS", snapshot=_HANN_SNAPSHOT, deleted=True),
        "a_peak": _insert_value(engine, code="a_Peak", snapshot=_HANN_SNAPSHOT),
        "already": _insert_value(engine, code="v_RMS", snapshot=_HANN_SNAPSHOT, stale=1),
    }
    _reset_data_version(engine)  # 新库 init_db 已把版本置 1，这里还原成老库

    assert store_db.mark_legacy_vrms_stale(engine) == 2  # hann + legacy

    flags = _stale_flags(engine, list(ids.values()))
    assert flags[ids["rect"]] == 0
    assert flags[ids["hann"]] == 1
    assert flags[ids["legacy"]] == 1
    assert flags[ids["deleted"]] == 0
    assert flags[ids["a_peak"]] == 0
    assert flags[ids["already"]] == 1

    # 幂等：数据迁移版本已记到 user_version，再跑不重复扫表/标记
    assert store_db.mark_legacy_vrms_stale(engine) == 0
    with engine.begin() as conn:
        assert conn.exec_driver_sql("PRAGMA user_version").scalar() == (
            store_db.DATA_VERSION_VRMS_RECT
        )
