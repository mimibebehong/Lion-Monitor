"""数据集管理：列表 / 通道文件明细 / 有效性状态 / 软删除。"""

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
    kpi_row,
    page_header,
    reset_table_selection,
    section,
    selectable_table,
    show_flash,
)

from lion_monitor.store import datasets, tree  # noqa: E402
from lion_monitor.store import metrics as metric_store

task = store_ctx.current_task()
if task is None:
    page_header("数据集管理", "还没有测试任务，先去「任务列表」新建一个", demo=False)
    st.stop()

with store_ctx.open_session() as session:
    rows = datasets.list_datasets(session, task.id)
    nodes = tree.list_nodes(session, task.id)

channel_codes = [n.name for n in nodes if n.node_type == "channel"]
value_count = 0
if channel_codes:
    with store_ctx.open_session() as session:
        for code in channel_codes:
            value_count += len(metric_store.list_values(session, task.id, code))

page_header(
    "数据集管理",
    "一个数据集 = 一次采集（多通道文件）；删除为软删除，可在回收站恢复",
    badge=f"{len(rows)} 个数据集",
    demo=False,
)
show_flash()

valid_count = sum(1 for r in rows if r.status == "有效")
partial_count = sum(1 for r in rows if r.status == "部分有效")
kpi_row(
    [
        ("数据集", str(len(rows)), None),
        ("通道文件", str(sum(r.file_count for r in rows)), "来自 channel 表"),
        ("指标值", str(value_count), "趋势分析的数据源"),
        (
            "有效数据集",
            str(valid_count),
            f"部分有效 {partial_count} 个 · 判定后的结果" if partial_count else "判定后的结果",
        ),
    ]
)

left, right = st.columns([0.58, 0.42])

with left:
    section("数据集列表", "勾选一行后在右侧看通道文件、改状态或删除")
    if rows:
        frame = pd.DataFrame(
            [
                {
                    "编号": f"DS-{r.id:03d}",
                    "名称": r.name,
                    "采集时间": r.collected_at or "—",
                    "通道": r.channel_count,
                    "文件": r.file_count,
                    "状态": r.status,
                    "备注": r.remark or "—",
                }
                for r in rows
            ]
        )
        picked = selectable_table(frame, key="dataset_table")
    else:
        st.caption("还没有数据集：去「数据接入」扫描目录并导入")
        picked = []

with right:
    if not picked:
        st.info("左侧勾选一个数据集，这里显示它的通道文件与操作", icon="👈")
    else:
        row = rows[picked[-1]]
        b1, b2 = st.columns(2)
        if b1.button("＋ 去数据接入", key="ds_new_mode", width="stretch"):
            st.switch_page("pages/ingest.py")
        b2.button(
            "✕ 取消选中",
            on_click=reset_table_selection,
            args=("dataset_table",),
            key="ds_clear_mode",
            width="stretch",
        )
        section(f"📦 DS-{row.id:03d} {row.name}", f"{row.collected_at}　状态 {row.status}")

        with store_ctx.open_session() as session:
            files = datasets.list_files(session, row.id)

        if files:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "通道": f.channel_code,
                            "文件": f.file_name,
                            "点数": f.points,
                            "采样率": int(f.sample_rate),
                            "大小(MB)": round(f.size_bytes / 1024 / 1024, 2),
                            "状态": f.status,
                        }
                        for f in files
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("这个数据集没有通道文件")

        # 保存 / 删除同一个数据集的入口放在一张卡里、同一排（现场：同类功能不要分散）
        with st.container(border=True):
            new_name = st.text_input("名称", value=row.name, key=f"ds_name_{row.id}")
            cols = st.columns(2)
            status_idx = (
                datasets.DATASET_STATUS.index(row.status)
                if row.status in datasets.DATASET_STATUS
                else 0
            )
            new_status = cols[0].selectbox(
                "状态",
                datasets.DATASET_STATUS,
                index=status_idx,
                key=f"ds_status_{row.id}",
            )
            new_remark = cols[1].text_input("备注", value=row.remark, key=f"ds_remark_{row.id}")

            arm_key = f"ds_del_armed_{row.id}"
            armed = bool(st.session_state.get(arm_key))
            del_key = f"del_ds_btn_{row.id}"
            if armed:
                st.markdown(danger_button_css(del_key), unsafe_allow_html=True)

            b_save, b_del = st.columns(2)
            if b_save.button("保存修改", key=f"ds_save_{row.id}", width="stretch"):
                try:
                    with store_ctx.open_session() as session:
                        datasets.update_dataset(
                            session,
                            row.id,
                            name=new_name,
                            status=new_status,
                            remark=new_remark,
                        )
                except datasets.DatasetError as exc:
                    ui.error(str(exc))
                else:
                    st.session_state.pop(arm_key, None)
                    flash(f"已保存：DS-{row.id:03d} {new_name}（{new_status}）")
                    st.rerun()

            if b_del.button(
                "确认删除？" if armed else "删除数据集",
                key=del_key,
                width="stretch",
                help="软删除该数据集，同时清掉它的通道文件与指标值；点两次确认",
            ):
                if not armed:
                    st.session_state[arm_key] = True
                    st.rerun()
                else:
                    with store_ctx.open_session() as session:
                        counts = datasets.delete_dataset(session, row.id)
                    st.session_state.pop(arm_key, None)
                    flash(
                        f"已软删除 DS-{row.id:03d}，同时清掉 {counts['metric_value']} 个指标值",
                        icon="🗑️",
                    )
                    st.rerun()

            st.caption(f"源目录：`{row.source_dir}`")
