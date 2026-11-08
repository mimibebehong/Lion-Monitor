"""工作台：九步进度、设备健康概览、待办、当前任务、最近导入。

本页所有数字都来自 ``data/lion.db``（任务 / 健康树 / 数据集 / 指标值 / 阈值），
统计口径见 ``lion_monitor.features.overview``。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store_ctx  # noqa: E402
import streamlit as st  # noqa: E402
from ui import (  # noqa: E402
    compact_css,
    empty_state,
    kpi_row,
    page_header,
    section,
    stepper,
    twin_cards,
)

from lion_monitor.features import overview  # noqa: E402

task = store_ctx.current_task()
with store_ctx.open_session() as session:
    summary = overview.task_overview(session, task.id if task else None)
    recent = overview.recent_datasets(session, task.id) if task else []

st.markdown(compact_css(), unsafe_allow_html=True)
st.markdown(
    "<style>.td-f{white-space:nowrap}"
    ".tr-head{font-size:14px;line-height:1.5;color:#e6edf3}"
    # 最近导入：每行压到 20px 级，展开后也不把页面顶出滚动
    ".rec{display:flex;gap:8px;align-items:baseline;font-size:13px;line-height:1.5;"
    "padding:1px 0;white-space:nowrap}"
    ".rec .c{color:#7cc4f5;font-family:ui-monospace,Menlo,monospace;font-size:12.5px}"
    ".rec .n{color:#e6edf3;max-width:190px;overflow:hidden;text-overflow:ellipsis}"
    ".rec .m{color:#8b949e}"
    ".tr-head .tr-meta{color:#9aa7b4;font-size:12.5px;margin-left:8px}"
    # 与「最近导入」折叠条标题完全一致（Streamlit 段落 0.82rem / #e6edf3 / 400）；
    # 马哥 2026-11-04：流程完成度 9/9 与最近导入的字体颜色和大小保持一致
    ".tr-sub{font-size:0.82rem;color:#e6edf3;margin:6px 0 2px;font-weight:400}"
    # 含原始 HTML 的 markdown 容器带 margin-bottom:-16px，会把下面的九步条压上来
    '[data-testid="stMarkdownContainer"]:has(.tr-head){margin-bottom:0 !important}'
    '[data-testid="stMarkdown"]:has(.tr-head),'
    '[data-testid="stElementContainer"]:has(.tr-head){height:auto !important}'
    ".todo-row{display:block;padding:5px 8px;margin:0 0 3px;border-radius:6px;"
    "border:1px solid transparent;color:#e6edf3;font-size:15px;line-height:1.65;"
    "text-decoration:none !important}"
    # Streamlit 给 markdown 里的 a 加了 underline，权重比 .todo-row 高，必须再压一次
    '[data-testid="stMarkdownContainer"] a.todo-row{text-decoration:none !important}'
    "a.todo-row:hover{text-decoration:none !important}"
    "a.todo-row b{text-decoration:none !important}"
    "a.todo-row .todo-desc{text-decoration:none !important}"
    ".todo-row:hover{color:#4c9be8;background:#141c26;border-color:#20303f}"
    ".todo-row .todo-desc{color:#9aa7b4;font-size:13px}"
    ".todo-row:hover .todo-desc{color:#7cc4f5}"
    "</style>",
    unsafe_allow_html=True,
)

# 页头只留「📋 任务号」：状态在「任务概览」里已有一处，徽标再写一遍就是重复
page_header("工作台", badge="无测试任务" if task is None else None, demo=False)

if task is None:
    st.info("还没有测试任务，先去「任务列表」新建一个。", icon="📋")
    st.stop()

kpi_row(
    [
        ("任务", str(summary.task_count), None),
        ("数据集", str(summary.dataset_count), None),
        ("超限记录", str(summary.over_count), None),
        ("待重算", str(summary.stale_count), None),
    ]
)

# ---------------------------------------------------------------- 设备健康
section("设备健康状态")

if not summary.objects:
    empty_state("当前任务还没有监测对象；先去「健康树配置」把设备、对象与测点装配起来")
else:
    twin_cards(summary.objects, summary.devices)

left, right = st.columns([0.5, 0.5])

# ---------------------------------------------------------------- 待办
with left:
    section("待办")
    todos: list[tuple[str, str, str, str]] = []
    # 振动超限：一个（监测对象 × 通道 × 指标）组合一条，字段顺序见马哥 2026-11-04 示例：
    # 监测对象 · 监测通道 · 监测指标 · 超限等级 · 超限时间 · 超限点数
    for item in summary.over_todos:
        # 状态三级（马哥 2026-11-04）：健康 / 预警 / 告警
        # 等级文字不再染红/橙（马哥同日追加：恢复常态色，状态靠行首图标表达）
        level_text = "告警" if item.level == "alert" else "预警"
        # 每个字段一个 nowrap 块：窄屏只会在字段之间换行，不会把「超限时间」劈成两截
        fields = [
            f"<span class='td-f'>{item.object_name or '未归属对象'}</span>",
            f"<span class='td-f'>{item.channel}</span>",
            f"<span class='td-f'>{item.code}</span>",
            f"<span class='td-f'>{level_text}</span>",
            f"<span class='td-f'>{item.computed_at}</span>",
            f"<span class='td-f'>超限 {item.points} 点</span>",
        ]
        todos.append(("振动超限", " · ".join(fields), "/trends", item.level))
    if summary.pending_validity:
        todos.append(
            (
                "有效性待判",
                f"{summary.pending_validity} 个数据集目前只做了结构级校验",
                "/datasets",
                "warn",
            )
        )
    unset_n = summary.health_counts.get("unset", 0)
    if unset_n:
        todos.append(("阈值未设", f"{unset_n} 个指标节点还没有生效阈值", "/thresholds", "todo"))

    if todos:
        # 马哥 2026-11-04：不要右侧按钮了，整行直接点进对应页面
        for title, desc, route, tone in todos:
            icon = {"warn": "🟠", "alert": "🔴", "todo": "⚪", "done": "✅"}[tone]
            st.markdown(
                f"<a class='todo-row' href='{route}' target='_self' title='点击前往'>"
                f"{icon} <b>{title}</b>　<span class='todo-desc'>{desc}</span></a>",
                unsafe_allow_html=True,
            )
    else:
        empty_state("库里没有越限记录，也没有待判数据集与未设阈值的指标")

# ---------------------------------------------------------------- 任务概览
with right:
    section("任务概览")
    done_steps = sum(1 for _code, _name, state in summary.steps if state == "done")
    total_steps = len(summary.steps)
    # 任务信息与「流程完成度」标题合成**一个** markdown 块：
    # 拆开时含 HTML 的容器各带 margin-bottom:-16px，会互相压住（马哥 2026-11-04 报的重叠）。
    st.markdown(
        f"<div class='tr-head'><b>{task.code} · {task.name}</b>"
        f"<span class='tr-meta'>操作人 {task.operator} · 创建"
        f" {task.created_at:%Y-%m-%d %H:%M} · 状态 {task.status}</span></div>"
        f"<div class='tr-sub'>🔄 流程完成度 {done_steps}/{total_steps}</div>",
        unsafe_allow_html=True,
    )
    stepper(
        list(overview.STEPS),
        {code: state for code, _name, state in summary.steps},
        wrap=True,
    )

    # 最近导入（马哥 2026-11-04：并入任务概览；次要信息默认折叠）
    if not recent:
        st.caption("最近导入：还没有导入数据集，去「数据接入」导入采集目录")
    else:
        # 马哥 2026-11-04：默认展开（此前默认折叠）
        with st.expander(
            f"最近导入 · {len(recent)} 条（共 {summary.dataset_count} 个数据集）",
            expanded=True,
        ):
            st.markdown(
                "".join(
                    f"<div class='rec'><span class='c'>{row.code}</span>"
                    f"<span class='n'>{row.name}</span>"
                    f"<span class='m'>{row.channel_count} 通道 · {row.status}"
                    f" · {row.collected_at}</span></div>"
                    for row in recent
                ),
                unsafe_allow_html=True,
            )
