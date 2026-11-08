"""振动数据的概览绘图。

四个子图：全时波形 / 峰值附近局部放大 / 幅值谱 / STFT 时频图。

刻意不使用 ``pyplot``：直接构造 ``Figure`` + ``FigureCanvasAgg``，
这样库调用不会污染全局状态，也不依赖运行环境是否有显示后端。
"""

from __future__ import annotations

from pathlib import Path

import matplotlib as mpl
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from ..ingest import VibrationRecord

__all__ = [
    "amplitude_spectrum",
    "plot_vibration_overview",
    "save_vibration_overview",
    "stft_db",
    "top_peaks",
]

#: 中文字体候选（Noto CJK 在本机已安装），末尾保留 DejaVu 兜底
_FONT_CANDIDATES = [
    "Noto Sans CJK SC",
    "Noto Serif CJK SC",
    "WenQuanYi Zen Hei",
    "DejaVu Sans",
]
mpl.rcParams["font.sans-serif"] = _FONT_CANDIDATES
mpl.rcParams["axes.unicode_minus"] = False


def amplitude_spectrum(samples: np.ndarray, sample_rate_hz: float) -> tuple[np.ndarray, np.ndarray]:
    """单边幅值谱：返回 (频率 Hz, 幅值)，幅值已按 2/N 归一化（直流分量不翻倍）。"""
    x = np.asarray(samples, dtype=np.float64)
    n = x.size
    if n == 0:
        raise ValueError("样本为空，无法计算幅值谱")
    spectrum = np.fft.rfft(x - x.mean())
    amplitude = np.abs(spectrum) * 2.0 / n
    amplitude[0] /= 2.0
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate_hz)
    return freqs, amplitude


def stft_db(
    samples: np.ndarray,
    sample_rate_hz: float,
    *,
    nperseg: int = 1024,
    overlap: float = 0.5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """短时傅里叶变换，返回 (时间 s, 频率 Hz, 幅值 dB)，dB 以段内最大值为 0 dB 参考。"""
    x = np.asarray(samples, dtype=np.float64)
    n = x.size
    if n < nperseg:
        raise ValueError(f"样本数 {n} 少于每段长度 {nperseg}")
    step = max(1, int(nperseg * (1.0 - overlap)))
    window = np.hanning(nperseg)
    starts = range(0, n - nperseg + 1, step)
    frames = np.stack([x[s : s + nperseg] * window for s in starts])
    mags = np.abs(np.fft.rfft(frames, axis=1))
    ref = mags.max() if mags.max() > 0 else 1.0
    db = 20.0 * np.log10(np.maximum(mags / ref, 1e-12))
    times = (np.arange(frames.shape[0]) * step + nperseg / 2) / sample_rate_hz
    freqs = np.fft.rfftfreq(nperseg, d=1.0 / sample_rate_hz)
    return times, freqs, db.T


def top_peaks(
    freqs: np.ndarray,
    amp: np.ndarray,
    *,
    count: int = 3,
    min_gap_hz: float = 20.0,
) -> list[tuple[float, float]]:
    """按幅值从大到小挑出峰，返回 [(频率 Hz, 幅值), ...]。

    忽略直流分量；两个峰的间隔不小于 ``min_gap_hz``，避免把同一主瓣采成多个峰。
    """
    freqs = np.asarray(freqs, dtype=np.float64)
    amp = np.asarray(amp, dtype=np.float64)
    if freqs.size != amp.size:
        raise ValueError(f"频率与幅值长度不一致: {freqs.size} vs {amp.size}")
    if freqs.size < 2:
        return []
    picked: list[int] = []
    for idx in np.argsort(amp[1:])[::-1] + 1:
        f = float(freqs[idx])
        if all(abs(f - float(freqs[j])) >= min_gap_hz for j in picked):
            picked.append(int(idx))
            if len(picked) >= count:
                break
    return [(float(freqs[i]), float(amp[i])) for i in picked]


def _peak_envelope(
    x: np.ndarray, *, buckets: int = 1400
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """按桶求峰-峰包络与 RMS，返回 (桶中心索引, 下包络, 上包络, RMS)。

    高采样率时逐点连线会糊成实心块，包络才能在固定像素宽度里看清幅值变化。
    """
    n = x.size
    b = int(min(max(buckets, 2), n))
    edges = np.unique(np.linspace(0, n, b + 1).astype(int))
    lo = np.empty(edges.size - 1, dtype=np.float64)
    hi = np.empty(edges.size - 1, dtype=np.float64)
    rms = np.empty(edges.size - 1, dtype=np.float64)
    for i in range(edges.size - 1):
        seg = x[edges[i] : edges[i + 1]]
        lo[i] = seg.min()
        hi[i] = seg.max()
        rms[i] = float(np.sqrt(np.mean(np.square(seg, dtype=np.float64))))
    centers = ((edges[:-1] + edges[1:]) // 2).astype(int)
    return centers, lo, hi, rms


def plot_vibration_overview(
    record: VibrationRecord,
    *,
    zoom_s: float = 0.05,
    nperseg: int = 1024,
) -> Figure:
    """绘制振动记录的四联概览图，返回 Figure（调用方负责保存或显示）。"""
    meta = record.meta
    x = np.asarray(record.samples, dtype=np.float64)
    fs = meta.sample_rate_hz
    t = record.time_axis()

    fig = Figure(figsize=(12.0, 11.0), constrained_layout=True)
    FigureCanvasAgg(fig)
    gs = fig.add_gridspec(4, 1, height_ratios=[1.0, 1.0, 1.1, 1.4])

    # 1) 全时波形（峰值包络：32 kHz 原始点直接连线会糊成实心块）
    zoom_n = max(2, int(zoom_s * fs))
    peak_idx = int(np.argmax(np.abs(x)))
    start = max(0, min(peak_idx - zoom_n // 2, x.size - zoom_n))
    sl = slice(start, start + zoom_n)
    centers, lo, hi, bucket_rms = _peak_envelope(x, buckets=1400)
    ax = fig.add_subplot(gs[0])
    ax.fill_between(t[centers], lo, hi, color="#1f4e79", linewidth=0, label="峰-峰包络")
    ax.plot(t[centers], bucket_rms, color="#ffb300", linewidth=0.8, label="RMS")
    ax.plot(t[centers], -bucket_rms, color="#ffb300", linewidth=0.8)
    ax.axvspan(t[start], t[start + zoom_n - 1], color="#ff8f00", alpha=0.35, label="放大区间")
    ax.set_title("全时波形（峰值包络）", loc="left")
    ax.set_xlabel("时间 (s)")
    ax.set_ylabel("幅值")
    ax.legend(loc="upper right", fontsize=8, framealpha=0.9)

    # 2) 峰值附近局部放大
    ax = fig.add_subplot(gs[1])
    ax.plot(t[sl], x[sl], linewidth=0.9, color="#c00000")
    ax.set_title(f"局部放大（{zoom_s * 1000:.0f} ms，峰值 {abs(x[peak_idx]):.3f} 处）", loc="left")
    ax.set_xlabel("时间 (s)")
    ax.set_ylabel("幅值")

    # 3) 幅值谱
    freqs, amp = amplitude_spectrum(x, fs)
    ax = fig.add_subplot(gs[2])
    floor = max(float(amp.max()) * 1e-4, 1e-12)
    ax.semilogy(freqs, np.maximum(amp, floor), linewidth=0.7, color="#2e7d32")
    ax.set_title("幅值谱（前 3 个峰值已标注）", loc="left")
    ax.set_xlabel("频率 (Hz)")
    ax.set_ylabel("幅值")
    ax.set_xlim(0, fs / 2)
    ax.set_ylim(floor, float(amp.max()) * 15)
    for rank, (peak_f, peak_a) in enumerate(top_peaks(freqs, amp, count=3), start=1):
        ax.axvline(peak_f, color="#999999", linestyle="--", linewidth=0.7)
        ax.annotate(
            f"#{rank} {peak_f:.0f} Hz / {peak_a:.2e}",
            xy=(peak_f, peak_a),
            xytext=(8 if rank % 2 else -70, 10),
            textcoords="offset points",
            fontsize=8,
            color="#333333",
            bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "none", "alpha": 0.8},
        )

    # 4) 时频图
    times, s_freqs, db = stft_db(x, fs, nperseg=nperseg)
    ax = fig.add_subplot(gs[3])
    mesh = ax.pcolormesh(times, s_freqs, db, cmap="magma", shading="auto", vmin=-60, vmax=0)
    ax.set_title(f"时频图 STFT（段长 {nperseg}，50% 重叠）", loc="left")
    ax.set_xlabel("时间 (s)")
    ax.set_ylabel("频率 (Hz)")
    fig.colorbar(mesh, ax=ax, label="相对幅值 (dB)")

    fig.suptitle(
        f"{meta.path.name}    通道 {meta.channel}    {fs / 1000:g} kHz    "
        f"{meta.sample_count} 点 / {meta.duration_s:g} s    "
        f"{meta.started_at:%Y-%m-%d %H:%M:%S}",
        fontsize=11,
    )
    return fig


def save_vibration_overview(
    record: VibrationRecord,
    path: str | Path,
    *,
    dpi: int = 130,
    **kwargs: object,
) -> Path:
    """绘制并保存概览图，返回保存路径。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig = plot_vibration_overview(record, **kwargs)  # type: ignore[arg-type]
    fig.savefig(path, dpi=dpi, facecolor="white")
    fig.clear()
    return path
