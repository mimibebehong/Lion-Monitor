"""CF-2 验收 1–4：监测对象页「频率参数」页签（Streamlit AppTest 真跑页面）。

覆盖：

1. 读出 CF-1 导入的 57 个部件（首个部件名 Z1_R）；
2. 编辑一个部件 OM1 并保存 → 回库读新值，``hz_at_1rpm`` 同步；
3. 上传附件 CSV → 预览计数与后端一致 → 确认后入库；
4. 「导出配置」产出与附件同格式，可被 CF-1 导入器读回且 0 变化。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PAGE = ROOT / "app" / "pages" / "objects.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import component_freq, object_registry, tasks  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402

ATTACHMENT = ROOT / "tests" / "data" / "examples" / "监测对象_20260309.csv"
ROUND_TRIP = ROOT / "tests" / "data" / "examples" / "监测对象_20260309_GBK原始.csv"


class Env:
    def __init__(self, tmp_path, monkeypatch):
        path = tmp_path / "ui.db"
        monkeypatch.setenv("LION_DB", str(path))
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        with self.session() as session:
            self.task = tasks.create_task(session, name="频率参数 UI 任务")
            self.obj = object_registry.create_object(
                session, self.task.id, name="主减速器", obj_type="轴承"
            )

    def session(self):
        return store_db.session_scope(store_db.make_session_factory(self.engine))

    def parts(self):
        with self.session() as session:
            return component_freq.list_parts(session, self.task.id, object_id=self.obj.id)

    def import_attachment(self):
        with self.session() as session:
            component_freq.import_csv(session, self.obj.id, ATTACHMENT)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    instance = Env(tmp_path, monkeypatch)
    import store_ctx

    store_ctx._session_factory.clear()
    yield instance
    store_ctx._session_factory.clear()


def _app(env):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _captions(at) -> list[str]:
    return [element.value for element in at.caption]


def _numbers(at) -> dict[str, object]:
    return {element.label: element for element in at.number_input}


def _button(at, label: str):
    for element in at.button:
        if element.label == label:
            return element
    raise AssertionError(f"找不到按钮 {label}：{[b.label for b in at.button]}")


# ---------------------------------------------------------------- 验收 1


def test_freq_tab_lists_57_parts_from_cf1_import(env):
    env.import_attachment()

    at = _app(env)

    assert "频率参数" in [tab.label for tab in at.tabs]
    assert at.selectbox(key="cf_object").value.startswith("主减速器")
    assert any("部件 57 个" in text for text in _captions(at)), _captions(at)
    part_options = list(at.selectbox(key="cf_part").options)
    assert part_options[0] == "Z1_R · 右输入级主动锥齿轮"
    assert len(part_options) == 57
    # 已挂通道（多值 + 显示名）
    assert any("Chan_1" in text and "Chan_8" in text for text in _captions(at)), _captions(at)


def test_freq_tab_empty_state_when_no_ledger(env):
    at = _app(env)

    assert any("还没有频率参数" in text for text in _captions(at)), _captions(at)


# ---------------------------------------------------------------- 验收 2


def test_edit_om1_and_save_updates_value_and_normalized(env):
    env.import_attachment()
    at = _app(env)
    at.selectbox(key="cf_part").set_value("Z1_R · 右输入级主动锥齿轮").run()

    # 表单控件先改值、再点保存，同一次 run 提交（Streamlit 表单口径）
    _numbers(at)["OM1（Hz）"].set_value(350.0)
    _button(at, "保存频率参数").click()
    at.run()

    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    part = next(p for p in env.parts() if p.part_code == "Z1_R")
    assert part.points["om1"].value_hz == pytest.approx(350.0)
    assert part.points["om1"].hz_at_1rpm == pytest.approx(350.0 / 20898.0)
    assert any("已保存：Z1_R" in text for text in _captions(at) + [m.value for m in at.success])


# ---------------------------------------------------------------- 验收 3


def test_upload_preview_matches_backend_then_confirm_imports(env):
    at = _app(env)
    payload = ATTACHMENT.read_bytes()
    at.get("file_uploader")[0].set_value(("监测对象_20260309.csv", payload, "text/csv")).run()

    captions = _captions(at)
    assert any(
        "读取 131 行" in text and "将新增 250" in text and "将更新 0" in text for text in captions
    ), captions
    assert any("将更新 250 项，可能影响标记线显示" in w.value for w in at.warning), [
        w.value for w in at.warning
    ]
    assert env.parts() == []  # 预览不入库

    at.checkbox(key="cf_import_confirm").set_value(True).run()
    _button(at, "确认导入").click().run()

    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    parts = env.parts()
    assert len(parts) == 57
    with env.session() as session:
        summary = component_freq.preview_import(session, env.obj.id, ATTACHMENT)
    assert (summary.created, summary.updated, summary.unchanged) == (0, 0, 250)


def test_upload_gbk_source_also_previews(env):
    at = _app(env)
    at.get("file_uploader")[0].set_value(
        ("监测对象_20260309_GBK原始.csv", ROUND_TRIP.read_bytes(), "text/csv")
    ).run()

    assert any("读取 131 行" in text and "将新增 250" in text for text in _captions(at)), _captions(
        at
    )


def test_upload_bad_header_reports_chinese_error(env):
    at = _app(env)
    at.get("file_uploader")[0].set_value(("bad.csv", "序号,名称\n1,2\n".encode(), "text/csv")).run()

    errors = [e.value for e in at.error]
    assert any("缺少列" in text for text in errors), errors
    assert not any("Traceback" in text for text in errors)


# ---------------------------------------------------------------- 验收 4


def test_export_button_round_trips_through_cf1_importer(env):
    env.import_attachment()
    at = _app(env)

    labels = [element.label for element in at.get("download_button")]
    assert "⬇ 导出配置" in labels, labels

    with env.session() as session:
        data = component_freq.export_csv_bytes(session, env.task.id, object_id=env.obj.id)
    exported = Path(str(env.engine.url.database)).parent / "export.csv"
    exported.write_bytes(data)

    # 另一个空对象：把导出的配置导进去，内容应与台账一致（往返 0 变化）
    with env.session() as session:
        other = object_registry.create_object(session, env.task.id, name="空对象", obj_type="轴承")
        summary = component_freq.import_csv(session, other.id, exported)
        again = component_freq.import_csv(session, other.id, exported)
    assert summary.created == 250
    assert (again.created, again.updated, again.unchanged) == (0, 0, 250)
