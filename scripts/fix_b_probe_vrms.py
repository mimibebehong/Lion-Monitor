"""批次 B 探针 1：v_RMS 逐窗默认窗型（复查 v2 问题 5）改前/改后数值对比。

复现根因：2 s 窗 → 0.5 Hz 分辨率，hanning 主瓣宽 2 Hz；10 Hz 正好压在
``band_low = 10 Hz`` 下边界，主瓣 8–10 Hz 那半被带通切掉 → 10 Hz 分量低估约 10%。
rect（等价于不加窗）没有主瓣展宽，幅值保真。

用法：``.venv/bin/python scripts/fix_b_probe_vrms.py``
只读纯计算，不碰数据库。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.constants import G_TO_MM_S2, VELOCITY_BAND  # noqa: E402
from lion_monitor.features.metrics import compute_metric_series  # noqa: E402
from lion_monitor.params import default_params  # noqa: E402

FS = 32000.0
AMP_G = 0.8
LOW, HIGH = VELOCITY_BAND


def sine(freq_hz: float, seconds: float = 2.0, amp_g: float = AMP_G) -> np.ndarray:
    t = np.arange(int(round(seconds * FS))) / FS
    return amp_g * np.sin(2 * np.pi * freq_hz * t)


def theory(freq_hz: float, amp_g: float = AMP_G) -> float:
    """正弦加速度（g）→ 速度 RMS（mm/s）理论值。"""
    return amp_g * G_TO_MM_S2 / (math.sqrt(2) * 2 * math.pi * freq_hz)


def vrms(x: np.ndarray, window_fn: str) -> float:
    params = {**default_params("v_RMS"), "window_fn": window_fn}
    series = compute_metric_series(x, FS, params=params, codes=["v_RMS"])["v_RMS"]
    return series[0][1] if series else float("nan")


def pct(got: float, base: float) -> float:
    return (got - base) / base * 100.0


def main() -> int:
    out: dict[str, object] = {
        "default_window_fn": default_params("v_RMS")["window_fn"],
        "band_hz": [LOW, HIGH],
        "fs_hz": FS,
        "amplitude_g": AMP_G,
    }

    # ---------- A. 单频正弦：理论值 vs hanning(改前) vs rect(改后) ----------
    rows = []
    for freq in (10.0, 20.0, 50.0, 100.0):
        x = sine(freq)
        expect = theory(freq)
        rect = vrms(x, "rect")
        hann = vrms(x, "hanning")
        rows.append(
            {
                "freq_hz": freq,
                "theory_mm_s": round(expect, 4),
                "hanning_mm_s": round(hann, 4),
                "hanning_err_pct": round(pct(hann, expect), 3),
                "rect_mm_s": round(rect, 4),
                "rect_err_pct": round(pct(rect, expect), 3),
                "rect_vs_hanning_pct": round(pct(rect, hann), 3),
            }
        )
    out["sine_theory"] = rows

    # ---------- B. 10–500 Hz 逐点扫频（5 Hz 步长）----------
    sweep = []
    for freq in range(5, 501, 5):
        x = sine(float(freq))
        expect = theory(float(freq))
        rect = vrms(x, "rect")
        hann = vrms(x, "hanning")
        sweep.append(
            {
                "freq_hz": float(freq),
                "in_band": LOW <= freq <= HIGH,
                "theory_mm_s": round(expect, 4),
                "rect_err_pct": round(pct(rect, expect), 3),
                "hanning_err_pct": round(pct(hann, expect), 3),
            }
        )
    in_band = [row for row in sweep if row["in_band"]]
    below = [row for row in sweep if not row["in_band"]]
    out["sweep"] = {
        "in_band_points": len(in_band),
        "rect_max_abs_err_pct": round(max(abs(row["rect_err_pct"]) for row in in_band), 3),
        "hanning_max_abs_err_pct": round(max(abs(row["hanning_err_pct"]) for row in in_band), 3),
        "worst_in_band_rect": max(in_band, key=lambda row: abs(row["rect_err_pct"])),
        "worst_in_band_hanning": max(in_band, key=lambda row: abs(row["hanning_err_pct"])),
        "below_band_rect_pct_of_theory": {
            f"{row['freq_hz']:g}Hz": round(row["rect_err_pct"], 1) for row in below
        },
    }

    # ---------- C. 宽带噪声（含 10 Hz 分量）：泄漏对总量的影响 ----------
    rng = np.random.default_rng(20261104)
    t = np.arange(int(round(2.0 * FS))) / FS
    noise = (
        0.5 * rng.standard_normal(t.size)
        + 0.05 * np.sin(2 * np.pi * 10.0 * t)
        + 0.3 * np.sin(2 * np.pi * 300.0 * t)
    )
    rect_noise = vrms(noise, "rect")
    hann_noise = vrms(noise, "hanning")
    out["broadband_noise"] = {
        "note": "0.5g 宽带噪声 + 0.05g@10Hz + 0.3g@300Hz",
        "rect_mm_s": round(rect_noise, 4),
        "hanning_mm_s": round(hann_noise, 4),
        "diff_pct": round(pct(rect_noise, hann_noise), 3),
        "abs_diff_pct": round(abs(pct(rect_noise, hann_noise)), 3),
    }

    # ---------- D. 噪声口径的旁证：2 s 窗 vs 8 s 长窗（同一信号、同一窗型）----------
    # 说明「rect 不引入窗长依赖」；hanning 在宽带信号上本身就偏低（不是长窗能救回来的）
    rng8 = np.random.default_rng(20261104)
    long_t = np.arange(int(round(8.0 * FS))) / FS
    long_noise = (
        0.5 * rng8.standard_normal(long_t.size)
        + 0.05 * np.sin(2 * np.pi * 10.0 * long_t)
        + 0.3 * np.sin(2 * np.pi * 300.0 * long_t)
    )

    def series_mean(signal: np.ndarray, window_fn: str, window_s: float) -> float:
        params = {
            **default_params("v_RMS"),
            "window_fn": window_fn,
            "window_s": window_s,
            "step_s": window_s,
        }
        points = compute_metric_series(signal, FS, params=params, codes=["v_RMS"])["v_RMS"]
        return float(np.mean([value for _offset, value in points]))

    rect_2s = series_mean(long_noise, "rect", 2.0)
    rect_8s = series_mean(long_noise, "rect", 8.0)
    hann_2s = series_mean(long_noise, "hanning", 2.0)
    hann_8s = series_mean(long_noise, "hanning", 8.0)
    out["noise_window_length_stability"] = {
        "note": "同一段 8 s 宽带噪声，按 2 s / 8 s 窗各算一遍取均值",
        "rect_2s_mm_s": round(rect_2s, 4),
        "rect_8s_mm_s": round(rect_8s, 4),
        "rect_2s_vs_8s_pct": round(pct(rect_2s, rect_8s), 3),
        "hanning_2s_mm_s": round(hann_2s, 4),
        "hanning_8s_mm_s": round(hann_8s, 4),
        "hanning_2s_vs_8s_pct": round(pct(hann_2s, hann_8s), 3),
        "hanning_8s_vs_rect_8s_pct": round(pct(hann_8s, rect_8s), 3),
    }

    # ---------- E. 「泄漏有没有影响」的干净对照 ----------
    # 任务书要求「宽带噪声与改前总量差异 < 0.5%」。混合信号实测 6.35%（不达标），
    # 但差异全部来自压在带通下限上的 10 Hz 分量（hanning 在该点就低 10.31%），
    # 不是 rect 泄漏。这里做两个对照把这两件事分开：
    #   E1 同一条噪声去掉 10 Hz 分量 → 只差 300 Hz 分量与白噪
    #   E2 带限噪声（20–900 Hz，两端都离 10/1000 Hz 有裕度）
    from scipy.signal import butter, sosfiltfilt  # 局部 import：只在探针里用

    t8 = np.arange(int(round(8.0 * FS))) / FS

    def mixed(with_10hz: bool) -> np.ndarray:
        gen = np.random.default_rng(20261104)
        data = 0.5 * gen.standard_normal(t8.size) + 0.3 * np.sin(2 * np.pi * 300.0 * t8)
        if with_10hz:
            data = data + 0.05 * np.sin(2 * np.pi * 10.0 * t8)
        return data

    band_limited = mixed(False)
    sos = butter(6, [20.0, 900.0], btype="bandpass", fs=FS, output="sos")
    band_limited = sosfiltfilt(sos, band_limited)
    band_limited = band_limited / float(np.sqrt(np.mean(band_limited**2))) * 0.5

    def pair(data: np.ndarray) -> dict[str, float]:
        rect_mean = float(
            np.mean(
                [
                    v
                    for _o, v in compute_metric_series(
                        data,
                        FS,
                        params={
                            **default_params("v_RMS"),
                            "window_fn": "rect",
                            "window_s": 2.0,
                            "step_s": 2.0,
                        },
                        codes=["v_RMS"],
                    )["v_RMS"]
                ]
            )
        )
        hann_mean = float(
            np.mean(
                [
                    v
                    for _o, v in compute_metric_series(
                        data,
                        FS,
                        params={
                            **default_params("v_RMS"),
                            "window_fn": "hanning",
                            "window_s": 2.0,
                            "step_s": 2.0,
                        },
                        codes=["v_RMS"],
                    )["v_RMS"]
                ]
            )
        )
        return {
            "rect_mm_s": round(rect_mean, 4),
            "hanning_mm_s": round(hann_mean, 4),
            "rect_vs_hanning_pct": round(pct(rect_mean, hann_mean), 3),
        }

    out["leakage_control"] = {
        "note": "同一段 8 s 信号的 2 s 窗均值；差异只可能来自窗型",
        "E1_白噪+300Hz_无10Hz分量": pair(mixed(False)),
        "E1_白噪+300Hz+10Hz分量": pair(mixed(True)),
        "E2_带限噪声_20-900Hz_无带边缘分量": pair(band_limited),
    }

    text = json.dumps(out, ensure_ascii=False, indent=2)
    print(text)
    target = ROOT / "reports" / "2026-11-04-batch-b-probe-vrms.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    print(f"\n已写入 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
