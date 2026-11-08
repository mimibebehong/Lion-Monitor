"""数据分析：① 原始波形 / ② FFT 频谱 / ③ 包络谱（三个页签均已接通）。

紧凑单屏布局：数据源与时间范围合并成一行，参数按行横排，图位压缩，尽量不用上下滚。
① 原始振动曲线按方案 9.12 接通：区间读取 .dat → 超显示上限按最小/最大包络抽样 →
Plotly 折线，CSV 导出始终为原始逐点值。
② FFT 频谱按方案 9.12 接通：窗函数/幅值类型/去均值 → 整段一次 FFT → 单边幅值谱 +
主峰 ● + 目标频率/阶次竖线 + 逐频点 CSV。
③ 包络谱按方案 9.12 接通：巴特沃斯带通（0~fs/2 = 全频带等效不做带通）→ 希尔伯特包络 →
去均值 → FFT → 包络谱 + 主峰 ● + 目标频率/阶次竖线 + 逐频点 CSV。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store_ctx  # noqa: E402
import streamlit as st  # noqa: E402
import ui  # noqa: E402
from ui import page_header, show_flash  # noqa: E402

from lion_monitor.features.envelope import (  # noqa: E402
    load_envelope_spectrum,
)
from lion_monitor.features.spectrum import (  # noqa: E402
    AMPLITUDE_TYPES,
    SPECTRUM_PEAK,
    WINDOW_LABELS,
    load_spectrum,
    order_mark_lines,
)
from lion_monitor.features.waveform import (  # noqa: E402
    DEFAULT_MAX_POINTS,
    WAVE_UNITS,
    load_waveform,
)
from lion_monitor.store import (
    component_freq,  # noqa: E402
    datasets,  # noqa: E402
)
from lion_monitor.viz.sizing import CHART_HEIGHT  # noqa: E402
from lion_monitor.viz.spectrum_plot import spectrum_figure  # noqa: E402
from lion_monitor.viz.waveform_plot import waveform_figure  # noqa: E402

TASK = store_ctx.current_task()
if TASK is None:
    page_header("数据分析", "还没有测试任务，先去「任务列表」新建一个", demo=False)
    st.stop()

with store_ctx.open_session() as session:
    ds_rows = datasets.list_datasets(session, TASK.id)

page_header(
    "数据分析",
    "原始波形 / FFT 频谱 / 包络谱；目标频率与阶次可在图上标记",
    badge=f"{len(ds_rows)} 个数据集",
    demo=False,
)
show_flash()
# 本页要素最多（两行数据源 + 三页签参数 + 图），用紧凑版式把纯留白收掉，
# 保证绘图区在 1280×720 / 1400×800 下都不用向下拖就能看见。
st.markdown(ui.compact_css(), unsafe_allow_html=True)

if not ds_rows:
    st.info("还没有导入数据集：先去「数据接入」导入采集批次", icon="ℹ️")
    st.stop()


#: 幅值谱单位（FFT 页签固定看加速度 g 的幅值谱）
SPECTRUM_UNIT = "g"


def placeholder(text: str, height: int = CHART_HEIGHT) -> None:
    """虚线占位框：本版不画图，只占住位置。"""
    st.markdown(
        f"""<div style="height:{height}px;border:1px dashed #2a3441;border-radius:8px;
        display:flex;align-items:center;justify-content:center;margin:2px 0 4px 0;
        color:#8b949e;font-size:12px">{text}</div>""",
        unsafe_allow_html=True,
    )


def parse_numbers(raw: str) -> list[float]:
    """把「145, 235」这类输入拆成数字列表，非法项忽略。"""
    out: list[float] = []
    for token in raw.replace("，", ",").replace(" ", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(float(token))
        except ValueError:
            continue
    return out


def mark_caption(freq_raw: str, order_raw: str) -> None:
    """目标频率/阶次 → 图上将画在哪。

    阶次换算基准 = 「目标频率」里填的第一个值（手动输入的基准频率），
    即 阶次 n 画在 n × f0。没填目标频率时阶次无法换算。
    """
    freqs = parse_numbers(freq_raw)
    orders = parse_numbers(order_raw)
    bits: list[str] = []
    if freqs:
        bits.append("频率 " + " / ".join(f"{v:g} Hz" for v in freqs))
    if orders and freqs:
        # 每个目标频率都按同一组阶次标（马哥 2026-11-03），同一条频率的阶次同色
        groups = []
        for base in freqs:
            inner = " / ".join(f"{order:g}×={order * base:g}" for order in orders)
            groups.append(f"{base:g} Hz → {inner}")
        bits.append("阶次（Hz）：" + "；".join(groups))
    elif orders:
        bits.append("阶次需要先填目标频率（每个目标频率都按同一组阶次标）")
    st.caption("将标记：" + ("；".join(bits) if bits else "未输入目标频率/阶数"))


#: 特征频率来源文案（``ResolvedFreq.source``）
SOURCE_TEXT = {"node": "节点手填", "object": "对象派生"}


def ledger_mark_labels(
    marks: list[component_freq.ResolvedFreq],
) -> dict[str, component_freq.ResolvedFreq]:
    """台账特征频率 → {界面标签: ResolvedFreq}（标签带值、单位与来源）。"""
    labels: dict[str, component_freq.ResolvedFreq] = {}
    for mark in marks:
        column = component_freq.FREQ_KIND_COLUMNS.get(mark.freq_kind, mark.freq_kind)
        source = SOURCE_TEXT.get(mark.source, mark.source)
        labels[f"{mark.part_code} · {column} {mark.value_hz:g} Hz（{source}）"] = mark
    return labels


def peaks_caption(spectrums) -> str:
    """把前几个峰压成一行（主峰带 ●，其后按幅值降序）。"""
    bits: list[str] = []
    for spectrum in spectrums:
        if not spectrum.peaks:
            continue
        listed = " ｜ ".join(
            ("● " if rank == 0 else f"#{rank + 1} ") + peak.text(spectrum.unit)
            for rank, peak in enumerate(spectrum.peaks)
        )
        bits.append(f"{spectrum.label}：{listed}")
    return "峰：" + "；".join(bits) if bits else ""


# ---------------------------------------------- 数据源 + 时间范围（同一行）
c1, c2, c3, c4 = st.columns([0.36, 0.24, 0.20, 0.20], vertical_alignment="bottom")
ds_labels = {f"{row.code} · {row.collected_at[5:16]}": row for row in ds_rows}
label_a = c1.selectbox("数据集", list(ds_labels), key="ana_ds")
picked_ds = ds_labels[label_a]

with store_ctx.open_session() as session:
    files = datasets.list_files(session, picked_ds.id)
if not files:
    ui.warning("该数据集没有可用通道文件", icon="⚠️")
    st.stop()

picked_file = {f.channel_code: f for f in files}[
    c2.selectbox("通道", [f.channel_code for f in files], key="ana_ch")
]
fs = float(picked_file.sample_rate or 0.0)
points = int(picked_file.points or 0)
duration = points / fs if fs else 0.0

start_s = c3.number_input(
    "开始 (s)", min_value=0.0, max_value=float(duration), value=0.0, step=0.5, key="ana_t0"
)
end_s = c4.number_input(
    "结束 (s)",
    min_value=0.0,
    max_value=float(duration),
    value=round(duration, 1),
    step=0.5,
    key="ana_t1",
)
span = max(0.0, end_s - start_s)


# ---------------------------- 数据集对比行（与上方 A 行同列宽，隐藏标签省高度）
NO_CMP = "— 不对比 —"
cmp_on = False
file_b = None
label_b = ""
span_b = 0.0
b_start = 0.0
b_end = 0.0
b1, b2, b3, b4 = st.columns([0.36, 0.24, 0.20, 0.20], vertical_alignment="center")
label_b = b1.selectbox(
    "数据集 B", [NO_CMP, *ds_labels], key="ana_ds_b", label_visibility="collapsed"
)
cmp_on = label_b != NO_CMP
if not cmp_on:
    b2.caption("↑ 选一个数据集即叠加对比")
else:
    with store_ctx.open_session() as session:
        files_b = datasets.list_files(session, ds_labels[label_b].id)
    if not files_b:
        b2.caption("B 数据集没有可用通道文件")
    else:
        same = {f.channel_code: f for f in files_b}
        default = picked_file.channel_code if picked_file.channel_code in same else next(iter(same))
        ch_b = b2.selectbox(
            "通道 B",
            list(same),
            index=list(same).index(default),
            key="ana_ch_b",
            label_visibility="collapsed",
        )
        file_b = same[ch_b]
        fs_b = float(file_b.sample_rate or 0.0)
        points_b = int(file_b.points or 0)
        dur_b = points_b / fs_b if fs_b else 0.0
        for key, ceiling, seed in (
            ("ana_bt0", dur_b, 0.0),
            ("ana_bt1", dur_b, round(dur_b, 1)),
        ):
            if key in st.session_state and st.session_state[key] > ceiling:
                st.session_state[key] = seed
        b_start = b3.number_input(
            "开始 B (s)",
            min_value=0.0,
            max_value=float(dur_b),
            value=0.0,
            step=0.5,
            key="ana_bt0",
            label_visibility="collapsed",
        )
        b_end = b4.number_input(
            "结束 B (s)",
            min_value=0.0,
            max_value=float(dur_b),
            value=round(dur_b, 1),
            step=0.5,
            key="ana_bt1",
            label_visibility="collapsed",
        )
        span_b = max(0.0, b_end - b_start)

if cmp_on and file_b:
    fs_b = float(file_b.sample_rate or 0.0)
    cmp_note = f" ｜ 对比 {label_b} · {file_b.channel_code} · fs {fs_b:g} Hz · {span_b:.2f} s"
else:
    cmp_note = ""
st.caption(
    f"当前查看：{label_a} · {picked_file.channel_code} · fs {fs:g} Hz · {span:.2f} s{cmp_note}"
)

# 台账特征频率（CF-2）：挂在当前通道上的部件频率，按本批转速派生；未配置时给提示文案
with store_ctx.open_session() as session:
    ledger_marks = component_freq.resolve_freq_marks(
        session, TASK.id, picked_file.channel_code, dataset_id=picked_ds.id
    )
LEDGER = ledger_mark_labels(ledger_marks)
NO_LEDGER_TEXT = "未配置特征频率：去「监测对象 → 频率参数」录入或导入 CSV 后，这里就能直接勾选"

tab_wave, tab_fft, tab_env = st.tabs(
    ["① 原始振动曲线", "② FFT 频谱", "③ 包络谱"], key="analysis_tabs"
)

# ------------------------------------------------------ ① 原始振动曲线
with tab_wave:
    w1, w2, w3 = st.columns([0.22, 0.22, 0.56], vertical_alignment="bottom")
    wave_unit = w1.selectbox("纵轴", list(WAVE_UNITS), key="wave_unit")
    wave_cap = int(
        w2.number_input(
            "显示上限点数",
            min_value=1000,
            max_value=5000000,
            value=int(DEFAULT_MAX_POINTS),
            step=10000,
            key="wave_max_pts",
            help="只影响绘图抽样，导出 CSV 用原值；超出上限时按最小/最大包络抽样"
            "（每箱取最小与最大两点），尖峰不会被跳过",
        )
    )
    w3.caption("超出上限按最小/最大包络抽样；导出用原值")

    # 参数指纹：区间/单位/上限/对比任一变化就回到占位框（方案要求点「运行绘图」才计算）
    wave_key = "|".join(
        [
            picked_file.path,
            f"{start_s:.3f}",
            f"{end_s:.3f}",
            wave_unit,
            str(wave_cap),
            file_b.path if file_b else "",
            f"{b_start:.3f}",
            f"{b_end:.3f}",
        ]
    )

    def _series_of(file_row, label: str, t0: float, t1: float):
        return load_waveform(
            file_row.path, t0, t1, unit=wave_unit, label=f"{label} · {file_row.channel_code}"
        )

    chart, btns = st.columns([0.82, 0.18], vertical_alignment="top")
    with btns:
        if st.button("运行绘图", key="wave_run", width="stretch"):
            try:
                series_list = [_series_of(picked_file, label_a, start_s, end_s)]
                if cmp_on and file_b:
                    series_list.append(_series_of(file_b, label_b, b_start, b_end))
                st.session_state["wave_result"] = {
                    "key": wave_key,
                    "fig": waveform_figure(series_list, cap=wave_cap),
                    "csv": series_list[0].csv_text().encode("utf-8-sig"),
                }
            except ValueError as exc:
                st.session_state["wave_result"] = None
                ui.warning(f"无法绘图：{exc}", icon="⚠️")

        shown = st.session_state.get("wave_result")
        shown = shown if shown and shown["key"] == wave_key else None
        st.download_button(
            "导出 CSV",
            data=shown["csv"] if shown else b"",
            file_name=f"{picked_ds.code}-{picked_file.channel_code}-{start_s:g}~{end_s:g}s.csv",
            mime="text/csv",
            key="wave_csv",
            disabled=shown is None,
            width="stretch",
        )

    with chart:
        if shown:
            st.plotly_chart(shown["fig"], width="stretch", config={"displaylogo": False})
        else:
            placeholder(
                f"原始振动波形 · {picked_file.channel_code} · {start_s:.2f}~{end_s:.2f} s"
                f"{cmp_note}；点「运行绘图」出图"
            )

# ------------------------------------------------------------ ② FFT 频谱
with tab_fft:
    n_fft = int(span * fs)
    df_hz = fs / n_fft if n_fft else 0.0
    f1, f2, f3, f4 = st.columns([0.22, 0.20, 0.16, 0.42], vertical_alignment="bottom")
    win_label = f1.selectbox("窗函数", list(WINDOW_LABELS), index=0, key="fft_win")
    amp_label = f2.selectbox(
        "幅值类型",
        list(AMPLITUDE_TYPES),
        index=list(AMPLITUDE_TYPES).index(SPECTRUM_PEAK),
        key="fft_amp",
        help="峰值 Peak：谱线为该频率正弦分量的幅值；"
        "有效值 RMS = Peak / √2，便于与 v_RMS 这类有效值指标直接比较",
    )
    demean = f3.checkbox(
        "去均值", value=True, key="fft_demean", help="先减掉直流分量，避免 0 Hz 处一个大尖峰"
    )
    f4.caption(
        f"整段一次 FFT：区间 {span:.2f} s ≈ {n_fft:,} 点 → 分辨率 Δf = {df_hz:.4g} Hz"
        f"（0 ~ {fs / 2:g} Hz）"
    )
    with st.expander("显示标记线", expanded=False):
        m1, m2, m3 = st.columns([0.30, 0.20, 0.50], vertical_alignment="bottom")
        target_freq = m1.text_input(
            "目标频率 (Hz)",
            placeholder="25, 145, 235",
            key="fft_freq",
            help="逗号分隔；第一个值同时作为阶次换算的基准频率",
        )
        order_raw = m2.text_input("阶数", placeholder="1, 2, 3.5", key="fft_order")
        with m3:
            mark_caption(target_freq, order_raw)
        if LEDGER:
            picked_ledger = st.multiselect(
                "台账特征频率",
                list(LEDGER),
                key="fft_ledger",
                help="来自「监测对象 → 频率参数」，已按本批转速派生；节点手填覆盖优先",
            )
            if picked_ledger:
                st.caption("台账标记：" + " ｜ ".join(picked_ledger))
        else:
            picked_ledger = []
            st.caption(NO_LEDGER_TEXT)

    target_freqs = parse_numbers(target_freq) + [LEDGER[name].value_hz for name in picked_ledger]
    order_marks = order_mark_lines(parse_numbers(target_freq), parse_numbers(order_raw))
    fft_key = "|".join(
        [
            picked_file.path,
            f"{start_s:.3f}",
            f"{end_s:.3f}",
            win_label,
            amp_label,
            str(demean),
            target_freq,
            order_raw,
            "+".join(f"{LEDGER[name].value_hz:g}" for name in picked_ledger),
            file_b.path if file_b else "",
            f"{b_start:.3f}",
            f"{b_end:.3f}",
        ]
    )

    chart, btns = st.columns([0.82, 0.18], vertical_alignment="top")
    with btns:
        if st.button("频谱分析", key="fft_run", width="stretch"):
            try:
                spectrums = [
                    load_spectrum(
                        picked_file.path,
                        start_s,
                        end_s,
                        label=f"{label_a} · {picked_file.channel_code}",
                        unit=SPECTRUM_UNIT,
                        window=win_label,
                        demean=demean,
                        amplitude_type=amp_label,
                    )
                ]
                if cmp_on and file_b:
                    spectrums.append(
                        load_spectrum(
                            file_b.path,
                            b_start,
                            b_end,
                            label=f"{label_b} · {file_b.channel_code}",
                            unit=SPECTRUM_UNIT,
                            window=win_label,
                            demean=demean,
                            amplitude_type=amp_label,
                        )
                    )
                st.session_state["fft_result"] = {
                    "key": fft_key,
                    "fig": spectrum_figure(
                        spectrums, target_freqs=target_freqs, order_marks=order_marks
                    ),
                    "csv": spectrums[0].csv_text().encode("utf-8-sig"),
                    "caption": peaks_caption(spectrums),
                }
            except ValueError as exc:
                st.session_state["fft_result"] = None
                ui.warning(f"无法做频谱分析：{exc}", icon="⚠️")

        shown_fft = st.session_state.get("fft_result")
        shown_fft = shown_fft if shown_fft and shown_fft["key"] == fft_key else None
        st.download_button(
            "导出 CSV",
            data=shown_fft["csv"] if shown_fft else b"",
            file_name=f"{picked_ds.code}-{picked_file.channel_code}-{start_s:g}~{end_s:g}s-FFT.csv",
            mime="text/csv",
            key="fft_csv",
            disabled=shown_fft is None,
            width="stretch",
        )
        # 峰值明细放右列折叠条：收起只占 28px（右列本来空着一大截，等于不占高度），
        # 图底边才留得住首屏；展开是用户主动动作，主峰在图上已有 ● 标注。
        if shown_fft and shown_fft["caption"]:
            with st.popover("峰值", width="stretch"):
                st.caption(shown_fft["caption"])
    with chart:
        if shown_fft:
            st.plotly_chart(shown_fft["fig"], width="stretch", config={"displaylogo": False})
        else:
            placeholder(f"FFT 幅值谱（主峰 ●，目标频率/阶次画竖线）{cmp_note}；点「频谱分析」出图")

# ----------------------------------------------------------- ③ 包络谱
with tab_env:
    e1, e2, e3 = st.columns([0.38, 0.38, 0.24], vertical_alignment="bottom")
    band_lo = e1.number_input(
        "带通下限 (Hz)",
        min_value=0.0,
        max_value=float(fs / 2 or 1.0),
        value=0.0,
        step=50.0,
        key="env_lo",
    )
    band_hi = e2.number_input(
        "带通上限 (Hz)",
        min_value=0.0,
        max_value=float(fs / 2 or 1.0),
        value=float(fs / 2 or 1.0),
        step=50.0,
        key="env_hi",
        help="默认 fs/2（全频带，等效不做带通）；轴承诊断常用 2~10 kHz 共振带",
    )
    env_demean = e3.checkbox(
        "去均值",
        value=True,
        key="env_demean",
        help="先减掉包络均值，去掉 0 Hz 大尖峰；对 145/290 Hz 这类特征峰幅值无影响",
    )

    with st.expander("显示标记线", expanded=False):
        m1, m2, m3 = st.columns([0.30, 0.20, 0.50], vertical_alignment="bottom")
        env_freq = m1.text_input(
            "目标频率 (Hz)",
            placeholder="29.4, 58.8",
            key="env_freq",
            help="逗号分隔；第一个值同时作为阶次换算的基准频率",
        )
        env_order_raw = m2.text_input("阶数", placeholder="1, 2, 3.5", key="env_order_v")
        with m3:
            mark_caption(env_freq, env_order_raw)
        if LEDGER:
            picked_env_ledger = st.multiselect(
                "台账特征频率",
                list(LEDGER),
                key="env_ledger",
                help="来自「监测对象 → 频率参数」，已按本批转速派生；节点手填覆盖优先",
            )
            if picked_env_ledger:
                st.caption("台账标记：" + " ｜ ".join(picked_env_ledger))
        else:
            picked_env_ledger = []
            st.caption(NO_LEDGER_TEXT)

    env_targets = parse_numbers(env_freq) + [LEDGER[name].value_hz for name in picked_env_ledger]
    env_marks = order_mark_lines(parse_numbers(env_freq), parse_numbers(env_order_raw))
    band_ok = band_hi > band_lo
    if not band_ok:
        ui.warning("带通上限必须大于下限，包络谱不计算", icon="⚠️")

    env_key = "|".join(
        [
            picked_file.path,
            f"{start_s:.3f}",
            f"{end_s:.3f}",
            f"{band_lo:g}",
            f"{band_hi:g}",
            str(env_demean),
            env_freq,
            env_order_raw,
            "+".join(f"{LEDGER[name].value_hz:g}" for name in picked_env_ledger),
            file_b.path if file_b else "",
            f"{b_start:.3f}",
            f"{b_end:.3f}",
        ]
    )

    chart, btns = st.columns([0.82, 0.18], vertical_alignment="top")
    with btns:
        if st.button(
            "运行包络谱",
            key="env_run",
            disabled=not band_ok,
            width="stretch",
        ):
            try:
                envelopes = [
                    load_envelope_spectrum(
                        picked_file.path,
                        start_s,
                        end_s,
                        label=f"{label_a} · {picked_file.channel_code}",
                        unit=SPECTRUM_UNIT,
                        band_low_hz=band_lo,
                        band_high_hz=band_hi,
                        demean=env_demean,
                    )
                ]
                if cmp_on and file_b:
                    envelopes.append(
                        load_envelope_spectrum(
                            file_b.path,
                            b_start,
                            b_end,
                            label=f"{label_b} · {file_b.channel_code}",
                            unit=SPECTRUM_UNIT,
                            band_low_hz=band_lo,
                            band_high_hz=band_hi,
                            demean=env_demean,
                        )
                    )
                st.session_state["env_result"] = {
                    "key": env_key,
                    "fig": spectrum_figure(
                        envelopes, target_freqs=env_targets, order_marks=env_marks
                    ),
                    "csv": envelopes[0].csv_text().encode("utf-8-sig"),
                    "caption": peaks_caption(envelopes),
                }
            except ValueError as exc:
                st.session_state["env_result"] = None
                ui.warning(f"无法做包络谱分析：{exc}", icon="⚠️")

        shown_env = st.session_state.get("env_result")
        shown_env = shown_env if shown_env and shown_env["key"] == env_key else None
        st.download_button(
            "导出 CSV",
            data=shown_env["csv"] if shown_env else b"",
            file_name=(
                f"{picked_ds.code}-{picked_file.channel_code}-{start_s:g}~{end_s:g}s-包络谱.csv"
            ),
            mime="text/csv",
            key="env_csv",
            disabled=shown_env is None,
            width="stretch",
        )
        if shown_env and shown_env["caption"]:
            with st.popover("峰值", width="stretch"):
                st.caption(shown_env["caption"])
    with chart:
        if shown_env:
            st.plotly_chart(shown_env["fig"], width="stretch", config={"displaylogo": False})
        else:
            placeholder(
                f"包络谱 · 带通 {band_lo:g}~{band_hi:g} Hz · 主峰 ●，目标频率/阶次画竖线"
                f"{cmp_note}；点「运行包络谱」出图"
            )
