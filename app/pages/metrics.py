"""指标台账（metric_def）：指标码 / 单位 / 计算参数，与通道解耦。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json  # noqa: E402

import pandas as pd  # noqa: E402
import store_ctx  # noqa: E402
import streamlit as st  # noqa: E402
import ui
from ui import (
    danger_button_css,
    flash,
    kpi_row,
    page_header,
    reset_table_selection,
    section,
    selectable_table,
    show_flash,
)  # noqa: E402

from lion_monitor.features import metric_plugins  # noqa: E402
from lion_monitor.params import prepare_params  # noqa: E402
from lion_monitor.store import metric_def  # noqa: E402
from lion_monitor.store.metric_def import MetricDefError  # noqa: E402


def _prepared_defaults(raw: dict) -> tuple[dict, str | None]:
    """定义默认值走与节点参数同一套校验（Q9：这里不再绕过 band_low 下限/窗长规则）。"""
    prepared, problems, note = prepare_params(raw, None)
    if problems:
        raise MetricDefError("默认值校验未通过：" + "；".join(problems))
    return prepared, note


task = store_ctx.current_task()
if task is None:
    page_header("指标配置", "还没有测试任务，先去「任务列表」新建一个", demo=False)
    st.stop()

with store_ctx.open_session() as session:
    rows = metric_def.list_metrics(session, task.id)

page_header(
    "指标配置",
    "主数据台账：定义算哪些指标、用什么参数；装配到通道在「健康树配置」里做",
    badge=f"{len(rows)} 个指标",
    demo=False,
)
show_flash()

kpi_row(
    [
        ("指标", str(len(rows)), None),
        ("启用", str(sum(1 for r in rows if r.enabled)), None),
        ("声明参数", str(sum(1 for r in rows if r.schema)), "带 schema 的指标按声明渲染表单"),
    ]
)

left, right = st.columns([0.58, 0.42])

with left:
    section("指标台账", "选中一行后在右侧改参数")
    if rows:
        table = pd.DataFrame(
            [
                {
                    "指标码": r.code,
                    "单位": r.unit or "—",
                    "默认值": json.dumps(r.defaults, ensure_ascii=False) if r.defaults else "—",
                    "字段": f"{len(r.schema)} 项",
                    "启用": "是" if r.enabled else "否",
                }
                for r in rows
            ]
        )
        picked = selectable_table(table, key="metric_table")
    else:
        st.caption("台账为空：在右侧新建指标（新任务本应自带三个内置指标，可能是被删了）")
        picked = []

    plugins_now, plugin_errors = metric_plugins.load_plugins()
    folder = metric_plugins.plugin_dir()
    # 不传 expanded：让 Streamlit 记住用户的展开/收起，点表格行重跑后不会自己收起
    with st.expander(
        f"🧩 自定义算法（插件）：已加载 {len(plugins_now)} 个 · 放一个 .py 就能新增指标"
    ):
        if plugins_now:
            plugin_table = pd.DataFrame(
                [
                    {
                        "指标码": plugin.code,
                        "中文名": plugin.title,
                        "单位": plugin.unit or "—",
                        "参数项": f"{len(plugin.params_schema)} 项",
                    }
                    for plugin in plugins_now.values()
                ]
            )
            picked_plugin = selectable_table(plugin_table, key="plugin_table")
        else:
            st.caption(f"还没有插件：把接口模板存成 {folder} 下的 .py 文件，再点下方按钮扫描。")
            picked_plugin = []

        plugin = list(plugins_now.values())[picked_plugin[-1]] if picked_plugin else None

        if plugin is not None:
            if plugin.description:
                st.caption(plugin.description)
            st.text_area(
                "参数声明（JSON）",
                value=json.dumps(plugin.params_schema, ensure_ascii=False),
                height=120,
                disabled=True,
                help="由插件文件里的 PARAMS_SCHEMA 决定；要改就改插件文件再重新扫描",
                key=f"plugin_schema_{plugin.code}",
            )
            st.caption(
                f"来源文件：{plugin.path.name if plugin.path else '—'}　单位 {plugin.unit or '—'}"
            )

        # 三个操作同一排、等宽（2026-11-03：同类操作放一起，注意美观一致）
        code_for_del = plugin.code if plugin is not None else "none"
        arm_key = f"plugin_del_armed_{code_for_del}"
        armed = bool(st.session_state.get(arm_key))
        del_btn_key = f"plugin_del_{code_for_del}"
        if armed:
            st.markdown(danger_button_css(del_btn_key), unsafe_allow_html=True)

        b_scan, b_import, b_del = st.columns(3)
        if b_scan.button("扫描算法", key="rescan_plugins", width="stretch"):
            metric_plugins.load_plugins(force=True)
            flash("已扫描插件目录")
            st.rerun()
        if b_import.button("＋ 导入算法（.py）", key="open_import", width="stretch"):
            st.session_state["plugin_import_open"] = not st.session_state.get("plugin_import_open")
        if b_del.button(
            "确认删除算法？" if armed else "删除算法（.py）",
            key=del_btn_key,
            width="stretch",
        ):
            if plugin is None:
                ui.error("先在表格里选中一个算法")
            elif not armed:
                with store_ctx.open_session() as session:
                    usage = metric_def.code_usage(session, plugin.code)
                if usage:
                    ui.error(
                        f"还有 {len(usage)} 个任务台账在用「{plugin.code}」："
                        f"{'、'.join(usage)}；先在指标台账删掉这些指标，再来删算法"
                    )
                    st.session_state.pop(arm_key, None)
                else:
                    st.session_state[arm_key] = True
                    st.rerun()
            else:
                try:
                    removed_name = metric_plugins.remove_plugin(plugin.code)
                except metric_plugins.PluginError as exc:
                    ui.error(str(exc))
                else:
                    st.session_state.pop(arm_key, None)
                    flash(f"已删除算法文件：{removed_name}")
                    st.rerun()

        if st.session_state.get("plugin_import_open"):
            seq = int(st.session_state.get("plugin_upload_seq", 0))
            uploads = st.file_uploader(
                "选择 .py 算法文件（可多选，一次导入多个）",
                type=["py"],
                accept_multiple_files=True,
                key=f"plugin_upload_{seq}",
                help="导入前先校验 CODE 与 compute；单个文件就是只导一个",
            )
            overwrite = st.checkbox("覆盖同名文件", key=f"plugin_overwrite_{seq}")
            if uploads:
                done: list[str] = []
                failed: list[str] = []
                for upload in uploads:
                    try:
                        imported = metric_plugins.install_plugin(
                            upload.name, upload.getvalue(), overwrite=overwrite
                        )
                    except metric_plugins.PluginError as exc:
                        failed.append(f"{upload.name}：{exc}")
                    else:
                        done.append(imported.code)
                if done:
                    st.session_state["plugin_upload_seq"] = seq + 1
                    st.session_state["plugin_import_open"] = False
                    note = f"；未导入 {len(failed)} 个（{'；'.join(failed)}）" if failed else ""
                    flash(f"已导入 {len(done)} 个算法：{'、'.join(done)}{note}")
                    st.rerun()
                else:
                    ui.error("未导入：" + "；".join(failed))
            st.caption(
                f"接口模板：{folder / 'kurtosis.py'} 的写法（CODE / UNIT / TITLE / "
                "PARAMS_SCHEMA + compute）"
            )

        if plugin_errors:
            ui.warning(
                "以下文件加载失败（跳过，不影响其他指标）：\n"
                + "\n".join(f"- `{path.name}`：{text}" for path, text in plugin_errors)
            )

with right:
    if picked:
        row = rows[picked[-1]]
        b1, b2 = st.columns(2)
        b1.button(
            "＋ 新建指标",
            on_click=reset_table_selection,
            args=("metric_table",),
            key="met_new_mode",
        )
        b2.button(
            "✕ 取消选中",
            on_click=reset_table_selection,
            args=("metric_table",),
            key="met_clear_mode",
        )
        section(f"📐 {row.code}", f"ID {row.id}　单位 {row.unit or '—'}")
        with st.container(border=True):
            unit = st.text_input("单位", value=row.unit, key=f"met_unit_{row.id}")
            defaults = st.text_area(
                "默认值（JSON）",
                value=json.dumps(row.defaults, ensure_ascii=False),
                height=70,
                help="节点未覆盖时用这套值；改这里 → 未固化且未覆盖的节点跟着变",
                key=f"met_defaults_{row.id}",
            )
            with st.expander(f"参数声明（{len(row.schema)} 项字段）", expanded=False):
                schema_text = st.text_area(
                    "参数声明（JSON）",
                    value=json.dumps(row.schema, ensure_ascii=False, indent=2),
                    height=180,
                    help="key/label/group/type/unit/default/min/max/options/help 由这里声明",
                    key=f"met_schema_{row.id}",
                )
            enabled = st.checkbox("启用", value=row.enabled, key=f"met_enabled_{row.id}")

            st.divider()
            # 最下面一排：保存修改 | 从台账移除（同一排、真等宽）
            # 移除二合一（与传感器 / 监测对象 / 数据集 / 任务页一致）：
            # 第一次变红「确认移除？」，第二次才真删
            met_arm_key = f"del_metric_armed_{row.id}"
            met_armed = bool(st.session_state.get(met_arm_key))
            save_col, del_col = st.columns(2, vertical_alignment="center", gap="small")
            if save_col.button("保存修改", key=f"save_metric_{row.id}", width="stretch"):
                try:
                    parsed_defaults = json.loads(defaults) if defaults.strip() else {}
                    parsed_schema = json.loads(schema_text) if schema_text.strip() else []
                    if not isinstance(parsed_defaults, dict):
                        raise MetricDefError("默认值必须是 JSON 对象")
                    if not isinstance(parsed_schema, list):
                        raise MetricDefError("参数声明必须是 JSON 数组")
                    prepared, note = _prepared_defaults(parsed_defaults)
                    with store_ctx.open_session() as session:
                        updated = metric_def.update_metric(
                            session,
                            row.id,
                            unit=unit,
                            defaults=prepared,
                            schema=parsed_schema,
                            enabled=enabled,
                        )
                except (MetricDefError, json.JSONDecodeError) as exc:
                    ui.error(str(exc))
                else:
                    st.session_state.pop(met_arm_key, None)
                    parts = [f"已保存：{row.code}"]
                    if note:
                        parts.append(note)
                    if updated.stale_marked:
                        # 复查 v2 问题 3：定义默认值变了 → 跟随定义的节点旧值待重算
                        parts.append(
                            f"定义默认值已变，{updated.stale_marked} 条历史值标为「待重算」"
                            "（去趋势页重算后按新参数出数）"
                        )
                    flash("；".join(parts))
                    st.rerun()

            with del_col:
                if met_armed:
                    st.markdown(
                        danger_button_css(f"del_metric_btn_{row.id}"),
                        unsafe_allow_html=True,
                    )
                if st.button(
                    "确认移除？" if met_armed else "从台账移除",
                    key=f"del_metric_btn_{row.id}",
                    width="stretch",
                    help=(
                        "点两次移除：第一次进入待确认，第二次执行。只把这条指标定义移出台账（软删除），"
                        "算法实现保留；要彻底删算法去下方插件区"
                    ),
                ):
                    if not met_armed:
                        st.session_state[met_arm_key] = True
                        st.rerun()
                    with store_ctx.open_session() as session:
                        try:
                            metric_def.delete_metric(session, row.id)
                        except MetricDefError as exc:
                            ui.error(str(exc))
                        else:
                            st.session_state.pop(met_arm_key, None)
                            flash("已删除指标", icon="🗑️")
                            st.rerun()
    else:
        with st.container(border=True):
            section("新增指标", "指标码从「可用算法」里选；参数用 JSON")
            plugins_loaded, _plugin_errors = metric_plugins.load_plugins()
            available = metric_plugins.available_codes()
            code = st.selectbox(
                "指标码",
                list(available),
                format_func=lambda c: (
                    f"{c}（{plugins_loaded[c].title}）"
                    if c in plugins_loaded
                    else f"{c} · {available[c]}"
                ),
                key="new_metric_code",
            )
            chosen = plugins_loaded.get(code)
            unit = st.text_input(
                "单位", value=available.get(code, ""), key=f"new_metric_unit_{code}"
            )
            defaults = st.text_area(
                "默认值（JSON）",
                value="{}",
                height=70,
                help="节点未覆盖时生效",
                key=f"new_metric_defaults_{code}",
            )
            schema_text = st.text_area(
                "参数声明（JSON）",
                value=json.dumps(chosen.params_schema, ensure_ascii=False) if chosen else "[]",
                height=90,
                help="留空 [] 表示该指标没有参数表单（按系统默认计算）",
                key=f"new_metric_schema_{code}",
            )
            if chosen is not None and chosen.description:
                st.caption(f"插件说明：{chosen.description}")
            if st.button("创建指标", key="create_metric"):
                try:
                    parsed = json.loads(defaults) if defaults.strip() else {}
                    parsed_schema = json.loads(schema_text) if schema_text.strip() else []
                    prepared, note = _prepared_defaults(parsed)
                    with store_ctx.open_session() as session:
                        created = metric_def.create_metric(
                            session, task.id, code, unit, schema=parsed_schema, defaults=prepared
                        )
                except (MetricDefError, json.JSONDecodeError) as exc:
                    ui.error(str(exc))
                else:
                    flash(f"已创建指标：{created.code}" + (f"；{note}" if note else ""))
                    st.rerun()
