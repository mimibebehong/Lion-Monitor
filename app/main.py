"""Lion-Monitor 前端入口。

负责：页面导航、全局任务上下文、统一提示。
导航分组：监测对象 / 传感器 / 指标配置 / 阈值设置 都归到"测试任务"下，
它们都是当前测试任务内的定义项。
当前任务来自数据库（data/lion.db）；工作台与任务/健康树/阈值等页面同样读库，
回收站读 deletion_log 留痕（真实软删除记录）。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import nav_state
import store_ctx
import streamlit as st

from lion_monitor.logging_setup import setup_logging

# G15：控制台 + 文件（logs/lion-monitor.log，10 MB × 5）。Streamlit 每次 rerun 都会走到
# 这里，setup_logging 是幂等的，不会重复挂 handler。
_LOG_FILE = setup_logging(store_ctx.PROJECT_ROOT)
_LOGGER = logging.getLogger("lion_monitor.page")

st.set_page_config(
    page_title="Lion-Monitor 锂电产线振动监测",
    # 浏览器标签页用和 EXE/安装包同一张图标（app\assets\lion_icon.png，128 px）
    page_icon=str(Path(__file__).resolve().parent / "assets" / "lion_icon.png"),
    layout="wide",
    initial_sidebar_state="expanded",
)


NAV = {
    "总览": [
        st.Page("pages/home.py", title="工作台", icon="🏠", default=True),
    ],
    "测试任务": [
        st.Page("pages/tasks.py", title="任务列表", icon="📋"),
        st.Page("pages/health_tree.py", title="健康树配置", icon="🌳"),
        st.Page("pages/objects.py", title="监测对象", icon="⚙️"),
        st.Page("pages/sensors.py", title="传感器", icon="📡"),
        st.Page("pages/metrics.py", title="指标配置", icon="📐"),
        st.Page("pages/thresholds.py", title="阈值设置", icon="🚨"),
    ],
    "数据与分析": [
        st.Page("pages/ingest.py", title="数据接入", icon="📥"),
        st.Page("pages/trends.py", title="趋势分析", icon="📈"),
        st.Page("pages/datasets.py", title="数据集管理", icon="🗂️"),
        st.Page("pages/channel_analysis.py", title="数据分析", icon="🔍"),
    ],
    "系统": [
        st.Page("pages/backup.py", title="数据库备份", icon="💾"),
        st.Page("pages/recycle_bin.py", title="回收站", icon="♻️"),
    ],
}

with st.sidebar:
    st.markdown("### 🦁 Lion-Monitor")
    st.caption("锂电产线振动监测")

    task_rows = store_ctx.task_options()
    if task_rows:
        active = store_ctx.current_task()
        labels = [f"{row.code} · {row.name}" for row in task_rows]
        default_index = next(
            (i for i, row in enumerate(task_rows) if active and row.id == active.id), 0
        )
        chosen = st.selectbox("当前测试任务", labels, index=default_index, key="task_selector")
        st.session_state["current_task"] = chosen
        store_ctx.set_current_task(task_rows[labels.index(chosen)].id)
    else:
        st.session_state.pop("current_task", None)
        st.warning("还没有测试任务，先到「任务列表」新建。", icon="📋")
    st.caption("监测对象 / 传感器 / 指标配置 / 阈值设置 都属于当前任务，各自独立")
    st.divider()

# 2026-11-05：页面里任何未捕获异常都要落盘（现场要能回溯）。
# Streamlit 自己只往终端打「Uncaught app execution」，服务模式没有终端就丢了；
# 这里兜一层 —— 记录后原样抛出，页面上的报错框照常显示。
# LION_PROFILE=1 时额外把每个页面的脚本耗时写进日志，用来定位「点一下要等好几秒」。
_PROFILE = os.environ.get("LION_PROFILE") == "1"
_page = st.navigation(NAV, position="sidebar")

# 2026-11-05：点导航进另一个页面时，要回到这个页面的默认样子，
# 而不是上次离开时的样子（上次选中的树节点 / 展开状态 / 页签 / 筛选条件）。
# 规则在 nav_state（纯 dict 逻辑，单测在 tests/test_nav_resets_page_state.py）。
nav_state.reset_if_page_changed(
    st.session_state,
    str(getattr(_page, "url_path", "") or getattr(_page, "title", "?")),
)

_started = time.perf_counter()
try:
    _page.run()
except Exception:  # noqa: BLE001 - 兜底：任何异常都先记录再交给 Streamlit
    _LOGGER.exception("页面执行异常（%s）", os.environ.get("LION_PAGE", "unknown"))
    raise
finally:
    if _PROFILE:
        _LOGGER.info(
            "profile: 页面 %s 脚本耗时 %.0f ms",
            getattr(_page, "title", "?"),
            (time.perf_counter() - _started) * 1000,
        )
