"""数据接入：扫描采集目录 → 按批次预览 → 导入（落库 + 算指标 + 判有效性）。"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import store_ctx  # noqa: E402
import streamlit as st  # noqa: E402
import ui
from ui import (  # noqa: E402
    flash,
    hint,
    page_header,
    progress_reporter,
    section,
    show_flash,
)

from lion_monitor.config import resolve_scan_dir  # noqa: E402
from lion_monitor.features import ui_settings  # noqa: E402
from lion_monitor.features.dir_browser import (  # noqa: E402
    DirError,
    drives,
    list_subdirs,
    parent_of,
)
from lion_monitor.features.pipeline import (  # noqa: E402
    aggregate_plan,
    import_batch,
    metric_plan,
)
from lion_monitor.ingest.vibration import (  # noqa: E402
    VibrationFormatError,
    scan_vibration_dir_checked,
)
from lion_monitor.store import datasets, tree  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402

logger = logging.getLogger(__name__)

#: 判断异常文案里有没有中文（有中文的是我们自己写的业务错误，可以直接给操作员看）
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _problem_text(batch_name: str, exc: Exception) -> str:
    """把导入异常翻成一条中文可操作提示；SQL / 堆栈只进日志，不上页面（复查 B3）。"""
    message = str(exc).strip()
    if isinstance(exc, (datasets.DatasetError, VibrationFormatError)) and _CJK.search(message):
        return f"{batch_name}：{message}"
    logger.exception("导入批次 %s 失败", batch_name)
    return (
        f"{batch_name}：导入未完成（{type(exc).__name__}），该批次的数据没有正常写入；"
        "请检查目录权限、文件是否被占用、健康树里该通道是否配了指标，再重试一次。"
        "详细原因已写入日志"
    )


# 默认目录放配置模块（lion_monitor.config.DEFAULT_SCAN_DIR），可用环境变量 LION_SCAN_DIR 覆盖
DEFAULT_DIR = resolve_scan_dir(os.environ.get("LION_SCAN_DIR", ""))

task = store_ctx.current_task()
if task is None:
    page_header("数据接入", "还没有测试任务，先去「任务列表」新建一个", demo=False)
    st.stop()

with store_ctx.open_session() as session:
    nodes = tree.list_nodes(session, task.id)
    existing = datasets.list_datasets(session, task.id)

channel_nodes = {n.name: n.id for n in nodes if n.node_type == "channel"}

with store_ctx.open_session() as session:
    plan_rows_src = metric_plan(session, task.id)
    plan = aggregate_plan(plan_rows_src)

DIR_KEY = "ingest_dir"
BROWSE_KEY = "ingest_browse_open"
BROWSE_PATH_KEY = "ingest_browse_path"
BROWSE_ERROR_KEY = "ingest_browse_error"
#: 目录列表一行放几个（马哥 2026-11-05：多个文件夹排一行）——
#: 4 列在 1280 宽下每列约 215px，刚好放得下 21 字符的批次名
DIRS_PER_ROW = 4
# 优先级（马哥 2026-11-04）：页面手填 > 上次记住的 > LION_SCAN_DIR > 内置默认
st.session_state.setdefault(DIR_KEY, ui_settings.get_scan_dir() or DEFAULT_DIR)


def _processed_dirs() -> dict[str, int]:
    """当前任务里「目录 → 数据集编号」的映射（判断批次是否已处理）。"""
    with store_ctx.open_session() as session:
        return datasets.processed_dirs(session, task.id)


def _scan_dir(folder: str, processed: dict[str, int]) -> tuple[list[dict], list[str]]:
    """扫描目录并按采集批次分组；返回 (批次列表, 错误列表)。

    抽成函数是为了「点扫描目录」和「选完目录自动扫描」走同一套逻辑。
    """
    root = Path(folder)
    batches: list[dict] = []
    errors: list[str] = []
    if not root.is_dir():
        return batches, [f"目录不存在：{root}"]

    subs = sorted(p for p in root.iterdir() if p.is_dir()) or [root]
    for sub in subs:
        try:
            metas, header_bad = scan_vibration_dir_checked(sub)
        except (VibrationFormatError, NotADirectoryError) as exc:
            errors.append(f"{sub.name}：{exc}")
            continue
        if header_bad:
            # 单个文件头部读不了不影响整批（复查 B3）：这里先点名，导入时跳过这几个文件
            errors.append(
                f"{sub.name}：{len(header_bad)} 个文件头部读不了，导入时会跳过"
                f"（{'、'.join(item.filename for item in header_bad)}）"
            )
        if not metas:
            continue
        done = processed.get(datasets.norm_dir(sub))
        batches.append(
            {
                "批次": sub.name,
                "状态": f"已处理 · {done}" if done else "未处理",
                "_已处理": bool(done),
                "目录": str(sub),
                "采集时间": metas[0].started_at.strftime("%Y-%m-%d %H:%M:%S"),
                "通道": " / ".join(sorted({m.channel for m in metas})),
                "点数": metas[0].sample_count,
                "采样率": metas[0].sample_rate_hz,
                "时长(s)": round(metas[0].duration_s, 2),
                "文件数": len(metas),
            }
        )
    return batches, errors


def _scan_and_flash(path: str) -> None:
    """扫目录并把结果放进 session_state；扫到了就提示条数，没扫到说明原因。"""
    batches, errors = _scan_dir(path, _processed_dirs())
    st.session_state["scan_batches"] = batches
    st.session_state["scan_errors"] = errors
    if batches:
        flash(f"已选择目录并扫描到 {len(batches)} 个批次：{path}")
    else:
        flash(
            f"已选择目录，但没有扫到可导入的批次（{path}）"
            + (f"：{'；'.join(errors)}" if errors else ""),
            icon="⚠️",
            tone="warning",
        )


def _open_browser() -> None:
    """点「浏览目录」：打开页面内的目录浏览器（马哥 2026-11-04）。

    为什么不再用 Windows 原生对话框：局域网多人访问时弹窗出现在**服务端屏幕**上，
    开机自启（服务跑在 session 0）更是根本弹不出来 —— 网页端浏览器任何浏览器都能用。
    """
    st.session_state[BROWSE_KEY] = True
    st.session_state[BROWSE_ERROR_KEY] = ""
    current = (st.session_state.get(DIR_KEY) or "").strip()
    if current and os.path.isdir(current):
        st.session_state[BROWSE_PATH_KEY] = os.path.abspath(current)
    else:
        roots = drives()
        st.session_state[BROWSE_PATH_KEY] = roots[0][1] if roots else os.path.abspath(os.sep)


def _close_browser() -> None:
    st.session_state[BROWSE_KEY] = False
    st.session_state[BROWSE_ERROR_KEY] = ""


def _enter_dir(path: str) -> None:
    st.session_state[BROWSE_PATH_KEY] = path
    st.session_state[BROWSE_ERROR_KEY] = ""


def _use_dir() -> None:
    """「保存」：写回输入框 + 记住（下次打开还是它）+ 立即扫描。"""
    path = (st.session_state.get(BROWSE_PATH_KEY) or "").strip()
    if not path:
        return
    st.session_state[DIR_KEY] = path
    ui_settings.remember_scan_dir(path)
    st.session_state[BROWSE_KEY] = False
    st.session_state[BROWSE_ERROR_KEY] = ""
    _scan_and_flash(path)


def _render_browser() -> None:
    """页面内目录浏览卡片：面包屑 + 上级 + 盘符 + 子目录列表 + 保存。"""
    if not st.session_state.get(BROWSE_KEY):
        return
    path = st.session_state.get(BROWSE_PATH_KEY) or ""
    with st.container(border=True):
        head = st.columns([0.62, 0.19, 0.19], vertical_alignment="center")
        head[0].markdown(f"**浏览：** `{path}`")
        up = parent_of(path)
        head[1].button(
            "↑ 上级",
            width="stretch",
            disabled=up is None,
            on_click=_enter_dir,
            args=(up or path,),
        )
        head[2].button("取消", width="stretch", on_click=_close_browser)

        roots = drives()
        if len(roots) > 1:
            drive_cols = st.columns(min(len(roots), 6))
            for index, (label, root) in enumerate(roots[:6]):
                drive_cols[index].button(
                    label,
                    key=f"ingest_drive_{index}",
                    width="stretch",
                    on_click=_enter_dir,
                    args=(root,),
                )

        error = st.session_state.get(BROWSE_ERROR_KEY) or ""
        if error:
            ui.warning(error)
        try:
            with st.spinner("正在读取目录…"):
                entries = list_subdirs(path)
            st.session_state[BROWSE_ERROR_KEY] = ""
        except DirError as exc:
            entries = []
            st.session_state[BROWSE_ERROR_KEY] = str(exc)
            ui.warning(str(exc))

        if entries:
            with st.container(height=200):
                # 马哥 2026-11-05：多个文件夹排一行，不要一个占一整行
                for start in range(0, len(entries), DIRS_PER_ROW):
                    chunk = entries[start : start + DIRS_PER_ROW]
                    row = st.columns(DIRS_PER_ROW, gap="small", vertical_alignment="center")
                    for offset, entry in enumerate(chunk):
                        row[offset].button(
                            f"📁 {entry.name}",
                            key=f"ingest_dir_{start + offset}",
                            width="stretch",
                            help=entry.name,
                            on_click=_enter_dir,
                            args=(entry.path,),
                        )
        elif not error:
            st.caption("这个目录下没有子文件夹（可以直接选它）")

        foot = st.columns([0.72, 0.28], vertical_alignment="center")
        foot[1].button("保存", width="stretch", on_click=_use_dir)


page_header(
    "数据接入",
    "扫描目录 → 按采集批次分组 → 导入为数据集并计算指标（1 数据集含多通道）",
    badge=(
        f"{len(existing)} 数据集 · {sum(d.file_count for d in existing)} 文件 · "
        f"{len(channel_nodes)} 通道节点"
    ),
    demo=False,
)
show_flash()

with st.container(border=True):
    c1, c2, c3 = st.columns([0.60, 0.20, 0.20], vertical_alignment="bottom")
    folder_text = c1.text_input(
        "数据目录",
        key=DIR_KEY,
        help="每个子目录 = 一个采集批次；当前只解析 envis .dat"
        "（512 B 头 + float32 样本）。可以手填，也可以点「浏览目录」挑",
    )
    c2.button("📁 浏览目录", width="stretch", on_click=_open_browser)
    scan = c3.button("扫描目录", width="stretch")

_render_browser()

if scan:
    ui_settings.remember_scan_dir(folder_text)
    batches_scan, errors_scan = _scan_dir(folder_text, _processed_dirs())
    st.session_state["scan_batches"] = batches_scan
    st.session_state["scan_errors"] = errors_scan

batches = st.session_state.get("scan_batches") or []
errors = st.session_state.get("scan_errors") or []

if errors:
    ui.warning("；".join(errors))


def _short_reason(item) -> str:
    """坏文件原因的短版：去掉「文件损坏：」和文件名前缀，页面上不重复文件名。"""
    text = item.reason
    for prefix in ("文件损坏：", f"{item.filename}: ", f"{item.filename}："):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text


def _render_plan_detail() -> None:
    """指标计算明细：默认折叠，需要时再展开，避免占版面。"""
    plan_rows = [
        {
            "监测对象": row.object_name,
            "通道": row.channel_code,
            "将计算的指标": " / ".join(row.metric_codes) or "（健康树里没挂指标）",
        }
        for row in plan_rows_src
    ]
    no_metric = [
        f"{row.object_name}·{row.channel_code}" for row in plan_rows_src if not row.metric_codes
    ]
    with st.expander(f"指标计算明细（{len(plan_rows_src)} 个通道，按健康树配置）", expanded=False):
        if plan_rows:
            st.dataframe(pd.DataFrame(plan_rows), hide_index=True, width="stretch")
        else:
            st.caption("健康树里还没有通道节点。")
        hint(
            "一行 = 一个监测对象下的一个通道节点；同一通道号挂到多个监测对象时，"
            "各按自己节点的生效参数计算、各存一行（参数指纹与转速都相同则复用同一次计算）"
        )
        if no_metric:
            ui.warning(
                f"这些通道在健康树里没有指标节点，导入时不会产生指标值：{' / '.join(no_metric)}"
                "　→ 去「健康树配置」在通道下加指标节点",
                icon="⚠️",
            )
        if not channel_nodes:
            ui.warning("健康树里还没有通道节点，所有文件都只会入数据集、不写指标值", icon="⚠️")


if not batches:
    section("扫描结果", "勾选要导入的批次；同一目录重复导入不会产生重复数据集")
    st.caption("还没有扫描结果：填好目录后点「扫描目录」")
    _render_plan_detail()
    st.stop()

all_names = [b["批次"] for b in batches]
pending_names = [b["批次"] for b in batches if not b["_已处理"]]
done_names = [b["批次"] for b in batches if b["_已处理"]]

# 标题与说明压成一行：3 个批次时才不会把导入按钮顶到首屏外
st.caption(
    f"**扫描结果** · 共 {len(batches)} 个批次，未处理 {len(pending_names)} 个 · "
    "同一目录重复导入不会产生重复数据集"
)
st.dataframe(
    pd.DataFrame(batches).drop(columns=["目录", "_已处理"]),
    hide_index=True,
    width="stretch",
)

PICK_KEY = "ingest_pick"
# 选项变了（重扫/换目录）就重置选择，避免残留已不存在的值
if PICK_KEY not in st.session_state or any(
    name not in all_names for name in st.session_state[PICK_KEY]
):
    st.session_state[PICK_KEY] = list(pending_names)


def _set_pick(values: list[str]) -> None:
    st.session_state[PICK_KEY] = list(values)


b1, b2, b3, b4, b5 = st.columns([0.19, 0.13, 0.13, 0.36, 0.19], vertical_alignment="center")
b1.button(
    f"全选未处理（{len(pending_names)}）",
    on_click=_set_pick,
    args=(pending_names,),
    disabled=not pending_names,
    width="stretch",
)
b2.button(
    "全选",
    on_click=_set_pick,
    args=(all_names,),
    disabled=not all_names,
    width="stretch",
)
b3.button(
    "全不选",
    on_click=_set_pick,
    args=([],),
    disabled=not all_names,
    width="stretch",
)

picked = b4.multiselect(
    "要导入的批次",
    all_names,
    key=PICK_KEY,
    placeholder="未勾选任何批次",
    label_visibility="collapsed",
)
compute_now = b5.checkbox(
    "导入时计算指标",
    value=True,
    key="ingest_compute",
    help="算哪些指标由健康树里「该通道下挂的指标节点」决定；取消勾选只入数据集（结构级校验），"
    "不写任何指标值，之后配好指标再重导即可",
)
redo = [name for name in picked if name in done_names]
overwrite_values = 0
if redo:
    # 覆盖范围：选中批次里「已处理」的那些数据集，数一下会被覆盖的指标值行数（G9）
    by_dir = {datasets.norm_dir(d.source_dir): d.id for d in existing if d.source_dir}
    redo_ids = [
        by_dir[datasets.norm_dir(batch["目录"])]
        for batch in batches
        if batch["批次"] in redo and datasets.norm_dir(batch["目录"]) in by_dir
    ]
    with store_ctx.open_session() as session:
        overwrite_values = sum(
            metric_store.count_values_for_dataset(session, dataset_id) for dataset_id in redo_ids
        )
    st.info(
        f"选中里有 {len(redo)} 个已处理批次（{' / '.join(redo)}）："
        "重导会按当前健康树配置重算并覆盖指标值",
        icon="ℹ️",
    )

overwrite_ok = True
if redo:
    overwrite_ok = st.checkbox(
        f"确认覆盖：将覆盖 {overwrite_values} 行指标值（旧值先删后按当前参数重算，不可撤销）",
        key="ingest_overwrite_ok",
    )

left, right = st.columns([0.30, 0.70], vertical_alignment="center")
_INPORT_HELP = (
    "逐文件按「健康树里该通道挂的指标节点」+ 该节点的生效参数计算并落库（值按节点存）；"
    "通道号在健康树里找不到同名通道节点时，只入数据集、不产生趋势点。"
)
if left.button(
    f"导入选中批次（{len(picked)}）",
    disabled=not picked or (bool(redo) and not overwrite_ok),
    width="stretch",
    help=_INPORT_HELP,
):
    results = []
    problems: list[str] = []
    todo = [batch for batch in batches if batch["批次"] in picked]
    bar = st.progress(0.0, text=f"准备导入 {len(todo)} 个批次…")
    with store_ctx.open_session() as session:
        for index, batch in enumerate(todo):
            try:
                results.append(
                    import_batch(
                        session,
                        task.id,
                        batch["目录"],
                        channel_nodes,
                        compute=compute_now,
                        progress=progress_reporter(bar, index, len(todo)),
                    )
                )
            except (datasets.DatasetError, VibrationFormatError, ValueError) as exc:
                session.rollback()
                problems.append(_problem_text(batch["批次"], exc))
            except Exception as exc:  # noqa: BLE001 - 兜底：不把 SQL/堆栈丢给操作员
                # 没预料到的异常（写库冲突、文件被占用等）：回滚会话，只给一条中文提示
                session.rollback()
                problems.append(_problem_text(batch["批次"], exc))
    bar.progress(1.0, text=f"导入完成：{len(results)}/{len(todo)} 个批次")
    if problems:
        ui.error("\n\n".join(problems))
    if results:

        def _describe(r) -> str:
            """一个批次的结果文案：含「成功 N 个文件，损坏 K 个（文件名；原因）」。"""
            # 整批文件头部都读不了时没有采集时间（G8）：不要出现空的「 → 无效」
            when = f"{r.collected_at} → " if r.collected_at else ""
            text = (
                f"{when}{r.status}（{r.channels} 通道，{r.metrics_written} 个指标值，"
                f"真算 {r.compute_runs} 次/复用 {r.dedupe_hits} 次）"
                f"，成功 {r.files_ok} 个文件，损坏 {len(r.files_damaged)} 个"
            )
            if r.files_damaged:
                causes = list(dict.fromkeys(_short_reason(item) for item in r.files_damaged))
                if len(causes) == 1:
                    names = "、".join(item.filename for item in r.files_damaged)
                    text += f"（{names}；原因：{causes[0]}）"
                else:
                    # 原因不止一种：逐条点名「哪个通道/文件坏了、为什么」（P1 / S5）
                    detail = "；".join(
                        f"{item.channel_code or item.filename}：{_short_reason(item)}"
                        for item in r.files_damaged
                    )
                    text += f"（{detail}）"
            invalid = [item for item in r.files_bad if not item.damaged]
            if invalid:
                names = "、".join(item.filename for item in invalid)
                causes = list(dict.fromkeys(_short_reason(item) for item in invalid))
                cause_text = f"；原因：{causes[0]}" if len(causes) == 1 else ""
                text += f"，另有 {len(invalid)} 个判无效（{names}{cause_text}）"
            if r.discarded:
                text += "　→ 整批没有有效文件，未保留数据集"
            if r.missing_channels:
                # G2 漏采提示：树上装配了、本批一个文件都没有的通道；只提示，不改状态
                text += (
                    f"，本批缺少通道：{'、'.join(r.missing_channels)}"
                    f"（健康树已装配 {len(channel_nodes)} 路，本批 {r.channels} 路）"
                )
            return text

        summary = "；".join(_describe(r) for r in results)
        unmapped = sorted({code for r in results for code in r.unmapped_channels})
        if unmapped:
            summary += f"；未映射到健康树的通道：{' / '.join(unmapped)}"
        skipped = [
            (f"{s.object_name}·{s.channel_code}·{s.code}", s.reason)
            for r in results
            for s in (r.skipped_metrics or [])
        ]
        if skipped:
            merged: dict[tuple[str, str], int] = {}
            for name, reason in skipped:
                merged[(name, reason)] = merged.get((name, reason), 0) + 1
            detail = " / ".join(
                f"{name}×{count}（{reason}）" if count > 1 else f"{name}（{reason}）"
                for (name, reason), count in merged.items()
            )
            summary += f"；跳过 {len(skipped)} 个指标：{detail}"
        # G12：样本不足一窗 → 不出值并说明原因（点数 / 窗长 / 采样率 / 所需点数）
        short = [
            (f"{s.object_name}·{s.channel_code}·{s.code}", s.reason)
            for r in results
            for s in (r.skipped_short or [])
        ]
        if short:
            merged_short: dict[tuple[str, str], int] = {}
            for name, reason in short:
                merged_short[(name, reason)] = merged_short.get((name, reason), 0) + 1
            detail = " / ".join(
                f"{name}×{count}（{reason}）" if count > 1 else f"{name}（{reason}）"
                for (name, reason), count in merged_short.items()
            )
            summary += f"；样本不足一窗未出值 {len(short)} 个指标：{detail}"
        kept = sum(1 for r in results if not r.discarded)
        dropped = len(results) - kept
        head = f"已导入 {kept} 个数据集"
        if dropped:
            head += f"（另有 {dropped} 个批次整批无有效文件，未保留）"
        # 一个数据集都没进来时它是失败结果，不该显示成绿色成功框
        flash(
            f"{head}：{summary}",
            icon="⚠️" if kept == 0 else "✅",
            tone="error" if kept == 0 else "success",
        )
        st.session_state.pop("scan_batches", None)
        st.rerun()

_render_plan_detail()
