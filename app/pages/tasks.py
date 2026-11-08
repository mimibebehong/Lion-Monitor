"""测试任务：任务列表（数据库）+ 新建任务。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import store_ctx  # noqa: E402
import streamlit as st  # noqa: E402
import ui
from ui import (  # noqa: E402
    danger_button_css,
    flash,
    page_header,
    section,
    selectable_table,
    show_flash,
)

from lion_monitor.store import metric_def, tasks  # noqa: E402


def switch_current_task(task_id: int) -> None:
    """切当前任务：同时清掉侧边栏下拉的旧值，让它按新任务重建。"""
    store_ctx.set_current_task(task_id)
    st.session_state.pop("task_selector", None)


_HOVER_BG = "#4c9be8"


def _actions_class(task_id: int) -> str:
    """四个操作按钮的容器 class（悬停/按下变蓝，正常态不动样式）。"""
    return f"task-actions-{task_id}"


def _actions_css(task_id: int) -> str:
    key = _actions_class(task_id)
    sel = f".st-key-{key} button"
    return (
        "<style>"
        f"{sel}:hover, {sel}:focus-visible {{background: {_HOVER_BG} !important;"
        f" border-color: {_HOVER_BG} !important; color: #fff !important;}}"
        f"{sel}:hover p, {sel}:focus-visible p {{color: #fff !important;}}"
        f"{sel}:active {{background: #3f86cc !important; border-color: #3f86cc !important;}}"
        "</style>"
    )


def _arm_class(task_id: int) -> str:
    """待确认删除按钮的容器 class（样式只作用于它，不影响全局按钮）。"""
    return f"task-del-arm-{task_id}"


STATUS_TEXT = {
    "draft": "草稿",
    "ready": "已就绪",
    "done": "已完成",
    "archived": "已归档",
}

rows = store_ctx.task_options()
page_header(
    "测试任务",
    "一次测试活动 = 一个任务；监测对象、传感器、指标配置、阈值设置都在任务内定义",
    badge=f"{len(rows)} 个任务",
    demo=False,
)

show_flash()

tab_list, tab_new = st.tabs(["任务列表", "新建任务"], key="task_tabs")

with tab_list:
    if not rows:
        st.info("还没有测试任务。切到「新建任务」建一个。", icon="📋")
    else:
        with store_ctx.open_session() as session:
            usage = {row.id: tasks.task_usage(session, row.id) for row in rows}

        df = pd.DataFrame(
            [
                {
                    "编号": row.code,
                    "名称": row.name,
                    "状态": STATUS_TEXT.get(row.status, row.status),
                    "操作人": row.operator,
                    "创建时间": row.created_at.strftime("%Y-%m-%d %H:%M"),
                    "数据集": usage[row.id]["dataset"],
                    "通道": usage[row.id]["channel"],
                    "指标值": usage[row.id]["metric_value"],
                }
                for row in rows
            ]
        )
        selected = selectable_table(df, key="task_table")

        # 换选中行就取消「待确认删除」状态，避免残留的红色确认按钮
        if st.session_state.get("task_sel_prev") != selected:
            for key in [k for k in st.session_state if str(k).startswith("del_armed_")]:
                st.session_state.pop(key, None)
            st.session_state["task_sel_prev"] = selected

        section("编辑与操作", "选中一行后在此保存、归档、切换或删除（同一处执行）")
        one = len(selected) == 1
        picked = rows[selected[0]] if one else None

        if one:
            with st.container(border=True):
                e1, e2 = st.columns(2)
                new_name = e1.text_input("任务名称", value=picked.name, key=f"t_name_{picked.id}")
                new_status = e2.selectbox(
                    "状态",
                    list(STATUS_TEXT),
                    index=list(STATUS_TEXT).index(picked.status)
                    if picked.status in STATUS_TEXT
                    else 0,
                    format_func=lambda s: STATUS_TEXT[s],
                    key=f"t_status_{picked.id}",
                )
                new_remark = st.text_area(
                    "备注", value=picked.remark, height=70, key=f"t_remark_{picked.id}"
                )

                # 保存 / 切换 / 归档 / 删除 同一排（马哥：放到一行，删除只留一个入口）
                arm_key = f"del_armed_{picked.id}"
                armed = bool(st.session_state.get(arm_key))
                with st.container(key=_actions_class(picked.id)):
                    st.markdown(_actions_css(picked.id), unsafe_allow_html=True)
                    a1, a2, a3, a4 = st.columns(4, vertical_alignment="center", gap="small")
                if a1.button("保存修改", key=f"save_{picked.id}", width="stretch"):
                    with store_ctx.open_session() as session:
                        tasks.update_task(
                            session,
                            picked.id,
                            name=new_name,
                            status=new_status,
                            remark=new_remark,
                        )
                    flash(f"已保存：{picked.code} · {new_name}")
                    st.rerun()
                if a2.button("设为当前任务", key=f"make_current_{picked.id}", width="stretch"):
                    switch_current_task(picked.id)
                    flash(f"当前任务切换为 {picked.code}")
                    st.rerun()
                if a3.button("归档", key=f"archive_{picked.id}", width="stretch"):
                    with store_ctx.open_session() as session:
                        tasks.archive_task(session, picked.id)
                    flash(f"已归档：{picked.code}")
                    st.rerun()
                # 待确认时只让删除按钮变红，蓝色（主色）始终只留「保存修改」一个
                with a4, st.container(key=_arm_class(picked.id)):
                    if armed:
                        st.markdown(
                            danger_button_css(f"task_del_btn_{picked.id}"),
                            unsafe_allow_html=True,
                        )
                    if st.button(
                        "确认删除？" if armed else "删除任务",
                        key=f"delete_{picked.id}",
                        width="stretch",
                        help="点两次删除：第一次进入待确认，第二次执行（可在回收站恢复）",
                    ):
                        if not armed:
                            st.session_state[arm_key] = True
                            st.rerun()
                        with store_ctx.open_session() as session:
                            tasks.soft_delete_task(session, picked.id)
                        if st.session_state.get(store_ctx.TASK_KEY) == picked.id:
                            st.session_state.pop(store_ctx.TASK_KEY, None)
                        st.session_state.pop("task_selector", None)
                        st.session_state.pop(arm_key, None)
                        flash(f"已删除（可在回收站恢复）：{picked.code}")
                        st.rerun()
                if armed:
                    st.markdown(
                        ":red[再点一次「确认删除？」即删除，可在回收站恢复；点别的行自动取消。]"
                    )

        if one:
            st.caption(
                f"已选中：{picked.code} · {picked.name}（{STATUS_TEXT.get(picked.status)}）"
                f"　操作人 {picked.operator}　创建 {picked.created_at:%Y-%m-%d %H:%M}"
            )
        elif len(selected) > 1:
            ui.warning("只能选中一行进行编辑/归档/删除。")
        else:
            st.caption("提示：点表格左侧行号可选中一行。")

        if one:
            section("任务详情", "来自 test_task / dataset / channel / metric_value 表")
            with st.container(border=True):
                d1, d2, d3 = st.columns(3)
                d1.metric("数据集", usage[picked.id]["dataset"])
                d2.metric("通道", usage[picked.id]["channel"])
                d3.metric("指标值", usage[picked.id]["metric_value"])
                if picked.remark:
                    st.caption(f"备注：{picked.remark}")

    section("本任务的配置项", "四项定义都挂在任务下；换任务后各自独立，不共用")
    entries = [
        ("⚙️ 监测对象", "pages/objects.py", "设备/工位台账、额定转速"),
        ("📡 传感器", "pages/sensors.py", "型号、灵敏度、量程、安装"),
        ("📐 指标配置", "pages/metrics.py", "勾选指标 + 逐指标参数"),
        ("🚨 阈值设置", "pages/thresholds.py", "两级阈值 warn / alert"),
    ]
    for col, (label, page, note) in zip(st.columns(4), entries, strict=True):
        with col.container(border=True):
            st.markdown(f"**{label}**")
            st.caption(note)
            if st.button("进入配置", key=f"jump_{page}", width="stretch"):
                st.switch_page(page)

with tab_new:
    section("新建测试任务", "建好后到「监测对象」配置设备，再到「数据接入」导入数据")
    with st.form("new_task"):
        c1, c2 = st.columns(2)
        name = c1.text_input("任务名称 *", placeholder="如：卷绕产线振动监测")
        operator = c2.text_input("操作人", value="mark")
        c3, c4 = st.columns(2)
        status = c3.selectbox("初始状态", ["draft", "ready"], format_func=lambda s: STATUS_TEXT[s])
        remark = st.text_area("备注", placeholder="测点位置、工况说明等", height=80)
        c4.caption("任务号按 TT-YYYYMMDD-NN 自动生成（同日递增）")
        submitted = st.form_submit_button("创建任务")

    if submitted:
        if not name.strip():
            ui.error("任务名称不能为空")
        else:
            with store_ctx.open_session() as session:
                created = tasks.create_task(session, name=name, operator=operator, remark=remark)
                if status != "draft":
                    created = tasks.update_task(session, created.id, status=status)
                # 新任务开箱带三个内置指标（马哥 2026-11-03）：健康树指标下拉直接可用
                metric_def.seed_defaults(session, created.id)
            switch_current_task(created.id)
            flash(f"已创建：{created.code} · {created.name}")
            st.rerun()
