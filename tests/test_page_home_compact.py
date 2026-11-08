"""工作台紧凑化（马哥 2026-11-03「主要信息不需要下拉便能看到，次要信息可适当删除」）。

可自动化的口径：

1. 页头副标题必须短（一句流程指引，不再是 76 字的九步全称）；
2. 「共 N 台设备：严重 0、警告 0…」这类与 KPI / 表格重复的统计行不再显示；
3. 待办每条不再套 `st.container(border=True)`（每条多占 ~40px 边框与内边距），
   改成紧凑列表；
4. 「最近导入」表最多 5 行，并给出总条数（次要信息收成一句话）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "pages" / "home.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import datasets, tasks  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402


class Env:
    def __init__(self, tmp_path, monkeypatch, extra_datasets: int = 0):
        path = tmp_path / "home.db"
        monkeypatch.setenv("LION_DB", str(path))
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        with self.session() as session:
            self.task = tasks.create_task(session, name="工作台紧凑")
            for index in range(extra_datasets):
                datasets.create_dataset(
                    session,
                    self.task.id,
                    name=f"批次 {index}",
                    source_dir=f"/tmp/batch{index}",
                    collected_at=f"2026-11-0{index % 9 + 1} 08:00:00",
                )

    def session(self):
        return store_db.session_scope(store_db.make_session_factory(self.engine))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    instance = Env(tmp_path, monkeypatch, extra_datasets=8)
    import store_ctx

    store_ctx._session_factory.clear()
    yield instance
    store_ctx._session_factory.clear()


def _app(env):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(PAGE), default_timeout=120).run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _captions(at) -> list[str]:
    return [str(item.value) for item in at.caption]


def _markdown(at) -> list[str]:
    return [str(item.value) for item in at.markdown]


def test_header_has_no_subtitle(env):
    """马哥 2026-11-04：注释性文字去掉 —— 页头不再挂副标题。"""
    at = _app(env)

    captions = _captions(at)
    assert not [c for c in captions if "设备健康与待办" in c], captions


def test_no_duplicated_device_summary_caption(env):
    """设备健康下面的「共 N 台设备：严重 0、警告 0…」与 KPI / 表格重复，删掉。"""
    at = _app(env)

    joined = "\n".join(_captions(at))
    assert "台设备：严重" not in joined, joined


def test_todo_rows_are_not_bordered_containers():
    """待办每条不再用带边框容器，避免每行多出边框与内边距。"""
    src = PAGE.read_text(encoding="utf-8")
    start = src.index('section("待办"')
    end = src.index("# ---------------------------------------------------------------- 任务概览")

    block = src[start:end]
    assert "border=True" not in block, "待办每条仍是带边框容器"


def test_recent_import_capped_with_total(env):
    """最近导入只列最新 3 条（紧凑行，不是表格），总数写进折叠条标题。

    2026-11-04：默认**展开**（此前折叠），每条压成 22px 紧凑行（13px 字），展开后仍一屏。
    """
    src = PAGE.read_text(encoding="utf-8")

    assert "最近导入 · {len(recent)} 条（共 {summary.dataset_count} 个数据集）" in src
    assert "expanded=True" in src, "折叠条应默认展开"
    assert "class='rec'" in src, "最近导入应用紧凑行渲染"

    from lion_monitor.features import overview

    assert overview.recent_datasets.__defaults__ == (3,), "默认取最近 3 条"


def test_helper_import_uses_shared_ui() -> None:
    """工作台只用公共组件，别自造提示样式（马哥 2026-11-03「要牢记」）。"""
    src = PAGE.read_text(encoding="utf-8")
    assert "st.toast(" not in src
    assert "st.success(" not in src
