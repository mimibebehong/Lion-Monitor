"""导入进度回调（马哥 2026-11-03：长导入要有实时进度条）。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "tests", ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from _dat_fixture import write_dat  # noqa: E402

from lion_monitor.features import pipeline  # noqa: E402
from lion_monitor.store import channel_reg, metric_def, object_registry, tasks, tree  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402

FS = 32000


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "prog.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)

    folder = tmp_path / "batch"
    folder.mkdir()
    for code in ("CH1", "CH2", "CH3"):
        samples = 0.4 * np.sin(2 * np.pi * 97.3 * np.arange(FS * 4) / FS)
        write_dat(folder / f"{code}_4s.dat", samples=samples, channel=code, sample_rate_hz=FS)

    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="进度任务")
        obj = object_registry.create_object(
            session, task.id, "隔膜过辊轴承", "轴承", part="隔膜过辊"
        )
        metric_def.seed_defaults(session, task.id)
        defs = {row.code: row for row in metric_def.list_metrics(session, task.id)}
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(
            session,
            task.id,
            "component",
            "隔膜过辊轴承",
            parent_id=device.id,
            attrs={"部位": "隔膜过辊"},
            ref_id=obj.id,
        )
        nodes: dict[str, int] = {}
        for code in ("CH1", "CH2", "CH3"):
            ch = channel_reg.create_channel(session, task.id, obj.id, code)
            ch_node = tree.create_node(
                session, task.id, "channel", code, parent_id=comp.id, ref_id=ch.id
            )
            tree.create_node(
                session,
                task.id,
                "metric",
                "v_RMS",
                parent_id=ch_node.id,
                attrs={"code": "v_RMS", "单位": "mm/s"},
                ref_id=defs["v_RMS"].id,
            )
            nodes[code] = ch_node.id
        task_id = task.id

    yield {"factory": factory, "task": task_id, "folder": folder, "nodes": nodes}


def test_import_batch_reports_progress(env):
    seen: list[tuple[float, str]] = []

    with store_db.session_scope(env["factory"]) as session:
        result = pipeline.import_batch(
            session,
            env["task"],
            env["folder"],
            env["nodes"],
            compute=True,
            progress=lambda fraction, text: seen.append((fraction, text)),
        )

    assert result.metrics_written > 0
    fractions = [item[0] for item in seen]
    assert fractions == sorted(fractions), fractions
    assert fractions[0] >= 0.0
    assert fractions[-1] == pytest.approx(1.0), fractions
    assert len({text for _, text in seen}) > 1, "进度文案应随阶段变化"
    assert any("CH" in text for _, text in seen), seen


def test_import_batch_works_without_progress_callback(env):
    with store_db.session_scope(env["factory"]) as session:
        result = pipeline.import_batch(session, env["task"], env["folder"], env["nodes"])
    assert result.files_ok == 3


def test_progress_reporter_maps_stage_to_overall():
    """页面用 ui.progress_reporter 把「批次内进度」换算成整体进度。"""
    from app import ui

    class FakeBar:
        def __init__(self):
            self.calls = []

        def progress(self, value, text=None):
            self.calls.append((round(value, 4), text))

    bar = FakeBar()
    report = ui.progress_reporter(bar, 1, 3)
    report(0.0, "第一批")
    report(0.5, "第一批一半")
    report(1.0, "第一批完成")

    values = [call[0] for call in bar.calls]
    assert values == [round(1 / 3, 4), round(1.5 / 3, 4), round(2 / 3, 4)], values
    assert bar.calls[-1][1] == "第一批完成"


def test_progress_reporter_clamps_out_of_range():
    from app import ui

    class FakeBar:
        def __init__(self):
            self.calls = []

        def progress(self, value, text=None):
            self.calls.append(value)

    bar = FakeBar()
    report = ui.progress_reporter(bar, 2, 3)
    report(-5.0, "x")
    report(9.0, "y")
    assert bar.calls == [pytest.approx(2 / 3), 1.0], bar.calls


def test_ingest_page_uses_progress_bar():
    """数据接入页导入时必须把进度回调接上（长导入要能看见进度）。"""
    source = (ROOT / "app" / "pages" / "ingest.py").read_text(encoding="utf-8")
    assert "st.progress(" in source
    assert "progress=progress_reporter(" in source
