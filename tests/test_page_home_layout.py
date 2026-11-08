"""工作台版式（马哥 2026-11-04 四条要求）。

1、「当前任务」→「任务概览」，「最近导入」并入其中；
2、顶部九步条搬进「流程完成度」，不再单独占一行；
3、KPI 去掉「指标值」，并去掉所有注释性说明文字；
4、待办与任务概览各占一半，待办字号放大。
"""

from __future__ import annotations

from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "app" / "pages" / "home.py"


def _src() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_task_section_renamed_and_holds_recent_import():
    src = _src()

    assert 'section("任务概览")' in src
    assert 'section("当前任务")' not in src

    right = src[src.index('section("任务概览")') :]
    assert "最近导入 · {len(recent)} 条" in right, "最近导入要并入任务概览"


def test_stepper_moved_into_progress_block():
    src = _src()

    # 顶部不再有独立的九步条调用
    head = src[: src.index('section("设备健康状态")')]
    assert "stepper(" not in head, "九步条不应再单独占顶部一行"
    # 搬进任务概览，并给半宽栏用 wrap 模式
    right = src[src.index('section("任务概览")') :]
    assert "stepper(" in right
    assert "wrap=True" in right
    assert "流程完成度" in right
    # 马哥 2026-11-04：标题要带完成数，例如「流程完成度 9/9」
    assert "<div class='tr-sub'>🔄 流程完成度 {done_steps}/{total_steps}</div>" in right
    assert "st.progress(" not in right, "进度条已被九步条替代"


def test_kpi_drops_metric_value_and_captions():
    src = _src()
    kpi = src[
        src.index("kpi_row(") : src.index(
            "# ---------------------------------------------------------------- 设备健康"
        )
    ]

    assert "指标值" not in kpi
    assert "超限记录" in kpi
    for row in ("通道 {", "库内有效计算点", "按阈值逐点判定", "由旧参数算出"):
        assert row not in kpi, f"KPI 仍带注释性文字：{row}"


def test_todo_and_task_overview_split_evenly():
    src = _src()

    assert "st.columns([0.5, 0.5])" in src


def test_todo_font_is_larger():
    src = _src()

    assert "font-size:15px;line-height:1.65" in src
    assert ".todo-row .todo-desc{color:#9aa7b4;font-size:13px}" in src


def test_recent_import_font_is_one_step_larger():
    """马哥 2026-11-04：最近导入的数据集明细字体再大一号（12 → 13px，编号 11.5 → 12.5px）。"""
    src = _src()

    assert ".rec{display:flex;gap:8px;align-items:baseline;font-size:13px" in src
    assert "font-size:12.5px" in src


def test_progress_label_matches_recent_import_typography():
    """马哥 2026-11-04：「流程完成度 9/9」与「最近导入」标题字体颜色、大小保持一致。

    最近导入是 Streamlit 折叠条段落（0.82rem / #e6edf3 / 400），tr-sub 必须同规格。
    """
    src = _src()

    assert ".tr-sub{font-size:0.82rem;color:#e6edf3;margin:6px 0 2px;font-weight:400}" in src
