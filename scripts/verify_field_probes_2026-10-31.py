"""现场实跑复查用的补充探针（针对实跑里值得记录的现象取证）。

只读现场数据 + 复用实跑留下的临时库（~/workspace/temp/<日期>/field-run.db）。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.features.metrics import compute_metrics  # noqa: E402
from lion_monitor.features.pipeline import import_batch  # noqa: E402
from lion_monitor.ingest.vibration import read_vibration  # noqa: E402
from lion_monitor.store import datasets, limits, metric_assign, tree  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store.db import make_engine, make_session_factory  # noqa: E402

DATA = Path("/mnt/d/hermes_wp/Project/锂电产线/envision/data/四通道保存")
DB = Path.home() / "workspace" / "temp" / datetime.now().strftime("%Y-%m-%d") / "field-run.db"
OUT = ROOT / "reports" / "2026-10-31-field-run-probes.txt"

lines: list[str] = []


def say(text: str = "") -> None:
    print(text)
    lines.append(text)


def main() -> None:
    session = make_session_factory(make_engine(DB))()
    task = next(
        row
        for row in __import__("lion_monitor.store.tasks", fromlist=["x"]).list_tasks(session)
        if row.name == "卷绕产线振动监测"
    )
    say("=" * 90)
    say("现场实跑补充探针")
    say("=" * 90)

    # ---------------- P1 振幅随时间的漂移（解释 a_Peak 与谱峰幅值的差异）
    say("")
    say("[P1] 20 s 内每 1 s 的峰值 / RMS（CH2），看幅值是否恒定")
    for name in sorted(p.name for p in DATA.iterdir() if p.is_dir()):
        path = DATA / name / f"{name}-CH2_Task1.dat"
        record = read_vibration(path)
        x = record.samples.astype(np.float64)
        fs = record.meta.sample_rate_hz
        n = int(fs)
        rows = []
        for i in range(0, x.size - n + 1, n):
            seg = x[i : i + n] - x[i : i + n].mean()
            rows.append((float(np.abs(seg).max()), float(np.sqrt(np.mean(seg**2)))))
        peaks = np.array([row[0] for row in rows])
        rmss = np.array([row[1] for row in rows])
        say(
            f"　{name}：峰值 1s 均值 {peaks.mean():.4f} g"
            f"（最小 {peaks.min():.4f} / 最大 {peaks.max():.4f}，"
            f"起伏 {(peaks.max() / peaks.min() - 1) * 100:.1f}%）"
            f"· RMS 1s 均值 {rmss.mean():.4f} g（起伏 {(rmss.max() / rmss.min() - 1) * 100:.1f}%）"
        )
        say("    峰值序列：" + " ".join(f"{value:.3f}" for value in peaks))

    # ---------------- P2 重导同一批次：数据集与指标值会不会翻倍
    say("")
    say("[P2] 重导同一批次（10-29 16_10_44）：数据集数 / 指标值行数")
    channel_nodes = {
        row.name: row.id for row in tree.list_nodes(session, task.id) if row.node_type == "channel"
    }
    before = (len(datasets.list_datasets(session, task.id)), _value_count(session, task.id))
    result = import_batch(
        session, task.id, DATA / "2026-10-29 16_10_44", channel_nodes, compute=True
    )
    after = (len(datasets.list_datasets(session, task.id)), _value_count(session, task.id))
    say(
        f"　重导前 数据集 {before[0]} 个 / 指标值 {before[1]} 行"
        f" → 重导后 {after[0]} 个 / {after[1]} 行"
    )
    say(
        f"　本次结果：数据集 {result.dataset_id}（{result.status}，{result.metrics_written} 行，"
        f"真算 {result.compute_runs} 次）"
    )

    # ---------------- P3 阈值「越小越差」写入：limits.set_limit 的方向校验
    say("")
    say("[P3] 阈值方向『<』（越小越差）写入路径")
    nodes = {row.name: row for row in tree.list_nodes(session, task.id)}
    metric_node = next(
        row
        for row in tree.list_nodes(session, task.id)
        if row.node_type == "metric" and row.name == "a_Peak"
    )
    try:
        limits.set_limit(
            session, task.id, metric_node.id, "a_Peak", warn=0.5, alert=0.2, direction="<"
        )
    except Exception as exc:  # noqa: BLE001 —— 这里就是要记录异常类型与文案
        say(
            f"　warn=0.5 / alert=0.2（< 方向，页面口径要求 warn>alert）"
            f"→ {type(exc).__name__}: {exc}"
        )
    else:
        say("　写入成功（warn=0.5 > alert=0.2，direction='<'）")
    stored = limits.resolve_limit(session, task.id, metric_node.id, "a_Peak")
    say(f"　该节点现有阈值（继承解析）：{stored}")
    say(
        f"　健康判级用 point_level(0.1, {stored}) = "
        f"{__import__('lion_monitor.features.health', fromlist=['x']).point_level(0.1, stored)}"
    )

    # ---------------- P4 参数改了但值不重算（S4 未做）
    say("")
    say("[P4] 把 CH2·v_RMS 的 band_high 从 2000 改成 500：历史值是否被重算")
    ch2_node = nodes["CH2"]
    assign = next(
        row
        for row in metric_assign.list_assignments(session, task.id)
        if row.node_id == ch2_node.id and row.code == "v_RMS"
    )
    before_value = metric_store.list_values(session, task.id, "CH2", "v_RMS")[-1].value
    marked = metric_assign.update_params(session, assign.id, {"band_high": 500})
    rows = metric_store.list_values(session, task.id, "CH2", "v_RMS")
    say(
        f"　改参数前最新值 {before_value:.5f} mm/s → 改参数后最新值 {rows[-1].value:.5f} mm/s"
        f"（stale={rows[-1].stale}，参数指纹 {rows[-1].params_hash}）"
    )
    say(
        f"　新生效参数 band_high={metric_assign.effective_params(session, marked.id)['band_high']}"
        f"，但历史值未重算（S4 切窗/重算链路未做）"
    )
    metric_assign.update_params(session, assign.id, {"band_high": 2000})

    # ---------------- P5 指标值快照字段是否落库
    say("")
    say("[P5] metric_value 的参数快照与转速字段")
    sample = metric_store.list_values(session, task.id, "CH2", "v_RMS")[-1]
    say(
        f"　CH2·v_RMS 最新行：value={sample.value:.5f} {sample.unit}"
        f" · params_hash={sample.params_hash}"
        f" · rpm_used={sample.rpm_used} · rpm_source={sample.rpm_source!r} · stale={sample.stale}"
        f" · node_id={sample.node_id}"
    )

    # ---------------- P6 合成正弦回归：算法本身的量级对不对
    say("")
    say("[P6] 合成正弦回归（排除现场数据噪声，验证算法量级）")
    fs = 32000.0
    t = np.arange(int(20 * fs)) / fs
    for amplitude, freq in ((0.80, 145.0), (1.05, 172.0), (2.20, 235.0)):
        x = amplitude * np.sin(2 * np.pi * freq * t)
        values = compute_metrics(x, fs, codes=["a_Peak", "a_RMS", "v_RMS"])
        theory_v_rms = amplitude * 9806.65 / (2 * np.pi * freq) / np.sqrt(2)
        say(
            f"　{amplitude} gpk @ {freq:g} Hz → a_Peak {values['a_Peak']:.4f} g · "
            f"a_RMS {values['a_RMS']:.4f} g（理论 {amplitude / np.sqrt(2):.4f}）· "
            f"v_RMS {values['v_RMS']:.4f} mm/s（理论 {theory_v_rms:.4f}，"
            f"偏差 {(values['v_RMS'] / theory_v_rms - 1) * 100:+.2f}%）"
        )

    # ---------------- P7 批次缺通道文件：会不会提示
    say("")
    say("[P7] 批次只有 CH1/CH2/CH3（树上 4 个通道）：导入结果的提示")
    work = Path.home() / "workspace" / "temp" / datetime.now().strftime("%Y-%m-%d")
    partial = work / "probe-partial-CH123"
    partial.mkdir(parents=True, exist_ok=True)
    for item in sorted((DATA / "2026-10-29 15_28_34").iterdir()):
        if "CH4" not in item.name:
            (partial / item.name).write_bytes(item.read_bytes())
    result = import_batch(session, task.id, partial, channel_nodes, compute=True)
    say(
        f"　status={result.status} · channels={result.channels} · files_ok={result.files_ok}"
        f" · unmapped_channels={result.unmapped_channels} · 值行={result.metrics_written}"
    )
    say("　→ 树上配了 4 个通道、文件只有 3 个，结果里没有任何「缺 CH4」的提示")

    # ---------------- P8 截断文件：整批中断 + 残留数据集
    say("")
    say("[P8] 批次里一个文件被截断到 1/3：整批导入行为")
    broken = work / "probe-truncated"
    broken.mkdir(parents=True, exist_ok=True)
    raw = (DATA / "2026-10-29 15_28_34" / "2026-10-29 15_28_34-CH1_Task1.dat").read_bytes()
    (broken / "2026-10-29 15_28_34-CH1_Task1.dat").write_bytes(raw[: len(raw) // 3])
    before_codes = [row.code for row in datasets.list_datasets(session, task.id)]
    try:
        import_batch(session, task.id, broken, {"CH1": channel_nodes["CH1"]}, compute=True)
    except Exception as exc:  # noqa: BLE001 —— 记录抛出类型
        say(f"　抛出 {type(exc).__name__}：{str(exc)[:90]}…")
    after_rows = [
        (row.code, row.status, len(datasets.list_files(session, row.id)))
        for row in datasets.list_datasets(session, task.id)
    ]
    say(f"　导入前数据集 {len(before_codes)} 个 → 导入后 {len(after_rows)} 个")
    say(f"　新增的：{[row for row in after_rows if row[0] not in before_codes]}")

    # ---------------- P9 删数据集：指标值是否一起隐藏
    say("")
    say("[P9] 软删数据集后指标值是否残留")
    victim = datasets.list_datasets(session, task.id)[0]
    value_before = len(metric_store.list_values(session, task.id, "CH2", "a_Peak"))
    datasets.delete_dataset(session, victim.id)
    value_after = len(metric_store.list_values(session, task.id, "CH2", "a_Peak"))
    say(
        f"　删 {victim.code}：CH2·a_Peak 值行 {value_before} → {value_after}"
        f" · 数据集剩 {len(datasets.list_datasets(session, task.id))} 个"
    )

    session.close()
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n已写：{OUT}")


def _value_count(session, task_id: int) -> int:
    total = 0
    for code in ("CH1", "CH2", "CH3", "CH4"):
        total += len(metric_store.list_values(session, task_id, code))
    return total


if __name__ == "__main__":
    main()
