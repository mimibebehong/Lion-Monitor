"""P0 + P0.5 验收实跑（2026-10-31）：B1 / G1 / G9 / B3 / G10 逐条出证据。

用法：
    .venv/bin/python scripts/verify_p0_p05_2026-10-31.py            # 六条验收（除全量测试）
    .venv/bin/python scripts/verify_p0_p05_2026-10-31.py --field-json out.json
        # 只跑「现场四通道数据导入」并把逐位值落成 JSON（配合 git worktree 比对新旧实现）

口径：
- 所有写入落在 ``~/workspace/temp/2026-10-31/p0-p05/`` 下的临时库，**不碰 data/lion.db**
- 现场数据来自 ``/mnt/d/hermes_wp/Project/锂电产线/envision/data/四通道保存``（只读）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "app"))

import numpy as np  # noqa: E402

from lion_monitor import limits_rules  # noqa: E402
from lion_monitor.features import health  # noqa: E402
from lion_monitor.features.pipeline import import_batch  # noqa: E402
from lion_monitor.ingest.vibration import read_vibration  # noqa: E402
from lion_monitor.store import datasets, tasks, tree  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store.db import init_db, make_engine, make_session_factory  # noqa: E402
from lion_monitor.store.models import Channel, Dataset  # noqa: E402

FIELD_ROOT = Path("/mnt/d/hermes_wp/Project/锂电产线/envision/data/四通道保存")
WORK = Path.home() / "workspace" / "temp" / "2026-10-31" / "p0-p05"
CHANNELS = ("CH1", "CH2", "CH3", "CH4")
METRICS = ("a_Peak", "a_RMS", "v_RMS")


# ---------------------------------------------------------------- 小工具


def head(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def fresh(name: str):
    """建一个干净的临时库 + 会话工厂。"""
    folder = WORK / name
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)
    engine = make_engine(folder / "lion.db")
    init_db(engine)
    return engine, make_session_factory(engine)


def build_tree(session, task_id: int, object_name: str = "隔膜过辊轴承") -> dict[str, int]:
    """任务 → 设备 → 监测对象 → 4 通道 × 3 指标。"""
    root = tree.root_node(session, task_id)
    device = tree.create_node(session, task_id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task_id, "component", object_name, parent_id=device.id)
    nodes: dict[str, int] = {}
    for code in CHANNELS:
        node = tree.create_node(session, task_id, "channel", code, parent_id=comp.id)
        nodes[code] = node.id
        for metric_code in METRICS:
            tree.create_node(
                session,
                task_id,
                "metric",
                metric_code,
                parent_id=node.id,
                attrs={"code": metric_code},
            )
    return nodes


def alive_counts(session) -> dict[str, int]:
    """库内存活（未软删）的 dataset / channel / metric_value 行数。"""
    from lion_monitor.store.models import MetricValue

    return {
        "dataset": session.query(Dataset).filter(Dataset.deleted_at.is_(None)).count(),
        "channel": session.query(Channel).filter(Channel.deleted_at.is_(None)).count(),
        "metric_value": session.query(MetricValue).filter(MetricValue.deleted_at.is_(None)).count(),
    }


def write_field_batch(tmp_root: Path, *, samples_mutator=None, scale: float = 1.0):
    """用现场真实样本造一个可控批次（默认原样，供坏输入用例使用）。"""
    stamp = "20261029152834"
    folder = tmp_root / stamp
    src = FIELD_ROOT / "2026-10-29 15_28_34"
    for ch in CHANNELS:
        record = read_vibration(src / f"2026-10-29 15_28_34-{ch}_Task1.dat")
        samples = record.samples.copy()
        if samples_mutator is not None:
            samples_mutator(samples)
        header = (src / f"2026-10-29 15_28_34-{ch}_Task1.dat").read_bytes()[:512]
        buf = bytearray(header)
        buf[0x08:0x0C] = np.float32(scale).tobytes()  # 改 scale
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{stamp}-{ch}_Task1.dat").write_bytes(
            bytes(buf) + samples.astype("<f4").tobytes()
        )
    return folder


# ---------------------------------------------------------------- [6] 现场数据


def field_import(field_root: Path, db_folder: Path, object_name: str = "隔膜过辊轴承") -> dict:
    """把现场三个批次按健康树导入，返回逐位结果（JSON 可序列化）。"""
    if db_folder.exists():
        shutil.rmtree(db_folder)
    db_folder.mkdir(parents=True, exist_ok=True)
    engine = make_engine(db_folder / "lion.db")
    init_db(engine)
    factory = make_session_factory(engine)
    payload: dict = {"batches": []}
    with factory() as session:
        task = tasks.create_task(session, name="现场四通道验收")
        nodes = build_tree(session, task.id, object_name)
        for folder in sorted(p for p in field_root.iterdir() if p.is_dir()):
            result = import_batch(session, task.id, folder, nodes)
            batch = {
                "dir": folder.name,
                "code": datasets.get_dataset(session, result.dataset_id).code
                if result.dataset_id
                else None,
                "status": result.status,
                "files_ok": result.files_ok,
                "channels": result.channels,
                "metrics_written": result.metrics_written,
                "compute_runs": result.compute_runs,
                "dedupe_hits": result.dedupe_hits,
                "collected_at": result.collected_at,
                "values": {},
            }
            for file in datasets.list_files(session, result.dataset_id):
                for row in metric_store.list_values(session, task.id, file.channel_code):
                    if row.dataset_id != result.dataset_id:
                        continue
                    batch["values"][f"{file.channel_code}:{row.metric_code}"] = {
                        "value": repr(float(row.value)),
                        "unit": row.unit,
                        "params_hash": row.params_hash,
                        "computed_at": row.computed_at,
                        "node_id": row.node_id,
                    }
            payload["batches"].append(batch)
    payload["alive"] = alive_counts(session)
    return payload


# ---------------------------------------------------------------- 各条验收


def check_b1() -> None:
    head("[验收 1] B1 数据集编号：同一天两个任务各导同一批次 + 4 并发导入")
    engine, factory = fresh("b1")
    batch = write_field_batch(WORK / "b1" / "批次" / "四通道保存")
    with factory() as session:
        task_a = tasks.create_task(session, name="任务A")
        task_b = tasks.create_task(session, name="任务B")
        row_a = datasets.import_from_dir(session, task_a.id, batch)
        row_b = datasets.import_from_dir(session, task_b.id, batch)
        print(f"　任务A 导入 → {row_a.code}（{row_a.name}）")
        print(f"　任务B 导入 → {row_b.code}（{row_b.name}）")
        print(f"　两个编号是否相同：{row_a.code == row_b.code}（应为 False）")
        print(
            f"　任务A 数据集数 {len(datasets.list_datasets(session, task_a.id))} / "
            f"任务B {len(datasets.list_datasets(session, task_b.id))}"
        )

        task_c = tasks.create_task(session, name="并发任务")
        dirs = [write_field_batch(WORK / "b1" / f"并发{i}" / "四通道保存") for i in range(4)]

    def _run(folder: Path) -> tuple[str, str]:
        with factory() as own:
            try:
                return "ok", datasets.import_from_dir(own, task_c.id, folder).code
            except Exception as exc:  # noqa: BLE001 - 验收要看真实成功/失败
                return "fail", f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(_run, dirs))
    for idx, (status, detail) in enumerate(outcomes):
        print(f"　并发 tab{idx}：{status} {detail}")
    ok = sum(1 for status, _ in outcomes if status == "ok")
    with factory() as session:
        count = len(datasets.list_datasets(session, task_c.id))
        print(f"　并发成功 {ok}/4；库内该任务数据集 {count} 个")
        codes = [detail for status, detail in outcomes if status == "ok"]
        print(f"　并发编号是否互不相同：{len(set(codes)) == len(codes)}：{codes}")


def check_g1() -> None:
    head("[验收 2] G1 重复导入幂等：相对路径 vs 绝对路径")
    engine, factory = fresh("g1")
    batch = write_field_batch(WORK / "g1" / "批次" / "四通道保存")
    with factory() as session:
        task = tasks.create_task(session, name="任务G1")
        cwd = os.getcwd()
        os.chdir(batch)
        try:
            first = datasets.import_from_dir(session, task.id, ".")
        finally:
            os.chdir(cwd)
        second = datasets.import_from_dir(session, task.id, str(batch))
        third = datasets.import_from_dir(session, task.id, str(batch) + "/")
        print(f"　相对路径 '.' → {first.code}（id={first.id}）")
        print(f"　绝对路径 → {second.code}（id={second.id}）")
        print(f"　结尾斜杠 → {third.code}（id={third.id}）")
        rows = datasets.list_datasets(session, task.id)
        print(f"　数据集条数：{len(rows)}（应为 1）")
        print(f"　source_dir 落库值：{rows[0].source_dir if rows else '—'}")


def check_g9() -> None:
    head("[验收 2b] G9 覆盖导入二次确认（AppTest 真跑数据接入页）")
    engine, factory = fresh("g9")
    os.environ["LION_DB"] = str(WORK / "g9" / "lion.db")
    batch = write_field_batch(WORK / "g9" / "批次" / "四通道保存")
    with factory() as session:
        task = tasks.create_task(session, name="任务G9")
        nodes = build_tree(session, task.id)
        result = import_batch(session, task.id, batch, nodes)
        row = datasets.get_dataset(session, result.dataset_id)
        values = metric_store.count_values_for_dataset(session, row.id)
        batch_name = Path(row.source_dir).name

    import store_ctx  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(ROOT / "app" / "pages" / "ingest.py"), default_timeout=180)
    at.session_state["current_task_id"] = task.id
    at.session_state["scan_batches"] = [
        {
            "批次": batch_name,
            "状态": f"已处理 · {row.code}",
            "_已处理": True,
            "目录": row.source_dir,
            "采集时间": row.collected_at,
            "通道": "CH1 / CH2 / CH3 / CH4",
            "点数": 640000,
            "采样率": 32000.0,
            "时长(s)": 20.0,
            "文件数": 4,
        }
    ]
    at.session_state["scan_errors"] = []
    at.session_state["ingest_pick"] = [batch_name]
    at.run()
    boxes = [c for c in at.checkbox if "覆盖" in c.label]
    buttons = [b for b in at.button if b.label.startswith("导入选中批次")]
    print(f"　页面异常：{[e.value for e in at.exception]}")
    print(f"　覆盖确认勾选框：{[c.label for c in boxes]}")
    print(
        f"　库内该数据集指标值 {values} 行 → 勾选框文案里的行数是否一致："
        f"{bool(boxes) and f'将覆盖 {values} 行' in boxes[0].label}"
    )
    print(f"　未勾选时导入按钮 disabled：{buttons[0].disabled if buttons else '—'}")
    boxes[0].check()
    at.run()
    buttons = [b for b in at.button if b.label.startswith("导入选中批次")]
    print(f"　勾选后导入按钮 disabled：{buttons[0].disabled if buttons else '—'}")
    store_ctx._session_factory.clear()


def check_b3() -> None:
    head("[验收 3] B3 页面对坏输入：一条中文提示 / 库内 0 残留 / 无 Traceback")
    cases = {
        "NaN 样本": lambda s: s.__setitem__(slice(100, 101), np.nan),
        "Inf 样本": lambda s: s.__setitem__(slice(100, 101), np.inf),
    }
    for label, mutator in cases.items():
        _b3_case(f"b3-{label}", label, samples_mutator=mutator)
    _b3_case("b3-scale", "scale=NaN（整批）", scale=float("nan"))
    _b3_case("b3-dup", "同批重复通道号", duplicate=True)

    head("[验收 3b] B3 store 层：坏输入不抛 IntegrityError（直接跑 import_batch）")
    engine, factory = fresh("b3-store")
    folder = write_field_batch(
        WORK / "b3-store" / "批次" / "四通道保存",
        samples_mutator=lambda s: s.__setitem__(slice(0, 1), np.nan),
    )
    with factory() as session:
        task = tasks.create_task(session, name="任务B3")
        nodes = build_tree(session, task.id)
        result = import_batch(session, task.id, folder, nodes)
        print(
            f"　结果：status={result.status} discarded={result.discarded} "
            f"files_bad={len(result.files_bad)}"
        )
        print(f"　库内残留：{alive_counts(session)}")


def _b3_case(
    name: str, label: str, *, samples_mutator=None, scale: float = 1.0, duplicate: bool = False
) -> None:
    engine, factory = fresh(name)
    os.environ["LION_DB"] = str(WORK / name / "lion.db")
    if duplicate:
        folder = write_field_batch(WORK / name / "批次" / "四通道保存")
        src = folder / "20261029152834-CH1_Task1.dat"
        shutil.copy(src, folder / "20261029152834-CH1_Task2.dat")
    else:
        folder = write_field_batch(
            WORK / name / "批次" / "四通道保存", samples_mutator=samples_mutator, scale=scale
        )
    with factory() as session:
        task = tasks.create_task(session, name=f"任务{name}")
        build_tree(session, task.id)
        before = alive_counts(session)

    import store_ctx  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(ROOT / "app" / "pages" / "ingest.py"), default_timeout=300)
    at.session_state["current_task_id"] = task.id
    at.session_state["scan_batches"] = [
        {
            "批次": folder.name,
            "状态": "未处理",
            "_已处理": False,
            "目录": str(folder),
            "采集时间": "2026-10-29 15:28:34",
            "通道": "CH1 / CH2 / CH3 / CH4",
            "点数": 640000,
            "采样率": 32000.0,
            "时长(s)": 20.0,
            "文件数": 5 if duplicate else 4,
        }
    ]
    at.session_state["scan_errors"] = []
    at.run()
    button = [b for b in at.button if b.label.startswith("导入选中批次")][0]
    button.click()
    at.run()
    errors = [item.value for item in at.error]
    with factory() as session:
        after = alive_counts(session)
        values = sum(1 for ch in CHANNELS for _ in metric_store.list_values(session, task.id, ch))
    print(f"\n　[{label}]")
    print(f"　　Traceback / 异常：{[e.value for e in at.exception]}")
    print(f"　　页面提示条数：{len(errors)}")
    for text in errors:
        print(f"　　页面提示：{text[:160]}{'…' if len(text) > 160 else ''}")
    print(f"　　库内残留：导入前 {before} → 导入后 {after}；指标值 {values} 行")
    leaks = [k for k in ("INSERT", "SELECT", "sqlite3", "Traceback") if k in "".join(errors)]
    print(f"　　文案泄漏 SQL：{bool(leaks)}")
    store_ctx._session_factory.clear()


def check_g10() -> None:
    head("[验收 4] G10 NaN 判级")
    nan, inf = float("nan"), float("inf")
    for value in (nan, inf, -inf):
        print(
            f"　　值 {value!r:<8} grade('>',1.5,3.0)={limits_rules.grade(value, '>', 1.5, 3.0):<8}"
            f" point_level={health.point_level(value, (1.5, 3.0, '>', '指标级'))}"
        )
    for value in (1.0, 2.0, 9.0):
        print(
            f"　　值 {value!r:<8} grade('>',1.5,3.0)={limits_rules.grade(value, '>', 1.5, 3.0):<8}"
            f" point_level={health.point_level(value, (1.5, 3.0, '>', '指标级'))}"
        )
    from tree_ui import STATE_COLOR, STATE_TEXT  # noqa: E402

    print(f"　　页面文案：invalid → {STATE_TEXT['invalid']}（颜色 {STATE_COLOR['invalid']}）")
    print(f"　　LEVEL_ORDER：{health.LEVEL_ORDER}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--field-json", default="", help="只跑现场数据导入并把逐位结果写入该 JSON")
    parser.add_argument("--object-name", default="隔膜过辊轴承")
    args = parser.parse_args()

    if args.field_json:
        payload = field_import(FIELD_ROOT, WORK / "field", args.object_name)
        Path(args.field_json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"现场导入结果已写入 {args.field_json}")
        for batch in payload["batches"]:
            print(
                f"　{batch['dir']} → {batch['code']} {batch['status']} "
                f"{batch['metrics_written']} 个指标值"
            )
        return

    WORK.mkdir(parents=True, exist_ok=True)
    print(f"工作目录：{WORK}")
    print(f"现场数据：{FIELD_ROOT}（存在：{FIELD_ROOT.is_dir()}）")
    check_b1()
    check_g1()
    check_g9()
    check_b3()
    check_g10()
    head("[验收 6] 现场四通道数据导入（三个批次，逐位值见 JSON 比对）")
    payload = field_import(FIELD_ROOT, WORK / "field")
    for batch in payload["batches"]:
        print(
            f"　{batch['dir']}：{batch['code']} · {batch['status']} · "
            f"{batch['channels']} 通道 · {batch['metrics_written']} 个指标值 · "
            f"真算 {batch['compute_runs']} / 复用 {batch['dedupe_hits']} · {batch['collected_at']}"
        )
    print("\n　CH2 三批次 v_RMS（现场激励通道）：")
    for batch in payload["batches"]:
        print(f"　　{batch['dir']}：{batch['values'].get('CH2:v_RMS', {}).get('value')}")
    print(f"\n　库内活行数：{payload['alive']}")
    print(f"\n　JSON 已写入：{WORK / 'field-now.json'}")
    (WORK / "field-now.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
