"""现场四通道数据端到端实跑（2026-10-31 复查用）。

链路：扫描 → 导入（算指标）→ 指标值 → 趋势 → 健康评级 → 数据分析页素材（原始波形 / FFT / 包络谱）
      → 导出模板 v2 → 导入到新任务 → 页面 AppTest（健康树 / 趋势 / 数据分析）

数据：/mnt/d/hermes_wp/Project/锂电产线/envision/data/四通道保存（3 个四通道批次）
已知输入：0.80 gpk@145Hz / 1.05 gpk@172Hz / 2.20 gpk@235Hz，传感器 500 mV/g

只写临时库（~/workspace/temp/<日期>/），不碰 data/lion.db。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "app"))

from lion_monitor.features.envelope import load_envelope_spectrum  # noqa: E402
from lion_monitor.features.health import node_health  # noqa: E402
from lion_monitor.features.metrics import compute_metrics  # noqa: E402
from lion_monitor.features.overview import (  # noqa: E402
    health_counts,
    step_states,
    task_overview,
)
from lion_monitor.features.pipeline import import_batch, metric_plan  # noqa: E402
from lion_monitor.features.spectrum import load_spectrum  # noqa: E402
from lion_monitor.features.waveform import UNIT_ACCEL, load_waveform  # noqa: E402
from lion_monitor.ingest.vibration import read_vibration, scan_vibration_dir  # noqa: E402
from lion_monitor.store import (  # noqa: E402
    channel_reg,
    datasets,
    limits,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store.db import init_db, make_engine, make_session_factory  # noqa: E402

DATA_ROOT = Path("/mnt/d/hermes_wp/Project/锂电产线/envision/data/四通道保存")
KNOWN = {  # 批次目录名 → (输入 gpk, 输入频率 Hz)
    "2026-10-29 15_28_34": (0.80, 145.0),
    "2026-10-29 16_03_13": (1.05, 172.0),
    "2026-10-29 16_10_44": (2.20, 235.0),
}
SENSOR_MV_PER_G = 500.0
BANDWIDTH = 2000.0  # 让 v_RMS 覆盖到全频段，便于与 10–1000 对比
CODES = ("a_Peak", "a_RMS", "v_RMS")
WORK = Path.home() / "workspace" / "temp" / datetime.now().strftime("%Y-%m-%d")
OUT = ROOT / "reports" / "2026-10-31-field-run-output.txt"

lines: list[str] = []


def say(text: str = "") -> None:
    print(text)
    lines.append(text)


def kv(label: str, value: Any) -> None:
    say(f"　{label}：{value}")


def build_task(session, name: str) -> tuple[Any, dict[str, Any]]:
    """建任务 + 台账（1 对象 4 通道 + 内置三指标）+ 健康树（4 通道 × 3 指标）+ 默认阈值。"""
    task = tasks.create_task(session, name=name, operator="mark")
    obj = object_registry.create_object(session, task.id, "隔膜过辊轴承", "轴承", part="隔膜过辊")
    channels = {
        code: channel_reg.create_channel(session, task.id, obj.id, code)
        for code in ("CH1", "CH2", "CH3", "CH4")
    }
    metric_def.seed_defaults(session, task.id)
    defs = {row.code: row for row in metric_def.list_metrics(session, task.id)}

    root = tree.root_node(session, task.id)
    device = tree.create_node(
        session, task.id, "device", "卷绕产线", parent_id=root.id, attrs={"工位": "WL-1"}
    )
    comp = tree.create_node(
        session,
        task.id,
        "component",
        "隔膜过辊轴承",
        parent_id=device.id,
        attrs={"类型": "轴承", "部位": "隔膜过辊"},
        ref_id=obj.id,
    )
    metric_nodes: dict[tuple[str, str], int] = {}
    for code, row in channels.items():
        ch_node = tree.create_node(
            session, task.id, "channel", code, parent_id=comp.id, ref_id=row.id
        )
        for metric in CODES:
            node = tree.create_node(
                session,
                task.id,
                "metric",
                metric,
                parent_id=ch_node.id,
                attrs={"code": metric, "单位": defs[metric].unit},
                ref_id=defs[metric].id,
            )
            metric_nodes[(code, metric)] = node.id
            metric_assign_params = {"band_high": BANDWIDTH} if metric == "v_RMS" else {}
            metric_assign.assign_metric(
                session, task.id, ch_node.id, metric, params=metric_assign_params
            )
        # 阈值挂在指标节点上（阈值设置页的挂载口径）
        limits.set_limit(session, task.id, metric_nodes[(code, "v_RMS")], "v_RMS", 4.5, 11.2)
        limits.set_limit(session, task.id, metric_nodes[(code, "a_Peak")], "a_Peak", 1.5, 3.0)
        limits.set_limit(session, task.id, metric_nodes[(code, "a_RMS")], "a_RMS", 1.2, 2.0)
    return task, {"object": obj, "channels": channels, "defs": defs, "metric_nodes": metric_nodes}


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    db_file = WORK / "field-run.db"
    if db_file.exists():
        db_file.unlink()
    engine = make_engine(db_file)
    init_db(engine)
    session = make_session_factory(engine)()

    say("=" * 100)
    say(f"Lion-Monitor 现场四通道数据端到端实跑　{datetime.now():%Y-%m-%d %H:%M:%S}")
    say(f"数据目录：{DATA_ROOT}")
    say(f"临时库：{db_file}")
    say("=" * 100)

    # ---------------- 0. 建两个任务（导入目标任务先建，好让现场任务成为「最新任务」）
    target_task, target_refs = build_task(session, "复查导入任务")

    # ---------------- 1. 现场任务 + 健康树
    say("")
    say("[1] 现场任务与健康树")
    task, refs = build_task(session, "卷绕产线振动监测")
    nodes = tree.list_nodes(session, task.id)
    kinds: dict[str, int] = {}
    for row in nodes:
        kinds[row.node_type] = kinds.get(row.node_type, 0) + 1
    kv("任务号", task.code)
    kv("节点", f"{len(nodes)} 个 {kinds}")
    for row in metric_plan(session, task.id):
        kv(f"通道 {row.channel_code}", f"{row.object_name} → 指标 {' / '.join(row.metric_codes)}")

    # ---------------- 2. 扫描
    say("")
    say("[2] 扫描采集目录（只读头部）")
    batches: list[tuple[str, Path, list]] = []
    for sub in sorted(p for p in DATA_ROOT.iterdir() if p.is_dir()):
        metas = scan_vibration_dir(sub)
        if not metas:
            continue
        batches.append((sub.name, sub, metas))
        pk, f0 = KNOWN.get(sub.name, (float("nan"), float("nan")))
        kv(
            sub.name,
            f"{len(metas)} 文件 · 通道 {'/'.join(sorted({m.channel for m in metas}))} · "
            f"fs {metas[0].sample_rate_hz:g} Hz · {metas[0].sample_count} 点 · "
            f"{metas[0].duration_s:.2f} s · 已知输入 {pk} gpk @ {f0:g} Hz",
        )
    if len(batches) != 3:
        raise SystemExit(f"批次数量不对：{len(batches)}")

    channel_nodes = {row.name: row.id for row in nodes if row.node_type == "channel"}

    # ---------------- 3. 导入（含指标计算）
    say("")
    say("[3] 导入三批次并算指标（import_from_dir → metric_jobs → compute_metrics → upsert）")
    results = []
    for name, path, _metas in batches:
        result = import_batch(session, task.id, path, channel_nodes, compute=True)
        results.append((name, result))
        kv(
            name,
            f"数据集 {result.dataset_id} · 状态 {result.status} · "
            f"有效文件 {result.files_ok}/{result.channels} · "
            f"指标值 {result.metrics_written} 行 · 真算 {result.compute_runs} 次 / "
            f"复用 {result.dedupe_hits} 次 · 跳过 {len(result.skipped_metrics or [])} 个",
        )
        if result.files_bad:
            kv(
                "  坏文件",
                "；".join(
                    f"{item.filename}（{'损坏' if item.damaged else '无效'}：{item.reason}）"
                    for item in result.files_bad
                ),
            )

    # ---------------- 4. 指标值（批次 × 通道）
    say("")
    say("[4] 指标值：三批次 × 四通道（取各数据集自己的 channel 行）")
    ds_rows = {row.collected_at: row for row in datasets.list_datasets(session, task.id)}
    say(f"　数据集：{' / '.join(f'{row.code}={row.collected_at}' for row in ds_rows.values())}")

    value_table: dict[tuple[str, str, str], float] = {}
    for name, result in results:
        codes = [f.channel_code for f in datasets.list_files(session, result.dataset_id)]
        for code in ("CH1", "CH2", "CH3", "CH4"):
            if code not in codes:
                continue
            for metric in CODES:
                rows = [
                    row
                    for row in metric_store.list_values(session, task.id, code, metric)
                    if row.dataset_id == result.dataset_id
                ]
                if rows:
                    value_table[(name, code, metric)] = rows[-1].value

    header = f"{'批次':<22}{'通道':<6}" + "".join(f"{m:>14}" for m in CODES)
    say(header)
    for name, _result in results:
        for code in ("CH1", "CH2", "CH3", "CH4"):
            cells = "".join(
                f"{value_table.get((name, code, m), float('nan')):>14.5f}" for m in CODES
            )
            say(f"{name:<22}{code:<6}{cells}")

    # ---------------- 5. 趋势
    say("")
    say("[5] 趋势（trend_series，每批次一个点）")
    for code in ("CH2",):
        for metric in CODES:
            series = metric_store.trend_series(session, task.id, code, metric)
            kv(
                f"{code} · {metric}",
                f"{len(series)} 点："
                + " → ".join(f"{value:.5f}@{when[5:16]}" for when, value in series),
            )

    # ---------------- 6. 健康评级
    say("")
    say("[6] 健康评级（值 vs 继承阈值）")
    health = node_health(session, task.id)
    by_id = {row.id: row for row in tree.list_nodes(session, task.id)}
    for node_id, item in health.items():
        node = by_id[node_id]
        if node.node_type not in ("device", "component", "channel"):
            continue
        kv(
            f"{node.type_text} {node.name}",
            f"{item.state} · 最新 {item.latest_value if item.latest_value is not None else '—'}"
            f"{item.unit} · 超限 {item.over_count} 条",
        )
    kv("health_counts", health_counts(session, task.id, health))
    kv("step_states", step_states(session, task.id))
    overview = task_overview(session, task.id)
    kv(
        "task_overview",
        f"任务 {overview.task_count} · 数据集 {overview.dataset_count}"
        f" · 通道 {overview.channel_count} · 指标值 {overview.metric_value_count}"
        f" · 越限点 {overview.over_count} · 健康分布 {overview.health_counts}",
    )
    for dev in overview.devices:
        kv(
            f"  设备 {dev.name}",
            f"{dev.state} · 对象 {dev.object_count} · 通道 {dev.channel_count}"
            f" · 指标 {dev.metric_count} · 越限指标 {dev.over_metric_count}",
        )

    # ---------------- 7. 数据分析页素材：原始波形 / FFT / 包络谱
    say("")
    say("[7] 数据分析页素材（原始波形 / FFT 主峰 / 包络谱主峰），每批次 CH2（激励通道）")
    for name, _result in results:
        path = DATA_ROOT / name / f"{name}-CH2_Task1.dat"
        pk, f0 = KNOWN[name]
        wave = load_waveform(path, 0.0, 20.0, unit=UNIT_ACCEL, label=name)
        spectrum = load_spectrum(path, 0.0, 20.0, label=name, unit="g")
        env = load_envelope_spectrum(path, 0.0, 20.0, label=name, unit="g")
        record = read_vibration(path)
        x = record.samples.astype(np.float64)
        x = x - x.mean()
        crest = float(np.abs(x).max() / np.sqrt(np.mean(x**2)))
        p999 = float(np.percentile(np.abs(x), 99.9))
        fft_main = spectrum.main_peak()
        env_main = env.main_peak()
        kv(
            name,
            f"波形 {wave.point_count} 点（{wave.start_s:g} s 起，fs {wave.sample_rate:g} Hz）· "
            f"FFT 主峰 {fft_main.text('g') if fft_main else '—'} · 目标 {f0:g} Hz · "
            f"包络谱主峰 {env_main.text('g') if env_main else '—'}",
        )
        kv(
            "  诊断",
            f"a_Peak {float(np.abs(x).max()):.5f} g / 99.9% 分位 {p999:.5f} g"
            f"（比 {np.abs(x).max() / p999:.2f}×）· 波峰因数 {crest:.3f}"
            f" · 已知输入 {pk} gpk 偏差 {(float(np.abs(x).max()) / pk - 1) * 100:+.1f}%",
        )
        top = " ｜ ".join(peak.text("g") for peak in spectrum.peaks[:4])
        kv("  FFT 前四峰", top)
        kv(
            "  包络谱前四峰",
            " ｜ ".join(peak.text("g") for peak in env.peaks[:4]) or "—",
        )

    # ---------------- 8. 带宽对比：v_RMS 10–1000 vs 10–2000 vs 10–500
    say("")
    say("[8] v_RMS 带通对比（同一段 20 s 原始数据，CH2）")
    say(f"　{'批次':<22}{'10–1000':>12}{'10–2000':>12}{'10–500':>12}{'1000/2000 差':>14}")
    for name, _result in results:
        path = DATA_ROOT / name / f"{name}-CH2_Task1.dat"
        record = read_vibration(path)
        values = {}
        for low, high in ((10.0, 1000.0), (10.0, 2000.0), (10.0, 500.0)):
            params = {"band_low": low, "band_high": high}
            values[(low, high)] = compute_metrics(
                record.samples, record.meta.sample_rate_hz, params=params, codes=["v_RMS"]
            )["v_RMS"]
        ratio = values[(10.0, 1000.0)] / values[(10.0, 2000.0)]
        say(
            f"{name:<22}{values[(10.0, 1000.0)]:>12.5f}{values[(10.0, 2000.0)]:>12.5f}"
            f"{values[(10.0, 500.0)]:>12.5f}{(ratio - 1) * 100:>13.3f}%"
        )

    # ---------------- 9. 模板 v2 导出 → 导入新任务
    say("")
    say("[9] 模板 v2 导出 → 导入「复查导入任务」")
    payload = tree.export_template(session, task.id)
    preview = tree.preview_import(session, target_task.id, payload)
    kv(
        "预览",
        f"tree v{preview.version} · 树节点 {preview.node_total}"
        f" · 指标绑定 {preview.metric_bindings} · 阈值 {preview.threshold_rows}"
        f" · 未匹配 {len(preview.unmatched)}",
    )
    report = tree.import_template(session, target_task.id, payload)
    kv(
        "报告",
        f"新建节点 {report.node_total} · 绑定指标 {report.metrics_bound}"
        f" · 写阈值 {report.thresholds_written} · 跳过 {len(report.skipped)}",
    )
    src_outline = [
        (row.node_type, row.path.count("/"), row.name) for row in tree.list_nodes(session, task.id)
    ]
    dst_outline = [
        (row.node_type, row.path.count("/"), row.name)
        for row in tree.list_nodes(session, target_task.id)
    ]
    kv("结构与源任务一致", src_outline == dst_outline)
    kv("目标任务阈值条数", len(limits.list_limits(session, target_task.id)))

    # ---------------- 10. 页面 AppTest
    say("")
    say("[10] 页面实跑（Streamlit AppTest，库指向本次临时库）")
    os.environ["LION_DB"] = str(db_file)
    import store_ctx
    from streamlit.testing.v1 import AppTest

    store_ctx._session_factory.clear()
    pages = {
        "健康树配置": ROOT / "app" / "pages" / "health_tree.py",
        "趋势分析": ROOT / "app" / "pages" / "trends.py",
        "数据分析": ROOT / "app" / "pages" / "channel_analysis.py",
        "数据接入": ROOT / "app" / "pages" / "ingest.py",
    }
    for label, page in pages.items():
        at = AppTest.from_file(str(page), default_timeout=180)
        at.session_state["current_task_id"] = task.id
        at.run()
        errors = [str(item) for item in at.exception]
        kv(label, f"异常 {len(errors)} 个" + (f"：{errors}" if errors else " ✅"))
        if label == "健康树配置":
            kv(
                "  勾选/按钮",
                "包含阈值配置="
                f"{next((c.value for c in at.checkbox if c.label == '包含阈值配置'), None)}",
            )
        if label == "趋势分析":
            texts = [item.value for item in at.markdown]
            kv("  区块", " / ".join(text[:28] for text in texts[-4:]))
        if label == "数据分析":
            ds_options = list(at.selectbox(key="ana_ds").options)
            kv("  数据集", " / ".join(ds_options))
            at.selectbox(key="ana_ds").select(ds_options[-1]).run()
            at.selectbox(key="ana_ch").select("CH2").run()
            at.button(key="wave_run").click().run()
            wave_note = [c.value for c in at.caption if "当前查看" in c.value]
            kv("  波形页", wave_note[-1] if wave_note else "（无 caption）")
            at.button(key="fft_run").click().run()
            peak_caps = [c.value for c in at.caption if c.value.startswith("峰：")]
            kv("  FFT 峰", peak_caps[-1] if peak_caps else "（无）")
            at.button(key="env_run").click().run()
            env_caps = [c.value for c in at.caption if c.value.startswith("峰：")]
            kv("  包络谱峰", env_caps[-1] if env_caps else "（无）")
            kv("  页内异常", [str(item) for item in at.exception] or "无")
    store_ctx._session_factory.clear()

    say("")
    say("=" * 100)
    say(f"输出结束。临时库：{db_file}")
    say("=" * 100)
    session.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n已写：{OUT}")


if __name__ == "__main__":
    main()
