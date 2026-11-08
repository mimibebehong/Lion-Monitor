"""页面共用 UI 组件：页头、进度条、指标卡、表格、徽标。

只做展示，不含业务逻辑。所有页面统一从这里取样式，保证观感一致。
"""

from __future__ import annotations

import base64
import logging
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import streamlit as st

from lion_monitor.ui_staleness import newest_source_mtime, process_started_at, stale_notice

if TYPE_CHECKING:  # pandas 只在类型标注里用：顶层 import 会让首屏白等 ~1.1 s
    import pandas as pd

# ---------------------------------------------------------------- 页头

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


# 2026-11-05：日志必须任何入口都初始化。
# Streamlit 直接访问某个页面 URL（如 /ingest）时不会执行 app/main.py，只执行 pages/ingest.py，
# 结果 main.py 里的 setup_logging 被跳过、logs/lion-monitor.log 完全不写（实测 trace 为空）。
# ui 被每个页面 import，放在这里保证无论从哪个入口进来日志都落盘。
try:
    import store_ctx

    from lion_monitor.logging_setup import setup_logging

    setup_logging(store_ctx.PROJECT_ROOT)
except Exception:  # noqa: BLE001 - 日志初始化失败绝不能挡住页面
    pass

_LOG = logging.getLogger("lion_monitor.ui")


def _stale_notice() -> str | None:
    """源码比服务进程新（worker 刚提交过代码）→ 提示重启，否则 None。"""
    return stale_notice(
        started_at=process_started_at(),
        newest_mtime=newest_source_mtime(_SRC_ROOT),
    )


# ---------------------------------------------------------------- 滚动位置记忆

SCROLL_MEMORY_HTML = """
<script>
(function () {
  const doc = window.parent.document;
  const win = window.parent;
  const store = win.localStorage;
  const main = () => doc.querySelector('[data-testid="stMain"]');
  //: 重跑/切页后禁止回写记忆值的时长（此时 scrollTop 会被清零，写进去就把记忆冲成 0）
  const RESTORE_MS = 1500;
  const TICK_MS = 120;
  const TICKS = 40;  // 5 秒：大页面（趋势页带树 + 图）渲染慢，重试太短会被提前放弃

  const key = () => "ui_scroll:" + win.location.pathname;
  const saved = () => parseInt(store.getItem(key()) || "0", 10);
  const armed = () => Date.now() < (win.__uiScrollUntil || 0);

  // 每次脚本执行都重新挂监听：Streamlit 重跑会重建 iframe，旧监听器随 iframe 一起失效，
  // 只用 document 上的布尔标记会「标记还在、监听已死」。这里存引用，先摘旧再挂新。
  const handlers = win.__uiScrollHandlers || (win.__uiScrollHandlers = {});
  const events = [
    ["pointerdown", "arm", true],
    ["keydown", "arm", true],
    ["drop", "arm", true],
    ["wheel", "cancel", true],
    ["touchmove", "cancel", true],
    ["scroll", "save", true],
  ];
  for (const [name, slot, capture] of events) {
    if (handlers[slot]) doc.removeEventListener(name, handlers[slot], capture);
  }
  if (handlers.tabSave) doc.removeEventListener("pointerdown", handlers.tabSave, true);

  // 用户点了按钮 / 按了键 / 拖入文件 → 先记下当前位置，再让 Streamlit 去重跑
  handlers.arm = () => {
    const m = main();
    if (!m) return;
    store.setItem(key(), String(Math.round(m.scrollTop)));
    win.__uiScrollUntil = Date.now() + RESTORE_MS;
  };
  // 用户自己滚动 / 触摸 → 放弃还原，别跟用户抢
  handlers.cancel = () => {
    if (armed()) win.__uiScrollUntil = 0;
  };
  // 平时的滚动照常记忆，但还原窗口内不写（那是重跑/切页清零造成的假位置）
  handlers.save = () => {
    if (armed()) return;
    const m = main();
    if (m) store.setItem(key(), String(Math.round(m.scrollTop)));
  };

  // ---- tab 记忆：Streamlit 重跑后 st.tabs 会回到第一个页签（2026-11-03 要求回到原 tab）----
  const tabGroups = () => [...doc.querySelectorAll('[data-testid="stTabs"]')];
  const tabStoreKey = (group, index) => {
    const hit = (group.className || "").toString().match(/st-key-([\\w-]+)/);
    return "ui_tab:" + win.location.pathname + ":" + (hit ? hit[1] : "idx" + index);
  };
  handlers.tabSave = (event) => {
    const el = event.target;
    const target = el && el.closest ? el.closest('[role="tab"]') : null;
    if (!target) return;
    const group = target.closest('[data-testid="stTabs"]');
    if (!group) return;
    store.setItem(tabStoreKey(group, tabGroups().indexOf(group)), target.innerText.trim());
  };
  const restoreTabs = () => {
    tabGroups().forEach((group, index) => {
      const want = store.getItem(tabStoreKey(group, index));
      if (!want) return;
      const hit = [...group.querySelectorAll('[role="tab"]')].find(
        (tab) => tab.innerText.trim() === want
      );
      if (hit && hit.getAttribute("aria-selected") !== "true") hit.click();
    });
  };

  doc.addEventListener("pointerdown", handlers.arm, true);
  doc.addEventListener("keydown", handlers.arm, true);
  doc.addEventListener("drop", handlers.arm, true);
  doc.addEventListener("pointerdown", handlers.tabSave, true);
  doc.addEventListener("wheel", handlers.cancel, { capture: true, passive: true });
  doc.addEventListener("touchmove", handlers.cancel, { capture: true, passive: true });
  doc.addEventListener("scroll", handlers.save, true);

  // 还原：每次脚本执行都做一遍（重跑 / 侧栏切页都会执行本脚本，比常驻定时器可靠——
  // 从 iframe 里起的 setInterval 会随 iframe 销毁而失效，实测心跳会停）。
  const target = saved();
  win.__uiScrollUntil = Math.max(win.__uiScrollUntil || 0, Date.now() + RESTORE_MS);
  let tries = 0;
  const tick = () => {
    restoreTabs();  // 先回到原 tab（内容高度可能因此变化），再还原滚动位置
    const m = main();
    if (m && target > 0 && Math.abs(m.scrollTop - target) > 2) m.scrollTop = target;
    if (++tries < TICKS) setTimeout(tick, TICK_MS);
  };
  tick();
})();
</script>
"""


#: 全局按钮样式：正常态不加配色，悬停 / 按下 / 键盘聚焦才变蓝（2026-11-03 定为全站口径）
BUTTON_STYLE_CSS = """
<style>
[data-testid="stMainBlockContainer"] .stButton > button:not(:disabled):hover,
[data-testid="stMainBlockContainer"] .stButton > button:not(:disabled):active,
[data-testid="stMainBlockContainer"] .stButton > button:not(:disabled):focus-visible,
[data-testid="stMainBlockContainer"] [data-testid="stFormSubmitButton"] > button:hover,
[data-testid="stMainBlockContainer"] [data-testid="stFormSubmitButton"] > button:active,
[data-testid="stMainBlockContainer"] [data-testid="stFormSubmitButton"] > button:focus-visible {
    background-color: #4c9be8 !important;
    border-color: #4c9be8 !important;
    color: #ffffff !important;
}
</style>
"""


def scroll_memory() -> None:
    """记住并还原主区域滚动位置（全站通用，page_header 自动注入）。

    任何按钮点击/重跑后页面不应跳回顶部；按页面路径分别记忆，换页各记各的。
    """
    st.iframe(SCROLL_MEMORY_HTML, height=1)


def _task_chip() -> str | None:
    """当前测试任务的短号，用于在页头标明"数据属于哪个任务"。"""
    task = st.session_state.get("current_task")
    if not task:
        return None
    return str(task).split("·")[0].strip()


PAGE_HEADER_CSS = """<style>
.ph-right{display:flex;flex-direction:column;align-items:flex-end;gap:4px}
.ph-chip{font-size:12px;color:#8b949e;line-height:1.3}
.ph-badge{background:#16283a;color:#7cc4f5;padding:3px 10px;border-radius:12px;
font-size:13px;line-height:1.5;display:inline-block}
.ph-demo{color:#e0b060;font-size:12px;line-height:1.3}
[data-testid="stMarkdownContainer"]:has(.ph-right){margin-bottom:0 !important}
[data-testid="stMarkdown"]:has(.ph-right),
[data-testid="stElementContainer"]:has(.ph-right){height:auto !important}
</style>"""


def page_header(
    title: str, subtitle: str = "", badge: str | None = None, demo: bool = True
) -> None:
    """页面标题区：标题 + 说明 + 任务徽标 + 状态徽标 + 示例数据提示。"""
    left, right = st.columns([0.75, 0.25], vertical_alignment="center")
    with left:
        st.markdown(f"## {title}")
        if subtitle:
            st.caption(subtitle)
    with right:
        # 三个标签必须放在**同一个 markdown 块**里：
        # Streamlit 给「含原始 HTML 的 markdown 容器」加了 margin-bottom:-16px，
        # 拆成多个 st.markdown 时父层只分配 24px，两行标签会互相压住（2026-11-04 截图重叠）。
        chip = _task_chip()
        parts: list[str] = []
        if chip:
            parts.append(f"<div class='ph-chip'>📋 任务 {chip}</div>")
        if badge:
            parts.append(f"<div><span class='ph-badge'>{badge}</span></div>")
        if demo:
            parts.append("<div class='ph-demo'>示例数据 · 未接数据库</div>")
        if parts:
            st.markdown(f"<div class='ph-right'>{''.join(parts)}</div>", unsafe_allow_html=True)

    st.markdown(PAGE_HEADER_CSS + BUTTON_STYLE_CSS, unsafe_allow_html=True)
    notice = _stale_notice()
    if notice:
        st.warning(notice, icon="♻️")

    scroll_memory()


# ---------------------------------------------------------------- 进度条

_STEP_ICON = {"done": "✅", "current": "🔵", "todo": "⚪", "warn": "⚠️"}
_STEP_COLOR = {"done": "#66bb6a", "current": "#4c9be8", "warn": "#ffb74d", "todo": "#6e7681"}


def stepper(
    steps: list[tuple[str, str]],
    state: dict[str, str] | None = None,
    wrap: bool = False,
) -> None:
    """九步工作流进度条：**一整块 HTML**（单次 markdown、固定行高、flex 均分）。

    不用 st.columns 逐块排版 —— 九块卡片在紧凑间距下争高度，容器算 66px、
    实际只分到 57px，会压到下面 KPI 的头 9px（2026-11-04 报的重叠）。

    ``wrap=True``：给半宽栏用（2026-11-04 把九步条移进「流程完成度」）——
    卡片不再均分宽度，改按内容撑开、放不下就整块换行，条高随内容 auto。
    """
    state = state or {}
    chips: list[str] = []
    for code, name in steps:
        status = state.get(code, "todo")
        icon = _STEP_ICON.get(status, "⚪")
        color = _STEP_COLOR.get(status, "#9e9e9e")
        border = "#3a4a5c" if status == "current" else "#2a3441"
        flex = "0 0 auto" if wrap else "1"
        pad = "padding:0 8px;" if wrap else ""
        h = "26px" if wrap else "38px"
        chips.append(
            f"<div style='flex:{flex};display:flex;align-items:center;justify-content:center;"
            f"gap:5px;height:{h};box-sizing:border-box;{pad}border:1px solid "
            f"{border};border-radius:7px;font-size:12px;color:{color};font-weight:600;"
            "white-space:nowrap;overflow:hidden'>"
            f"<span style='font-size:13px'>{icon}</span><span>{code} {name}</span></div>"
        )
    st.markdown(
        # Streamlit 会给「含原始 HTML 的 markdown 容器」加 margin-bottom:-16px，
        # 父块因此只分配 24px，38px 的条必然溢出压住下一块（实测重叠 9px）。
        # 用 :has() 精确抵消这条负边距，并让包装层按内容撑高。
        "<style>"
        '[data-testid="stMarkdownContainer"]:has(.flow-strip){margin-bottom:0 !important}'
        '[data-testid="stMarkdown"]:has(.flow-strip),'
        '[data-testid="stElementContainer"]:has(.flow-strip){height:auto !important}'
        "</style>"
        "<div class='flow-strip' style='display:flex;gap:6px;margin:0 0 2px;"
        + ("flex-wrap:wrap;height:auto'>" if wrap else "height:38px'>")
        + "".join(chips)
        + "</div>",
        unsafe_allow_html=True,
    )


#: 状态 → (色值, 文案)，与全站健康口径一致（ok/warn/alert/invalid/unset）
STATE_STYLE = {
    "ok": ("#4caf50", "🟢 健康"),
    "warn": ("#fb8c00", "🟠 预警"),
    "alert": ("#d93a3a", "🔴 告警"),
    "invalid": ("#9c27b0", "🟣 数据异常"),
    "unset": ("#78828f", "⚪ 未设阈值"),
}

TWIN_CSS = """<style>
/* 同 ui.stepper：抵消 Streamlit 给原始 HTML 容器加的 -16px 负边距，
   否则卡片区会被下一块（待办/当前任务）压住 */
[data-testid="stMarkdownContainer"]:has(.twin-stage){margin-bottom:0 !important}
[data-testid="stMarkdown"]:has(.twin-stage),
[data-testid="stElementContainer"]:has(.twin-stage){height:auto !important}
.twin-stage{display:block}
.twin-dev{display:flex;align-items:center;gap:8px;margin:0 0 3px;font-size:13px}
.twin-dev .nm{font-weight:600;color:#e6edf3}
.twin-dev .meta{color:#8b949e;font-size:12px}
.twin-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
gap:9px;margin:0 0 6px}
.twin-fig{position:relative;height:clamp(105px,calc(100vh - 682px),185px);
border-radius:6px;overflow:hidden;background:#0d1117;border:1px solid #2a3441;margin-bottom:5px}
.twin-img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;
transition:opacity .14s ease}
.twin-img.ph{opacity:0}
/* 2026-11-04：预警 / 告警时卡片外框整体变色，不只是左侧色条 */
.twin-card.st-warn{border-color:#fb8c00;box-shadow:0 0 0 1px rgba(251,140,0,.28)}
.twin-card.st-alert{border-color:#d93a3a;box-shadow:0 0 0 1px rgba(217,58,58,.28)}
.twin-fig:hover .twin-img.ph{opacity:1}
.twin-fig:hover .twin-img.dg{opacity:0}
.twin-fig:hover{border-color:var(--c)}
.twin-card{border:1px solid #2a3441;border-left:3px solid var(--c);border-radius:7px;
transition:border-color .15s,box-shadow .15s;
padding:4px 7px;background:#161b22;line-height:1.25}
.twin-top{display:flex;align-items:center;gap:5px;line-height:1.25}
.twin-line{display:flex;align-items:center;gap:6px;margin-top:4px;flex-wrap:wrap}
.twin-name{font-size:13.5px;font-weight:600;color:#e6edf3;letter-spacing:.2px}
.twin-type{font-size:10.5px;color:#9aa7b4;border:1px solid #313c49;border-radius:3px;
padding:0 3px;line-height:1.5}
.twin-badge.inline{position:static;margin-left:auto;background:none;backdrop-filter:none}
.twin-badge{position:absolute;top:5px;right:5px;font-size:10.5px;font-weight:600;color:var(--c);
background:rgba(10,14,20,.86);border:1px solid var(--c);border-radius:4px;padding:0 5px;
line-height:1.6;white-space:nowrap;backdrop-filter:blur(2px)}
.twin-pts{display:flex;flex-wrap:wrap;gap:4px}
.pt{font-size:10.5px;padding:0 5px;border-radius:3px;background:#1a2230;color:#c9d1d9;
border:1px solid #2f3a47;line-height:1.55;white-space:nowrap}
.pt i{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:4px;
background:var(--pc)}
.twin-foot{font-size:10.5px;color:#8b949e;margin-left:auto;white-space:nowrap;
font-variant-numeric:tabular-nums}
.twin-read{display:flex;align-items:baseline;gap:5px;margin-top:5px;
font-variant-numeric:tabular-nums;line-height:1.35}
.twin-read .lab{font-size:10.5px;color:#8b949e}
.twin-read .val{font-size:13px;font-weight:600;line-height:1}
.twin-read .dash{font-size:13px;font-weight:600;color:#8b949e;line-height:1}
.twin-read .dim{font-size:10.5px;color:#8b949e}
</style>"""


#: 监测对象 → 示意图/实拍图关键字（按顺序匹配对象名，先匹配到先用）。
#: 图片来自现场提供的「监测对象与测点安装关系」图，切好放 app/assets/objects/。
OBJECT_IMAGE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("diaphragm_roller", ("隔膜", "过辊")),
    ("infeed_motor", ("牵引", "入片")),
    ("winding_cylinder", ("气缸", "张力")),
    ("transfer_motor", ("移栽", "搬送")),
)

OBJECT_ASSET_DIR = Path(__file__).parent / "assets" / "objects"


@lru_cache(maxsize=32)
def _image_data_uri(slug: str, kind: str) -> str:
    """图片转 data URI（webp 已压到 2~12KB，内嵌比走静态路径稳，不受挂载点影响）。"""
    path = OBJECT_ASSET_DIR / f"{slug}_{kind}.webp"
    if not path.exists():
        return ""
    return "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def object_slug(name: str) -> str:
    """按对象名找图片关键字；找不到返回空串（卡片退化为纯信息版）。"""
    for slug, keys in OBJECT_IMAGE_RULES:
        if any(key in name for key in keys):
            return slug
    return ""


def _figure_html(name: str, color: str, badge: str) -> str:
    """对象主图（示意图，悬停切实拍），状态徽标浮在图片右上角；无图返回空串。"""
    slug = object_slug(name)
    if not slug:
        return ""
    diagram = _image_data_uri(slug, "diagram")
    photo = _image_data_uri(slug, "photo")
    if not diagram and not photo:
        return ""
    html = f"<div class='twin-fig' style='--c:{color}'>"
    if diagram:
        html += f"<img class='twin-img dg' src='{diagram}' alt='{name} 示意图'>"
    if photo:
        html += f"<img class='twin-img ph' src='{photo}' alt='{name} 实拍'>"
    return html + f"<span class='twin-badge' style='--c:{color}'>{badge}</span></div>"


def _point_html(channel) -> str:
    """测点小徽标：状态色点 + 通道名。"""
    # 2026-11-04：测点徽标不再按状态染红/橙，统一用常态灰点；
    # 状态仍由卡片外框 + 图上徽标表达，避免一行里到处是红点。
    return f"<span class='pt' style='--pc:#6b7684'><i></i>{channel.name}</span>"


#: 健康度口径（悬停说明，2026-11-04）
HEALTH_HINT = "健康度 = 该对象最新 5 个有效指标点的 mean(指标值 / 阈值)；1.00 = 踩到严重阈值"


def _health_html(obj) -> str:
    """卡片底部：健康度（1.0 = 严重线）。没有可算的点显示「—」，不假装 0。

    正文只留「健康度 0.62 · 5 点」三段，口径与「无可用点」的解释都放悬停。
    """
    if obj.health_score is None:
        hint = HEALTH_HINT + "；当前对象没有可算的点（未设阈值或全是待重算点）"
        return (
            f"<div class='twin-read' title='{hint}'>"
            "<span class='lab'>健康度</span><span class='dash'>—</span></div>"
        )
    score = float(obj.health_score)
    # 2026-11-04：健康度数字恢复常态色，不再按 0.8 / 1.0 分档染橙/红
    color = "#e6edf3"
    # 比值可能远小于 0.01（指标量级比阈值小很多），别显示成 0.00 假装没值
    text = "&lt;0.01" if 0 < score < 0.01 else f"{score:.2f}"
    return (
        f"<div class='twin-read' title='{HEALTH_HINT}'>"
        "<span class='lab'>健康度</span>"
        f"<span class='val' style='color:{color}'>{text}</span>"
        f"<span class='dim'>· {obj.health_points} 点</span></div>"
    )


def twin_cards(objects: list, devices: list) -> None:
    """设备分组 → 每个监测对象一张「数字孪生卡」。

    objects 来自 ``overview.object_overviews()``，devices 来自 ``overview.device_overviews()``。
    卡片 = 对象图（示意图，悬停切实拍）+ 名称/类型/状态 + 测点徽标 + 指标·超限·待重算 + 最严重读数。
    图按对象名关键字匹配（``OBJECT_IMAGE_RULES``），没有图的对象自动退化成纯信息卡。
    """
    dev_meta = {item.node_id: item for item in devices}
    order: list[int] = []
    grouped: dict[int, list] = {}
    for obj in objects:
        if obj.device_id not in grouped:
            order.append(obj.device_id)
            grouped[obj.device_id] = []
        grouped[obj.device_id].append(obj)

    parts = [TWIN_CSS, "<div class='twin-stage'>"]
    for device_id in order:
        rows = grouped[device_id]
        dev = dev_meta.get(device_id)
        dev_name = rows[0].device_name or "未归设备"
        dev_state = dev.state if dev is not None else "unset"
        color, text = STATE_STYLE.get(dev_state, STATE_STYLE["unset"])
        meta = (
            f"对象 {dev.object_count} · 通道 {dev.channel_count} · "
            f"指标 {dev.metric_count} · 超限 {dev.over_metric_count}"
            if dev is not None
            else f"对象 {len(rows)}"
        )
        parts.append(
            "<div class='twin-dev'>"
            f"<span style='color:{color}'>{text.split()[0]}</span>"
            f"<span class='nm'>{dev_name}</span>"
            f"<span style='color:{color};font-size:12px'>{text.split()[-1]}</span>"
            f"<span class='meta'>{meta}</span></div>"
        )
        cards = [
            "<div class='twin-grid'>",
        ]
        for obj in rows:
            color, text = STATE_STYLE.get(obj.state, STATE_STYLE["unset"])
            pts = "".join(_point_html(ch) for ch in obj.channels)
            kind = f"<span class='twin-type'>{obj.type_label}</span>" if obj.type_label else ""
            reading = _health_html(obj)
            pts_html = pts or "<span class='twin-foot'>无测点</span>"
            figure = _figure_html(obj.name, color, text)
            # 图区能浮徽标就浮（数字孪生观感）；没图的对象退回行内右对齐，不能丢状态
            badge_html = "" if figure else f"<span class='twin-badge inline'>{text}</span>"
            cards.append(
                f"<div class='twin-card st-{obj.state}' style='--c:{color}'>"
                f"{figure}"
                f"<div class='twin-top'><span class='twin-name'>{obj.name}</span>{kind}"
                f"{badge_html}</div>"
                f"<div class='twin-line'><span class='twin-pts'>{pts_html}</span>"
                f"<span class='twin-foot'>指标 {obj.metric_count} · 超限 {obj.over_metric_count}"
                f" · 待 {obj.stale_count}</span></div>"
                f"{reading}"
                "</div>"
            )
        cards.append("</div>")
        parts.append("".join(cards))
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


# ---------------------------------------------------------------- 指标卡


def kpi_row(
    items: list[tuple[str, str, str | None, str | None] | tuple[str, str, str | None]],
) -> None:
    """一行指标卡：items=[(标签, 值, 说明或 None[, 悬停口径])]。

    说明走 caption（一行、可见）；**长口径**放第 4 项，走 `st.metric(help=...)` 的 ⓘ 悬停，
    不占版面 —— caption 在窄列里换行会把整行 KPI 撑高。
    """
    cols = st.columns(len(items))
    for col, item in zip(cols, items, strict=True):
        label, value, note = item[0], item[1], item[2]
        tip = item[3] if len(item) > 3 else None
        col.metric(label, value, help=tip)
        if note:
            col.caption(note)


# ---------------------------------------------------------------- 区块


def section(title: str, note: str = "") -> None:
    st.markdown(f"#### {title}")
    if note:
        st.caption(note)


def hint(text: str, icon: str = "ℹ️") -> None:
    """整行提示块：左侧亮色边线 + 灰字小行高，用来放口径说明这类长文字。

    2026-11-03：「提示性文字设计美观点」——长说明不要塞在窄列里跟按钮抢宽度，
    也不要跟短标签用同一种 caption 样式。整行、浅底、左侧 3px 亮蓝边线，读起来像注释而不像报错。
    """
    body = str(text).replace("\n", "<br>")
    st.markdown(
        "<div style='display:flex;gap:8px;align-items:flex-start;background:#161b22;"
        "border-left:3px solid #4c9be8;border-radius:6px;padding:8px 12px;margin:2px 0 10px;"
        "color:#9aa7b4;font-size:12.5px;line-height:1.65'>"
        f"<span style='flex:0 0 auto;opacity:.85'>{icon}</span>"
        f"<div style='flex:1 1 auto'>{body}</div></div>",
        unsafe_allow_html=True,
    )


def flash(message: str, icon: str = "✅", key: str = "ui_flash", tone: str = "success") -> None:
    """记一条提示，rerun 之后再显示（st.success 直接调用会被 rerun 冲掉）。

    ``tone`` 决定显示成哪种提示框：``success``（默认）/ ``warning`` / ``error``
    —— 例如「整批没有有效文件」不该显示成绿色成功框。

    2026-11-05：报错信息都要进日志（方便日后运维），所以 error / warning
    的提示在这里统一记一条 ``WARNING``（页面上的提示文案 + 调用位置一起留痕）。
    """
    if tone in ("error", "warning"):
        _LOG.warning("界面提示[%s] %s", tone, message, stacklevel=2)
    st.session_state[key] = (message, icon, tone)


def error(message: str, icon: str = "🚨") -> None:
    """页面红框报错：显示 + 落盘（2026-11-05「报错信息都要进 log」）。"""
    _LOG.error("界面报错: %s", message, stacklevel=2)
    st.error(message, icon=icon)


def warning(message: str, icon: str = "⚠️") -> None:
    """页面黄框警告：显示 + 落盘（现场排障要能看到发生过什么）。"""
    _LOG.warning("界面警告: %s", message, stacklevel=2)
    st.warning(message, icon=icon)


def show_flash(key: str = "ui_flash") -> None:
    """显示并清掉上一步记下的提示。"""
    item = st.session_state.pop(key, None)
    if item:
        message, icon, tone = item
        if tone == "error":
            st.error(message, icon=icon)
        elif tone == "warning":
            st.warning(message, icon=icon)
        else:
            st.success(message, icon=icon)


def demo_note(text: str) -> None:
    st.info(text, icon="🧪")


# ---------------------------------------------------------------- 表格


def selectable_table(df: pd.DataFrame, key: str, height: int | None = None) -> list[int]:
    """可多选的只读表格，返回选中行号列表。"""
    kwargs: dict[str, Any] = {}
    if height is not None:
        kwargs["height"] = height
    event = st.dataframe(
        df,
        key=key,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="multi-row",
        **kwargs,
    )
    picked = list(event.selection.rows) if event and event.selection else []
    # 删行后旧选中态仍带着越界行号，直接过滤，避免调用方 rows[picked[-1]] 抛 IndexError
    return [index for index in picked if 0 <= index < len(df)]


def reset_table_selection(key: str) -> None:
    """清空 st.dataframe 的选中态（给「＋ 新建」「取消选中」按钮做 on_click 回调）。

    必须在回调里调用：脚本运行中直接改该 key 会被 Streamlit 拒绝。
    """
    st.session_state[key] = {"selection": {"rows": [], "columns": []}}


DANGER_COLOR = "#d93a3a"


COMPACT_CSS = """<style>
/* 紧凑版式（2026-11-03：数据分析页绘图区不滚动就能看见）。
   只由显式调用 compact_css() 的页面注入 —— 别的页面照旧宽松。
   选择器全走 data-testid，Streamlit 大版本升级后万一失效，效果只是「回到原来的间距」，不坏功能。 */
[data-testid="stMainBlockContainer"] { padding-top: 4rem; padding-bottom: 0; }
[data-testid="stVerticalBlock"] { gap: 0.4rem; }
[data-testid="stCaptionContainer"] { margin: 0; }
[data-testid="stCaptionContainer"] p { line-height: 1.3; margin: 0; }
[data-testid="stWidgetLabel"] { margin-bottom: 0.1rem; min-height: 0; }
[data-testid="stWidgetLabel"] p { font-size: 0.8rem; line-height: 1.2; }
[data-testid="stTabs"] [role="tablist"] { height: 34px; }
[data-testid="stTabs"] [role="tab"] { height: 34px; padding: 0 0.7rem; }
[data-testid="stTabs"] [role="tabpanel"] { padding-top: 0.2rem; }
/* 「显示标记线」这类折叠条：收起时一行 40px → 28px，三个页签都受益 */
[data-testid="stExpander"] summary { min-height: 1.6rem; padding: 0.1rem 0.5rem; }
[data-testid="stExpander"] summary p { font-size: 0.82rem; }
</style>
"""


def compact_css() -> str:
    """返回「紧凑版式」样式；页面里 ``st.markdown(ui.compact_css(), unsafe_allow_html=True)``。

    收的是纯留白（主区上边距 96→64、说明行 38→20px、区块间距 16→9px、页签 40→34px），
    不动控件与图位高度 —— 用户要的是「同样内容一屏放得下」，不是删内容。
    """
    return COMPACT_CSS.strip()


def danger_button_css(key: str) -> str:
    """把某个按钮（``st.button(key=...)``）染成红色确认态。

    用法：``armed`` 时先 ``st.markdown(danger_button_css(key), unsafe_allow_html=True)`` 再画按钮。
    作用域按 Streamlit 生成的 ``st-key-<key>`` 类名，不会牵连同屏其它按钮。
    """
    return (
        "<style>"
        f".st-key-{key} button {{background: {DANGER_COLOR} !important;"
        f"border-color: {DANGER_COLOR} !important;color: #fff !important;}}"
        f".st-key-{key} button:hover {{background: #c22f2f !important;"
        "border-color: #c22f2f !important;}"
        f".st-key-{key} button p {{color: #fff !important; font-weight: 600;}}"
        "</style>"
    )


def progress_reporter(bar: Any, stage_index: int, stage_total: int):
    """把「第 stage_index 个阶段内部的 0~1 进度」换算成整体进度并刷新进度条。

    ``bar`` 是 ``st.progress(...)`` 返回的对象；页面按批次调用：
    ``progress=ui.progress_reporter(bar, idx, len(batches))``，
    单个批次内部再按文件推进，多批次也不会来回跳。
    """
    stages = max(1, int(stage_total))

    def report(fraction: float, text: str = "") -> None:
        inner = max(0.0, min(1.0, float(fraction)))
        overall = (int(stage_index) + inner) / stages
        bar.progress(min(max(overall, 0.0), 1.0), text=text)

    return report


def badge(text: str, tone: str = "ok") -> str:
    """生成状态徽标 HTML。tone: ok / warn / alert / muted。"""
    palette = {
        "ok": ("#14321f", "#7bd88f"),
        "warn": ("#33260f", "#ffb74d"),
        "alert": ("#331619", "#ef7d7d"),
        "muted": ("#1e242c", "#8b949e"),
    }
    bg, fg = palette.get(tone, palette["muted"])
    return (
        f"<span style='background:{bg};color:{fg};padding:2px 8px;border-radius:10px;"
        f"font-size:12px;white-space:nowrap'>{text}</span>"
    )


def status_cell(level: str) -> str:
    """把 pass/warn/fail/ok/alert/none 映射成徽标 HTML。"""
    mapping = {
        "pass": ("通过", "ok"),
        "ok": ("正常", "ok"),
        "warn": ("警告", "warn"),
        "fail": ("失败", "alert"),
        "alert": ("严重", "alert"),
        "none": ("未设阈值", "muted"),
    }
    text, tone = mapping.get(level, (level, "muted"))
    return badge(text, tone)


def empty_state(text: str) -> None:
    st.markdown(
        f"<div style='border:1px dashed #2a3441;border-radius:8px;padding:24px;"
        f"text-align:center;color:#8b949e'>{text}</div>",
        unsafe_allow_html=True,
    )


def kv_block(data: dict[str, Any]) -> None:
    """两列键值展示。"""
    items = list(data.items())
    for i in range(0, len(items), 2):
        cols = st.columns(2)
        for col, (k, v) in zip(cols, items[i : i + 2], strict=False):
            col.markdown(f"**{k}**：{v}")
