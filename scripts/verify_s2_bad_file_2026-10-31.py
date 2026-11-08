"""S-2 修复证据：真实四通道数据 + 一个人为截断文件，实跑导入管线。

对照 docs/reviews/2026-10-31-现场数据实跑复查.md 的 S-2 条目与 probes P8：
改造前一个截断文件会让整批中断、并在库里留下「待判 + 0 值」空数据集。

本脚本跑三个批次（都基于现场数据 2026-10-29 15_28_34 的四通道文件）：
- A 全好：四通道原样 → 结果应与改造前一致
- B 部分损坏：CH2 截断到 1/3 → 其余三通道照常入库、files_bad 一条、数据集保留且备注点名
- C 全坏：四个文件都截断 → 不保留数据集（软删留痕），列表里不长空壳

输出：reports/2026-10-31-s2-fix-evidence.txt（同时打印到 stdout）
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.features.pipeline import import_batch  # noqa: E402
from lion_monitor.ingest.vibration import read_vibration  # noqa: E402
from lion_monitor.store import datasets, recycle, tasks, tree  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store.db import init_db, make_engine, make_session_factory  # noqa: E402

DATA = Path("/mnt/d/hermes_wp/Project/锂电产线/envision/data/四通道保存")
BATCH = "2026-10-29 15_28_34"
TASK_NAME = "卷绕产线振动监测"
WORK = Path.home() / "workspace" / "temp" / datetime.now().strftime("%Y-%m-%d")
SRC_DB = WORK / "field-run.db"
DB = WORK / "s2-evidence.db"
OUT = ROOT / "reports" / "2026-10-31-s2-fix-evidence.txt"

#: 改造前实跑写入的值（docs/reviews/2026-10-31-现场数据实跑复查.md §3 表，批次 10-29 15:28:34）
BASELINE = {
    "CH1": {"a_Peak": "0.00785", "a_RMS": "0.00169", "v_RMS": "0.05007"},
    "CH2": {"a_Peak": "0.82939", "a_RMS": "0.57906", "v_RMS": "6.23095"},
    "CH3": {"a_Peak": "0.00168", "a_RMS": "0.00033", "v_RMS": "0.00955"},
    "CH4": {"a_Peak": "0.00764", "a_RMS": "0.00159", "v_RMS": "0.04742"},
}

lines: list[str] = []


def say(text: str = "") -> None:
    print(text)
    lines.append(text)


def _copy_batch(target: Path, truncate: tuple[str, ...] = ()) -> Path:
    """把现场批次复制到新目录；truncate 里的通道把数据体截到 1/3。"""
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted((DATA / BATCH).iterdir()):
        raw = item.read_bytes()
        if any(f"-{ch}_" in item.name for ch in truncate):
            raw = raw[: len(raw) // 3]
        (target / item.name).write_bytes(raw)
    return target


def _values(session, task_id: int, channel_code: str, dataset_id: int) -> dict[str, float]:
    """只取某个数据集下该通道的指标值（跨数据集会把历次导入的值混在一起）。"""
    return {
        row.metric_code: row.value
        for row in metric_store.list_values(session, task_id, channel_code)
        if row.dataset_id == dataset_id
    }


def _dataset_rows(session, task_id: int) -> list[tuple[str, str, int, int]]:
    rows = []
    for row in datasets.list_datasets(session, task_id):
        files = datasets.list_files(session, row.id)
        rows.append((row.code, row.status, len(files), row.file_count))
    return rows


def _ensure_task(session):
    """现场树优先（复用实跑库）；没有就按四通道 × 三指标现搭一个。"""
    rows = [row for row in tasks.list_tasks(session) if row.name == TASK_NAME]
    if rows:
        return rows[0]
    task = tasks.create_task(session, name=TASK_NAME)
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "卷绕机A", parent_id=device.id)
    for ch in ("CH1", "CH2", "CH3", "CH4"):
        node = tree.create_node(session, task.id, "channel", ch, parent_id=comp.id)
        for code in ("v_RMS", "a_Peak", "a_RMS"):
            tree.create_node(
                session, task.id, "metric", code, parent_id=node.id, attrs={"code": code}
            )
    return task


def main() -> None:
    if DB.exists():
        DB.unlink()
    if SRC_DB.exists():
        shutil.copy(SRC_DB, DB)  # 复用现场实跑的树 / 指标定义 / 阈值
    init_db(make_engine(DB))

    session = make_session_factory(make_engine(DB))()
    task = _ensure_task(session)
    channel_nodes = {
        node.name: node.id
        for node in tree.list_nodes(session, task.id)
        if node.node_type == "channel"
    }

    say("=" * 92)
    say(
        f"S-2 修复证据（批次里一个文件损坏不中断整批 + 不留空数据集）"
        f"　{datetime.now():%Y-%m-%d %H:%M:%S}"
    )
    say(f"临时库：{DB}")
    say(f"现场数据：{DATA / BATCH}")
    say(f"健康树通道节点：{channel_nodes}")
    say("=" * 92)

    # ---------------- 0. 截断文件确实是坏文件
    say("")
    say("[0] 截断文件本身：直接 read_vibration 仍然报错（证明它真的是坏文件，不是被悄悄接受）")
    probe_dir = _copy_batch(WORK / "s2-batch-partial", truncate=("CH2",))
    bad_path = probe_dir / f"{BATCH}-CH2_Task1.dat"
    try:
        read_vibration(bad_path)
        say("　⚠️ 没有报错——探针无效")
    except Exception as exc:  # noqa: BLE001 —— 只用来证明文件坏
        say(f"　{type(exc).__name__}：{str(exc)[:120]}…")
    say(f"　截断后大小 {bad_path.stat().st_size} B（原文件 2560512 B）")

    # ---------------- A 全好批次
    say("")
    say("[A] 全好批次（四通道原样）：结果应与改造前一致")
    good_dir = _copy_batch(WORK / "s2-batch-good")
    before = _dataset_rows(session, task.id)
    result = import_batch(session, task.id, good_dir, channel_nodes, compute=True)
    after = _dataset_rows(session, task.id)
    say(f"　导入前数据集 {len(before)} 个 → 导入后 {len(after)} 个")
    say(
        f"　status={result.status} · channels={result.channels} · files_ok={result.files_ok}"
        f" · files_bad={result.files_bad_count} · 损坏={len(result.files_damaged)}"
        f" · 指标值={result.metrics_written} · 真算 {result.compute_runs}/复用 {result.dedupe_hits}"
        f" · discarded={result.discarded} · dataset_id={result.dataset_id}"
    )
    say(f"　备注：「{result.remark}」")
    assert result.dataset_id is not None
    say("　对照改造前的基线：复查文档 §3 表里同一批次（10-29 15:28:34）改造前实跑写入的值")
    mismatches = 0
    compared = 0
    for code in ("CH1", "CH2", "CH3", "CH4"):
        values = _values(session, task.id, code, result.dataset_id)
        detail = " · ".join(
            f"{k}={values[k]:.5f}" for k in ("a_Peak", "a_RMS", "v_RMS") if k in values
        )
        say(f"　{code}：{detail}")
        for metric_code, expected in BASELINE[code].items():
            compared += 1
            actual = values.get(metric_code)
            if actual is None or f"{actual:.5f}" != expected:
                mismatches += 1
                say(f"　　⚠️ {code}·{metric_code} 与基线不同：{actual!r} ≠ {expected}")
    say(f"　与基线逐位比对：{compared - mismatches}/{compared} 完全相同（不一致 {mismatches} 处）")

    # ---------------- B 部分损坏
    say("")
    say("[B] 部分损坏：CH2 截断到 1/3，混在四通道批次里")
    before = _dataset_rows(session, task.id)
    result = import_batch(session, task.id, probe_dir, channel_nodes, compute=True)
    after = _dataset_rows(session, task.id)
    say(f"　导入前数据集 {len(before)} 个 → 导入后 {len(after)} 个（未抛异常，整批继续）")
    say(
        f"　status={result.status} · channels={result.channels} · files_ok={result.files_ok}"
        f" · files_bad={result.files_bad_count} · 损坏={len(result.files_damaged)}"
        f" · 指标值={result.metrics_written} · discarded={result.discarded}"
    )
    for item in result.files_bad:
        say(f"　坏文件：{item.filename} · 通道 {item.channel_code} · damaged={item.damaged}")
        say(f"　　原因：{item.reason[:150]}")
    say(f"　数据集备注：「{result.remark}」")
    assert result.dataset_id is not None
    for f in datasets.list_files(session, result.dataset_id):
        values = _values(session, task.id, f.channel_code, result.dataset_id)
        detail = " · ".join(
            f"{k}={values[k]:.5f}" for k in ("a_Peak", "a_RMS", "v_RMS") if k in values
        )
        say(
            f"　文件 {f.channel_code}：{f.file_name} · 状态 {f.status}"
            f" · 本数据集指标值 {len(values)} 行（{detail or '无'}）"
        )
    added = [row for row in after if row[0] not in {item[0] for item in before}]
    say(f"　新增数据集：{added}")

    # ---------------- C 全坏
    say("")
    say("[C] 全坏：四个文件都截断")
    allbad_dir = _copy_batch(WORK / "s2-batch-allbad", truncate=("CH1", "CH2", "CH3", "CH4"))
    before = _dataset_rows(session, task.id)
    result = import_batch(session, task.id, allbad_dir, channel_nodes, compute=True)
    after = _dataset_rows(session, task.id)
    say(f"　导入前数据集 {len(before)} 个 → 导入后 {len(after)} 个（不留「待判 + 0 值」空壳）")
    say(
        f"　status={result.status} · channels={result.channels} · files_ok={result.files_ok}"
        f" · files_bad={result.files_bad_count} · 损坏={len(result.files_damaged)}"
        f" · 指标值={result.metrics_written} · discarded={result.discarded}"
        f" · dataset_id={result.dataset_id}"
    )
    say(f"　备注：「{result.remark}[…]」")
    items = [i for i in recycle.list_recycle_items(session) if i.operator == "import_batch"]
    say(f"　回收站留痕（operator=import_batch）：{[i.entity_name for i in items]}")
    say(f"　留痕级联计数：{items[-1].cascade if items else '无'}")

    # ---------------- D 已导入批次再导（此刻全坏）：不动历史值
    say("")
    say("[D] 已导入过的批次此刻文件全坏：保留数据集与历史指标值，只标无效")
    for item in sorted(probe_dir.iterdir()):
        raw = item.read_bytes()
        item.write_bytes(raw[: len(raw) // 3])
    before = _dataset_rows(session, task.id)
    again = import_batch(session, task.id, probe_dir, channel_nodes, compute=True)
    after = _dataset_rows(session, task.id)
    say(f"　导入前数据集 {len(before)} 个 → 导入后 {len(after)} 个（不新增、也不丢）")
    say(
        f"　discarded={again.discarded} · dataset_id={again.dataset_id}"
        f" · files_ok={again.files_ok} · 损坏={len(again.files_damaged)}"
    )
    say(f"　备注：「{again.remark[:160]}…」")
    if again.dataset_id is not None:
        kept = datasets.get_dataset(session, again.dataset_id)
        kept_values = sum(
            len(_values(session, task.id, code, kept.id)) for code in ("CH1", "CH3", "CH4")
        )
        say(
            f"　数据集 {kept.code} 状态 {kept.status} · 文件 {kept.file_count} 个"
            f" · CH1/CH3/CH4 历史指标值仍 {kept_values} 行"
        )

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n已写入 {OUT}")


if __name__ == "__main__":
    main()
