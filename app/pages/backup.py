"""数据库备份（系统 › 运维）：整库在线备份 + 最近备份信息（T2）。

备份走 ``sqlite3.Connection.backup()``，页面开着、有人在用时也能安全备份；
默认留在 ``<库文件目录>/backups/``，只保留最近 N 份（N 可在页面上改）。
迁移前 init_db 也会自动备份，备份失败的迁移直接中止——这里给的是「手动多存一份」的入口。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import store_ctx  # noqa: E402
import streamlit as st  # noqa: E402
import ui
from ui import flash, page_header, section, show_flash  # noqa: E402

from lion_monitor.store import backup as store_backup  # noqa: E402

page_header(
    "数据库备份",
    "整库在线备份（可在使用中执行）；迁移前也会自动备份一份",
    badge="运维",
    demo=False,
)
show_flash()


def _size_text(size: int) -> str:
    return store_backup.human_size(size)


db_path = store_ctx.db_path()
db_exists = db_path.is_file()

left, mid, right = st.columns([0.52, 0.2, 0.28], vertical_alignment="center")
with left:
    st.caption(f"库文件：{db_path}")
    if db_exists:
        st.caption(f"当前大小：{_size_text(db_path.stat().st_size)}")
    else:
        ui.warning("库文件还不存在，先启动一次系统让它建库。", icon="⚠️")
with mid:
    keep = st.number_input(
        "保留份数",
        min_value=1,
        max_value=99,
        value=store_backup.DEFAULT_KEEP,
        step=1,
        key="backup_keep",
    )
with right:
    clicked = st.button(
        "立即备份数据库",
        key="backup_now",
        width="stretch",
        disabled=not db_exists,
    )

if clicked:
    try:
        info = store_backup.backup_database(db_path, keep=int(keep))
    except store_backup.BackupError as exc:
        flash(f"备份失败：{exc}", icon="❌", tone="error")
    else:
        flash(
            f"备份完成：{info.path.name}（{info.size_text()}，"
            f"{info.created_at:%Y-%m-%d %H:%M:%S}）→ {info.path}"
        )
    st.rerun()

st.divider()
section("最近备份", "最近一次备份的时间 / 大小 / 路径；旧备份按保留份数自动清理")

latest = store_backup.latest_backup(db_path)
if latest is None:
    st.caption("还没有备份文件；上面的「立即备份数据库」生成的第一个备份会出现在这里。")
else:
    st.markdown(
        f"最近备份：**{latest.created_at:%Y-%m-%d %H:%M:%S}** · {latest.size_text()} · "
        f"`{latest.path}`"
    )
    rows = store_backup.list_backups(latest.path.parent)
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "文件": item.path.name,
                    "时间": item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "大小": item.size_text(),
                }
                for item in rows
            ]
        ),
        hide_index=True,
        width="stretch",
        height=min(38 * len(rows) + 40, 240),
    )
