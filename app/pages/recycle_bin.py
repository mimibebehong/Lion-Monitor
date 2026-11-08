"""回收站：读 deletion_log 留痕，恢复软删除项。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import store_ctx  # noqa: E402
import streamlit as st  # noqa: E402
from ui import (  # noqa: E402
    empty_state,
    flash,
    page_header,
    section,
    selectable_table,
    show_flash,
)

from lion_monitor.store import recycle  # noqa: E402

with store_ctx.open_session() as session:
    items = recycle.list_recycle_items(session)

page_header(
    "回收站",
    "软删除的数据集、任务与节点在这里恢复；留痕来自 deletion_log",
    badge=f"{len(items)} 项",
    demo=False,
)
show_flash()

if not items:
    empty_state("回收站是空的：在「数据集管理」删除的数据集、「健康树配置」删除的节点会出现在这里")
    rows: list[int] = []
else:
    frame = pd.DataFrame(
        [
            {
                "类型": item.entity_label,
                "名称": item.entity_name,
                "删除时间": item.deleted_at.strftime("%Y-%m-%d %H:%M"),
                "剩余天数": (
                    f"{item.remaining_days} 天" if item.remaining_days is not None else "—"
                ),
                "级联": (
                    f"通道 {item.cascade.get('channel', 0)} / "
                    f"指标值 {item.cascade.get('metric_value', 0)}"
                    if item.entity_type == "dataset"
                    else (
                        f"节点 {sum(item.cascade.values())}"
                        if item.entity_type == "task_node"
                        else "—"
                    )
                ),
                "操作人": item.operator or "—",
            }
            for item in items
        ]
    )
    rows = selectable_table(frame, key="recycle_table")

c1, c2, c3, _ = st.columns([0.14, 0.16, 0.30, 0.40])
restore = c1.button("恢复选中", disabled=not rows, width="stretch")
c2.button("彻底删除", disabled=True, width="stretch")
c3.caption(f"保留期 {recycle.RECYCLE_RETENTION_DAYS} 天，过期自动清理；彻底删除按钮本期未实现")

if restore:
    messages: list[str] = []
    failed = 0
    with store_ctx.open_session() as session:
        for idx in rows:
            result = recycle.restore_item(session, items[idx].log_id)
            messages.append(result.message)
            failed += 0 if result.ok else 1
    flash("；".join(messages), icon="♻️" if not failed else "⚠️")
    st.rerun()

st.divider()
section("说明")
st.markdown(
    "- 软删除：写 `deleted_at`，查询默认过滤，可在本页恢复（恢复=把 `deleted_at` 置空，"
    "数据集连同通道、指标值一起回来）\n"
    "- 节点级：删除健康树节点时留痕里存**子树快照**"
    "（节点属性 + 子节点 + 该子树的指标授权 / 阈值），"
    "恢复后子树、授权、阈值一起回来；原父节点已删时恢复到任务根下并提示\n"
    "- 模板整树替换不静默批量删：旧树逐节点各留一条痕，条数 = 被替换节点数\n"
    f"- 保留期 {recycle.RECYCLE_RETENTION_DAYS} 天"
    "（环境变量 `LION_RECYCLE_RETENTION_DAYS` 可覆盖）："
    "应用启动时自动清理过期留痕，并真删对应的软删节点 / 授权 / 阈值行，日志记删除条数\n"
    "- 彻底删除：物理 `DELETE`，由外键 `ON DELETE CASCADE` 兜底清子表，"
    "写 `deletion_log(mode=hard)`——本期未接界面\n"
    "- 原始 `.dat` 文件不受影响，恢复后可直接再分析"
)
