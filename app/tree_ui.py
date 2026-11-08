"""健康树公共渲染（经典文件树样式）。

健康树页与趋势页共用，避免重复实现：
- 输入是调用方传入的嵌套快照（``store.tree.tree_snapshot``），本模块不读库、不造数据
- 展开/选中状态存 st.session_state，页面切换不丢
- 顶级设备可带显示勾选框：取消勾选只隐藏该设备子树，行本身保留
- 样式用 CSS 限定在 st.container(key=box_key) 内，不影响页面其他控件
"""

from __future__ import annotations

import html

import streamlit as st

STATE_TEXT = {
    "ok": "正常",
    "warn": "警告",
    "alert": "严重",
    "invalid": "数据异常",
    "unset": "未设阈值",
}
STATE_COLOR = {
    "ok": "#66bb6a",
    "warn": "#ffb74d",
    "alert": "#ef5350",
    "invalid": "#ab47bc",
    "unset": "#8b949e",
}

TYPE_TEXT = {
    "task": "测试任务",
    "device": "设备",
    "component": "监测对象",
    "channel": "通道",
    "metric": "监测指标",
    "threshold": "阈值",
}
TYPE_ICON = {
    "task": "🗂",
    "device": "🏭",
    "component": "⚙️",
    "channel": "📡",
    "metric": "📐",
    "threshold": "🚨",
}
CHILD_OF = {
    "task": "device",
    "device": "component",
    "component": "channel",
    "channel": "metric",
    "metric": "threshold",
}


def walk(node: dict, depth: int = 0):
    """深度优先遍历树快照，产出 (节点, 层级)；子节点缺 key 视为叶子。"""
    yield node, depth
    for child in node.get("children") or []:
        yield from walk(child, depth + 1)


def state_of(node: dict) -> str:
    """节点健康状态：优先用调用方注入的 ``state``，没注入或值非法一律按「未设阈值」。

    真实状态由 ``features.health.node_health`` 算出后注入（趋势页就是这么做的）；
    本函数不猜数值，也不回落到任何演示数据。
    """
    state = node.get("state")
    return state if state in STATE_TEXT else "unset"


_CSS = """
<style>
.st-key-@@BOX@@ button {
    justify-content: flex-start !important;
    text-align: left !important;
    border: none !important;
    background: transparent !important;
    padding: 0 2px !important;
    min-height: 22px !important;
    height: 22px !important;
    font-size: 13px !important;
    font-family: "Consolas", "DejaVu Sans Mono", monospace;
    color: #d5dde7 !important;
    line-height: 22px !important;
}
.st-key-@@BOX@@ button:hover { background: #1e2a36 !important; }
.st-key-@@BOX@@ button[kind="primary"] { background: #21384d !important; font-weight: 600; }
.st-key-@@BOX@@ [data-testid="stHorizontalBlock"] { gap: 0 !important; }
.st-key-@@BOX@@ [data-testid="stVerticalBlock"] { gap: 0 !important; }
.st-key-@@BOX@@ { position: relative; }
.st-key-@@BOX@@ .tree-help {
    position: absolute; top: 4px; right: 8px; z-index: 3;
    font-size: 12px; line-height: 1; color: #6e7681; cursor: help;
}
.st-key-@@BOX@@ .tree-help:hover { color: #4c9be8; }
</style>
"""


def render_tree(
    tree: dict,
    state_key: str = "tree_state",
    box_key: str = "treebox",
    device_checks: bool = True,
    caption: str = "＋/－ 展开折叠；点节点名称选中",
    hide_types: set[str] | None = None,
    caption_help: str = "",
    fragment: bool = False,
) -> str:
    """渲染健康树，返回选中的节点 id。

    hide_types：要隐藏的节点类型（例如趋势页不显示 threshold），
    隐藏后其父节点按实际可见子节点决定是否显示 ＋/－。

    ``caption_help``：把操作说明做成容器右上角的 ⓘ tooltip（**不占版面高度**），
    传了它就不再渲染 ``caption`` 那一行（马哥 2026-11-03：提示信息要简化）。

    ``fragment``：把整棵树包进 ``st.fragment``（马哥 2026-11-05「点一下要等两三秒」）：
    展开/折叠、勾设备只重跑**树本身**，不再把右侧面板和整页元素重建一遍；
    点名称改选中仍要整页重跑（右侧详情得跟着换）。
    """
    st.markdown(_CSS.replace("@@BOX@@", box_key), unsafe_allow_html=True)
    hidden = hide_types or set()

    expanded_key = f"{state_key}__expanded"
    if expanded_key not in st.session_state:
        st.session_state[expanded_key] = {
            node["id"] for node, _ in walk(tree) if node.get("children")
        }
    if state_key not in st.session_state:
        st.session_state[state_key] = tree["id"]
    if device_checks:
        for node in tree.get("children", []):
            if node["type"] == "device":
                st.session_state.setdefault(f"{state_key}__show_{node['id']}", True)

    expanded: set[str] = st.session_state[expanded_key]
    # 片段里 st.rerun() 默认仍是整页（scope="app"）；只有展开/折叠才缩到本片段
    toggle_scope = "fragment" if fragment else "app"

    def render(node: dict, prefixes: list[bool], is_last: bool, depth: int) -> None:
        nid = node["id"]
        kids = [c for c in node.get("children", []) if c["type"] not in hidden]
        has_kids = bool(kids)
        is_open = nid in expanded
        c1, c2, c3, c4 = st.columns([0.07, 0.06, 0.71, 0.16], vertical_alignment="center")

        if has_kids:
            if c1.button("−" if is_open else "+", key=f"{state_key}__tog_{nid}"):
                if is_open:
                    expanded.discard(nid)
                else:
                    expanded.add(nid)
                st.rerun(scope=toggle_scope)
        else:
            c1.markdown("<div style='height:22px'></div>", unsafe_allow_html=True)

        if device_checks and node["type"] == "device":
            shown = c2.checkbox(
                f"呈现 {node['name']}",
                key=f"{state_key}__show_{nid}",
                label_visibility="collapsed",
                help="取消勾选只隐藏该设备的子树，不影响数据",
            )
        else:
            c2.markdown("<div style='height:22px'></div>", unsafe_allow_html=True)
            shown = True

        if depth == 0:
            branch = ""
        else:
            branch = "".join("│  " if not last else "   " for last in prefixes)
            branch += "└─" if is_last else "├─"

        if node["type"] == "threshold":
            attrs = node["attrs"]
            text = f"{attrs['方向']}{attrs['warn']} / {attrs['方向']}{attrs['alert']}"
            icon = "🚨"
        else:
            text = node["name"]
            icon = "📁" if has_kids else "📄"
        if not shown:
            text = f"{text}（已隐藏）"

        if c3.button(
            f"{branch} {icon} {text}",
            key=f"{state_key}__sel_{nid}",
            type="primary" if st.session_state[state_key] == nid else "secondary",
        ):
            st.session_state[state_key] = nid
            st.rerun()

        state = state_of(node)
        color = STATE_COLOR.get(state, "#9e9e9e")
        label = "—" if node["type"] == "threshold" else STATE_TEXT[state]
        c4.markdown(
            f"<div style='font-size:12px;white-space:nowrap;color:{color}'>● {label}</div>",
            unsafe_allow_html=True,
        )

        if has_kids and is_open and shown:
            for i, child in enumerate(kids):
                render(child, [*prefixes, is_last], i == len(kids) - 1, depth + 1)

    def _body() -> None:
        with st.container(border=True, key=box_key):
            if caption_help:
                st.markdown(
                    "<div class='tree-help' title=\"" + html.escape(caption_help) + '">ⓘ</div>',
                    unsafe_allow_html=True,
                )
            render(tree, [], True, 0)
            if caption and not caption_help:
                st.caption(caption)

    if fragment:
        st.fragment(_body, key=f"tree_frag_{box_key}")()
    else:
        _body()

    return str(st.session_state[state_key])
