"""部件频率台账（``component_freq``）：附件 CSV 导入导出 + 转速/频率解析（CF-1）。

口径（设计文档 §2.4 / §3.2–3.4，任务书 CF-1 定稿）：

- 归一化存储：``hz_at_1rpm = value_hz / ref_rpm``（等价「阶次 ÷ 60」）
- ``scaling=speed`` 运行期 ``f = hz_at_1rpm × rpm_actual``；``fixed`` 直接用 ``value_hz``
- 唯一键 ``(object_id, part_code, freq_kind)``（``deleted_at IS NULL`` 部分唯一索引）
- 一个部件可被多个通道监测：部件频率记录只有一条，``channel_code`` 存逗号分隔的多值，
  通道显示名落 ``channel_reg.remark``

附件没有转速列，参考转速由该部件 ``OM1 × 60`` 反推：同部件多行 OM1 有精度差异
（如 ``348.3`` / ``348.3333333``）时取出现次数最多的值，并列取小数位最多者
（仍并列取文件中先出现的），规则见 ``_mode_value``。
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..params import resolve_rpm
from . import object_registry, tree
from .db import begin_immediate
from .models import ChannelReg, ComponentFreq, Dataset, MonitorObject

__all__ = [
    "CSV_COLUMNS",
    "FREQ_ATTR_PREFIX",
    "FREQ_KINDS",
    "FREQ_KIND_COLUMNS",
    "FREQ_KIND_LABELS",
    "PART_KIND_OF",
    "SCALING_LABELS",
    "ComponentFreqError",
    "ComponentFreqRow",
    "FreqPoint",
    "ImportSummary",
    "PartFreqRow",
    "ResolvedFreq",
    "export_csv_bytes",
    "export_csv_text",
    "freq_override_key",
    "import_csv",
    "list_channels_of",
    "list_freqs",
    "list_parts",
    "node_freq_overrides",
    "object_id_of_node",
    "parse_freq_overrides",
    "preview_import",
    "resolve_component_freq",
    "resolve_freq_marks",
    "resolve_node_rpm",
    "save_part",
    "set_node_freq_override",
]

#: 台账支持的频率项（与设计文档 §2.4 的 name 列对齐）
FREQ_KINDS: tuple[str, ...] = ("om1", "om2", "omx", "om2x", "bpfi", "bpfo", "ftf", "bsf")

#: 频率项 → 附件列名（逐列写死；BPF0 列名如此，语义按内圈 BPFO）
FREQ_KIND_COLUMNS: dict[str, str] = {
    "om1": "OM1",
    "om2": "OM2",
    "omx": "OMx",
    "om2x": "OM2x",
    "bpfi": "外圈故障特征频率BPFI",
    "bpfo": "内圈故障特征频率BPF0",
    "ftf": "保持架故障特征频率FTF",
    "bsf": "滚动体故障特征频率BSF",
}

#: 频率项中文标签（报告 / 界面用）
FREQ_KIND_LABELS: dict[str, str] = {
    "om1": "1×转频",
    "om2": "2×转频",
    "omx": "啮合频率",
    "om2x": "2×啮合频率",
    "bpfi": "内圈故障特征频率 BPFI",
    "bpfo": "外圈故障特征频率 BPFO",
    "ftf": "保持架故障特征频率 FTF",
    "bsf": "滚动体故障特征频率 BSF",
}

#: 缩放方式 → 界面文案（``fixed`` = 不随转速缩放，如 2× 线频 / 固有频率）
SCALING_LABELS: dict[str, str] = {
    "speed": "随转速缩放",
    "fixed": "固定值（不随转速）",
}

#: 附件「部件类型」→ ``component_kind``
PART_KIND_OF: dict[str, str] = {
    "齿轮": "gear",
    "轴承": "bearing",
    "行星齿轮": "planet",
    "轴": "shaft",
}

#: 导出列序（与附件完全一致）
CSV_COLUMNS: tuple[str, ...] = (
    "序号",
    "振动通道名称",
    "部件类型",
    "监测部件编号",
    "监测部件名称",
    "OM1",
    "OM2",
    "OMx",
    "OM2x",
    "外圈故障特征频率BPFI",
    "内圈故障特征频率BPF0",
    "保持架故障特征频率FTF",
    "滚动体故障特征频率BSF",
    "D",
)

_REQUIRED_COLUMNS: tuple[str, ...] = (*CSV_COLUMNS[:5], *FREQ_KIND_COLUMNS.values(), "D")

_SUMMARY_MAX_DETAILS = 50


class ComponentFreqError(Exception):
    """部件频率台账业务错误（文件编码 / 表头 / 对象不存在等）。"""


@dataclass(frozen=True)
class ComponentFreqRow:
    """一条部件频率记录（``channels`` 由多值通道号拆出）。"""

    id: int
    task_id: int
    object_id: int
    part_code: str
    part_name: str
    part_type: str
    channel_code: str
    component_kind: str
    ref_rpm: float | None
    hz_at_1rpm: float
    scaling: str
    freq_kind: str
    value_hz: float
    geom_d: float | None = None
    note: str = ""
    updated_at: datetime | None = None

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(part for part in self.channel_code.split(",") if part)

    @property
    def order_at_ref(self) -> float | None:
        """参考转速下的阶次（``hz_at_1rpm × 60``）；``fixed`` 无阶次。"""
        if self.scaling != "speed":
            return None
        return self.hz_at_1rpm * 60.0


@dataclass(frozen=True)
class ResolvedFreq:
    """按实际转速解析出的频率值。

    ``source``：``object`` = 对象台账派生（含转速缩放），``node`` = 节点手填覆盖。
    """

    value_hz: float
    freq_kind: str
    part_code: str
    scaling: str
    rpm_used: float | None
    rpm_source: str
    source: str = "object"


@dataclass(frozen=True)
class FreqPoint:
    """部件下的一个频率项（台账原始值 + 归一化值 + 缩放方式）。"""

    id: int
    freq_kind: str
    value_hz: float
    hz_at_1rpm: float
    scaling: str

    @property
    def order(self) -> float | None:
        """参考转速下的阶次（``hz_at_1rpm × 60``）；``fixed`` 无阶次。"""
        if self.scaling != "speed":
            return None
        return self.hz_at_1rpm * 60.0


@dataclass(frozen=True)
class PartFreqRow:
    """一个部件的频率参数视图（``points`` 按 ``FREQ_KINDS`` 顺序）。"""

    part_code: str
    part_name: str
    part_type: str
    component_kind: str
    channels: tuple[str, ...]
    channel_names: tuple[str, ...]
    ref_rpm: float | None
    geom_d: float | None
    points: dict[str, FreqPoint]

    @property
    def channels_text(self) -> str:
        """已挂通道的展示文本（有显示名时「通道号（显示名）」）。"""
        bits = [
            f"{code}（{name}）" if name else code
            for code, name in zip(self.channels, self.channel_names, strict=True)
        ]
        return " / ".join(bits)


@dataclass
class ImportSummary:
    """导入报告：新增 / 真的变了 / 命中未变 / 跳过（含行号原因）/ 部件数 / 新通道数。"""

    rows_read: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    parts: int = 0
    channels: int = 0
    skipped_details: list[str] = field(default_factory=list)

    @property
    def records(self) -> int:
        return self.created + self.updated + self.unchanged

    def describe(self) -> str:
        return (
            f"读取 {self.rows_read} 行：新增 {self.created} 条、更新 {self.updated} 条、"
            f"未变 {self.unchanged} 条、跳过 {self.skipped} 行；"
            f"部件 {self.parts} 个、新通道 {self.channels} 个"
        )


def _to_row(model: ComponentFreq) -> ComponentFreqRow:
    return ComponentFreqRow(
        id=model.id,
        task_id=model.task_id,
        object_id=model.object_id,
        part_code=model.part_code,
        part_name=model.part_name,
        part_type=model.part_type,
        channel_code=model.channel_code,
        component_kind=model.component_kind,
        ref_rpm=model.ref_rpm,
        hz_at_1rpm=model.hz_at_1rpm,
        scaling=model.scaling,
        freq_kind=model.freq_kind,
        value_hz=model.value_hz,
        geom_d=model.geom_d,
        note=model.note,
        updated_at=model.updated_at,
    )


# ---------------------------------------------------------------- 查询


def list_freqs(
    session: Session,
    task_id: int,
    *,
    object_id: int | None = None,
    part_code: str | None = None,
    freq_kind: str | None = None,
    channel_code: str | None = None,
) -> list[ComponentFreqRow]:
    """按任务 / 对象 / 部件 / 频率项 / 通道过滤台账行（按录入顺序）。"""
    stmt = select(ComponentFreq).where(
        ComponentFreq.task_id == task_id, ComponentFreq.deleted_at.is_(None)
    )
    if object_id is not None:
        stmt = stmt.where(ComponentFreq.object_id == object_id)
    if part_code is not None:
        stmt = stmt.where(ComponentFreq.part_code == part_code)
    if freq_kind is not None:
        stmt = stmt.where(ComponentFreq.freq_kind == freq_kind)
    rows = [_to_row(model) for model in session.execute(stmt.order_by(ComponentFreq.id)).scalars()]
    if channel_code is not None:
        rows = [row for row in rows if channel_code in row.channels]
    return rows


def list_channels_of(session: Session, object_id: int) -> list[ChannelReg]:
    """该监测对象台账下的通道行（含 ``remark`` 里的显示名）。"""
    stmt = (
        select(ChannelReg)
        .where(ChannelReg.object_id == object_id, ChannelReg.deleted_at.is_(None))
        .order_by(ChannelReg.id)
    )
    return list(session.execute(stmt).scalars())


def list_parts(
    session: Session, task_id: int, *, object_id: int | None = None
) -> list[PartFreqRow]:
    """按部件汇总频率参数（同一 ``part_code`` 的多条频率项合成一行）。

    顺序与台账录入顺序一致（首个 ``part_code`` 出现在前）；通道显示名取
    ``channel_reg.remark``。
    """
    rows = list_freqs(session, task_id, object_id=object_id)
    if not rows:
        return []
    displays: dict[str, str] = {}
    for model in session.execute(
        select(ChannelReg).where(ChannelReg.task_id == task_id, ChannelReg.deleted_at.is_(None))
    ).scalars():
        displays.setdefault(model.code, model.remark)

    groups: dict[tuple[int, str], list[ComponentFreqRow]] = {}
    for row in rows:
        groups.setdefault((row.object_id, row.part_code), []).append(row)

    parts: list[PartFreqRow] = []
    for part_rows in groups.values():
        first = part_rows[0]
        values = {row.freq_kind: row for row in part_rows}
        points = {
            kind: FreqPoint(
                id=model.id,
                freq_kind=kind,
                value_hz=model.value_hz,
                hz_at_1rpm=model.hz_at_1rpm,
                scaling=model.scaling,
            )
            for kind in FREQ_KINDS
            for model in (values.get(kind),)
            if model is not None
        }
        channels = first.channels
        parts.append(
            PartFreqRow(
                part_code=first.part_code,
                part_name=first.part_name,
                part_type=first.part_type,
                component_kind=first.component_kind,
                channels=channels,
                channel_names=tuple(displays.get(code, "") for code in channels),
                ref_rpm=first.ref_rpm,
                geom_d=first.geom_d,
                points=points,
            )
        )
    return parts


def _part_models(session: Session, object_id: int, part_code: str) -> list[ComponentFreq]:
    stmt = select(ComponentFreq).where(
        ComponentFreq.object_id == object_id,
        ComponentFreq.part_code == part_code,
        ComponentFreq.deleted_at.is_(None),
    )
    return list(session.execute(stmt.order_by(ComponentFreq.id)).scalars())


def save_part(
    session: Session,
    object_id: int,
    part_code: str,
    *,
    ref_rpm: float | None = None,
    values: dict[str, float | None] | None = None,
    scalings: dict[str, str] | None = None,
) -> int:
    """保存一个部件的频率参数（界面编辑入口）；返回真正改动的频率项条数。

    - ``ref_rpm``：改参考转速 → 该部件所有频率项 ``hz_at_1rpm`` 一起重算
    - ``values``：频率项 → Hz；``None`` 表示该项不动（要清理请走导入/导出）
    - ``scalings``：频率项 → ``speed`` / ``fixed``（固定值不随转速缩放）
    - 部件下没有的频率项给了值 → 新建一条（沿用该部件的名称/类型/通道）
    """
    obj = _active_object(session, object_id)
    rows = _part_models(session, object_id, part_code)
    if not rows:
        raise ComponentFreqError(f"部件「{part_code}」没有频率记录，请先导入或录入")

    values = dict(values or {})
    scalings = dict(scalings or {})
    for kind in (*values, *scalings):
        if kind not in FREQ_KINDS:
            raise ComponentFreqError(f"未知频率项：{kind}（可选 {'/'.join(FREQ_KINDS)}）")
    for kind, scaling in scalings.items():
        if scaling not in SCALING_LABELS:
            raise ComponentFreqError(f"缩放方式只能是 speed / fixed，收到：{scaling}（{kind}）")

    if ref_rpm is not None:
        rpm_value = float(ref_rpm)
        if not math.isfinite(rpm_value) or rpm_value <= 0:
            raise ComponentFreqError("参考转速必须是大于 0 的数字")
        for model in rows:
            model.ref_rpm = rpm_value
    ref_value = rows[0].ref_rpm

    before = {model.freq_kind: (model.value_hz, model.scaling, model.hz_at_1rpm) for model in rows}
    by_kind = {model.freq_kind: model for model in rows}
    first = rows[0]
    created = 0
    now = datetime.now()

    for kind, value in values.items():
        if value is None:
            continue
        hz = float(value)
        if not math.isfinite(hz) or hz < 0:
            raise ComponentFreqError(f"频率值必须是不小于 0 的数字（{kind}：{value!r}）")
        scaling = scalings.get(kind, by_kind[kind].scaling if kind in by_kind else "speed")
        model = by_kind.get(kind)
        if model is None:
            model = ComponentFreq(
                task_id=obj.task_id,
                object_id=obj.id,
                part_code=part_code,
                part_name=first.part_name,
                part_type=first.part_type,
                channel_code=first.channel_code,
                component_kind=first.component_kind,
                ref_rpm=ref_value,
                hz_at_1rpm=0.0,
                scaling=scaling,
                freq_kind=kind,
                value_hz=hz,
                geom_d=first.geom_d,
                note="",
                created_at=now,
                updated_at=now,
            )
            session.add(model)
            by_kind[kind] = model
            created += 1
        else:
            model.value_hz = hz
            model.scaling = scaling
            model.updated_at = now

    for kind, scaling in scalings.items():
        model = by_kind.get(kind)
        if model is not None and model.scaling != scaling:
            model.scaling = scaling
            model.updated_at = now

    for model in by_kind.values():
        rpm = model.ref_rpm
        model.hz_at_1rpm = model.value_hz / rpm if rpm and rpm > 0 else 0.0

    changed = created
    for kind, snapshot in before.items():
        model = by_kind.get(kind)
        if model is None:
            continue
        if (model.value_hz, model.scaling, model.hz_at_1rpm) != snapshot:
            changed += 1
    session.commit()
    return changed


# ---------------------------------------------------------------- 导入预览


def preview_import(session: Session, object_id: int, csv_path: str | Path) -> ImportSummary:
    """导入预览：跑完整导入逻辑后回滚（不落库），计数与真导入一致。

    ``begin_immediate`` 要在 ``begin_nested`` 之前：预览同样先读后写，没取写锁的话
    并发时会撞 ``SQLITE_BUSY_SNAPSHOT``（复查 v2 严重项 2）。
    """
    begin_immediate(session)
    savepoint = session.begin_nested()
    try:
        summary = import_csv(session, object_id, csv_path, commit=False)
    finally:
        savepoint.rollback()
    return summary


# ---------------------------------------------------------------- CSV 读取


def _read_text(path: str | Path) -> str:
    """自动识别编码：先 UTF-8（含 BOM），再 GBK（Excel 直出）。"""
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ComponentFreqError(f"无法识别 CSV 编码（已试 UTF-8 / GBK）：{path}")


def _read_rows(path: str | Path) -> list[tuple[int, dict[str, str]]]:
    """读 CSV → [(行号, 行字典)]；表头缺列直接报错（不猜列）。"""
    reader = csv.reader(io.StringIO(_read_text(path)))
    try:
        header = [cell.strip() for cell in next(reader)]
    except StopIteration as exc:  # pragma: no cover - 空文件
        raise ComponentFreqError(f"CSV 是空文件：{path}") from exc
    missing = [name for name in _REQUIRED_COLUMNS if name not in header]
    if missing:
        raise ComponentFreqError(f"CSV 缺少列：{'、'.join(missing)}")
    positions = {name: header.index(name) for name in _REQUIRED_COLUMNS}
    rows: list[tuple[int, dict[str, str]]] = []
    for line_no, cells in enumerate(reader, start=2):
        if not any(cell.strip() for cell in cells):
            continue
        row = {
            name: (cells[index].strip() if index < len(cells) else "")
            for name, index in positions.items()
        }
        rows.append((line_no, row))
    return rows


def _number(text: str) -> float | None:
    """空 → None；非数字抛 ``ValueError``（由调用方按行报错）。"""
    if not text:
        return None
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(text)
    return value


def _decimals(text: str) -> int:
    return len(text.split(".", 1)[1]) if "." in text else 0


def _mode_value(values: list[str]) -> str:
    """出现次数最多者；并列取小数位最多者，再并列取先出现者（附件 OM1 口径）。"""
    if not values:
        return ""
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for index, value in enumerate(values):
        counts[value] = counts.get(value, 0) + 1
        first_seen.setdefault(value, index)
    return max(
        counts,
        key=lambda value: (counts[value], _decimals(value), -first_seen[value]),
    )


def _component_kind(part_type: str) -> str:
    return PART_KIND_OF.get(part_type, "other")


def _active_object(session: Session, object_id: int) -> MonitorObject:
    model = session.get(MonitorObject, object_id)
    if model is None or model.deleted_at is not None:
        raise ComponentFreqError(f"监测对象不存在：{object_id}")
    return model


def _sync_channel(session: Session, task_id: int, object_id: int, code: str, display: str) -> bool:
    """通道台账里补一条（显示名放 ``remark``）；返回是否新建。"""
    existing = (
        session.execute(
            select(ChannelReg).where(
                ChannelReg.task_id == task_id,
                ChannelReg.code == code,
                ChannelReg.deleted_at.is_(None),
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        if display and not existing.remark:
            existing.remark = display
        return False
    session.add(
        ChannelReg(
            task_id=task_id,
            object_id=object_id,
            code=code,
            remark=display,
            created_at=datetime.now(),
        )
    )
    return True


# ---------------------------------------------------------------- 导入


def _upsert(
    session: Session,
    *,
    task_id: int,
    object_id: int,
    part_code: str,
    part_name: str,
    part_type: str,
    channel_code: str,
    ref_rpm: float,
    freq_kind: str,
    value_hz: float,
    geom_d: float | None,
) -> str:
    """按 ``(object, part, kind)`` 幂等 upsert；返回 created / updated / unchanged。"""
    hz_at_1rpm = value_hz / ref_rpm
    model = (
        session.execute(
            select(ComponentFreq).where(
                ComponentFreq.object_id == object_id,
                ComponentFreq.part_code == part_code,
                ComponentFreq.freq_kind == freq_kind,
                ComponentFreq.deleted_at.is_(None),
            )
        )
        .scalars()
        .first()
    )
    kind = _component_kind(part_type)
    if model is None:
        session.add(
            ComponentFreq(
                task_id=task_id,
                object_id=object_id,
                part_code=part_code,
                part_name=part_name,
                part_type=part_type,
                channel_code=channel_code,
                component_kind=kind,
                ref_rpm=ref_rpm,
                hz_at_1rpm=hz_at_1rpm,
                scaling="speed",
                freq_kind=freq_kind,
                value_hz=value_hz,
                geom_d=geom_d,
                note="",
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
        )
        return "created"

    changed = (
        model.part_name != part_name
        or model.part_type != part_type
        or model.channel_code != channel_code
        or model.component_kind != kind
        or model.ref_rpm != ref_rpm
        or model.hz_at_1rpm != hz_at_1rpm
        or model.scaling != "speed"
        or model.value_hz != value_hz
        or model.geom_d != geom_d
    )
    if not changed:
        return "unchanged"

    model.part_name = part_name
    model.part_type = part_type
    model.channel_code = channel_code
    model.component_kind = kind
    model.ref_rpm = ref_rpm
    model.hz_at_1rpm = hz_at_1rpm
    model.scaling = "speed"
    model.value_hz = value_hz
    model.geom_d = geom_d
    model.updated_at = datetime.now()
    return "updated"


def _group_by_part(
    rows: list[tuple[int, dict[str, str]]],
    summary: ImportSummary,
) -> dict[str, list[tuple[int, dict[str, str]]]]:
    """按部件编号分组，同时做行级校验（坏行跳过、不中断整批）。"""
    groups: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for line_no, row in rows:
        part_code = row["监测部件编号"]
        if not part_code:
            summary.skipped += 1
            _add_detail(summary, f"第 {line_no} 行：监测部件编号为空")
            continue
        problems: list[str] = []
        for column in (*FREQ_KIND_COLUMNS.values(), "D"):
            try:
                _number(row[column])
            except ValueError:
                problems.append(f"列「{column}」不是数字：{row[column]!r}")
        if not row["序号"]:
            problems.append("通道号（序号）为空")
        if problems:
            summary.skipped += 1
            _add_detail(summary, f"第 {line_no} 行：" + "；".join(problems))
            continue
        groups.setdefault(part_code, []).append((line_no, row))
    return groups


def _add_detail(summary: ImportSummary, text: str) -> None:
    if len(summary.skipped_details) < _SUMMARY_MAX_DETAILS:
        summary.skipped_details.append(text)


def import_csv(
    session: Session, object_id: int, csv_path: str | Path, *, commit: bool = True
) -> ImportSummary:
    """导入附件格式 CSV（UTF-8 / GBK 都吃），按对象幂等 upsert。

    ``commit=False`` 供导入预览用：调用方自行回滚（``preview_import``）。
    开头 ``begin_immediate`` 先取写锁：导入是「先读后写」（查对象 / 查已有行），
    没取锁时并发第二个导入会撞 ``SQLITE_BUSY_SNAPSHOT`` 直接报 database is locked
    （复查 v2 严重项 2）。
    """
    begin_immediate(session)
    obj = _active_object(session, object_id)
    rows = _read_rows(csv_path)
    summary = ImportSummary(rows_read=len(rows))
    groups = _group_by_part(rows, summary)

    for part_code, part_rows in groups.items():
        first = part_rows[0][1]
        part_name = first["监测部件名称"]
        part_type = first["部件类型"]
        om1_values = [row["OM1"] for _, row in part_rows if row["OM1"]]
        if not om1_values:
            summary.skipped += len(part_rows)
            _add_detail(
                summary,
                f"部件 {part_code}：没有 OM1，无法反推参考转速（跳过 {len(part_rows)} 行）",
            )
            continue
        ref_rpm = float(_mode_value(om1_values)) * 60.0

        channels: list[str] = []
        displays: dict[str, str] = {}
        for _, row in part_rows:
            code = row["序号"]
            if code not in channels:
                channels.append(code)
            if row["振动通道名称"] and not displays.get(code):
                displays[code] = row["振动通道名称"]
        channel_code = ",".join(channels)
        for code in channels:
            if _sync_channel(session, obj.task_id, obj.id, code, displays.get(code, "")):
                summary.channels += 1

        geom_d_text = _mode_value([row["D"] for _, row in part_rows if row["D"]])
        geom_d = float(geom_d_text) if geom_d_text else None

        for freq_kind, column in FREQ_KIND_COLUMNS.items():
            chosen = _mode_value([row[column] for _, row in part_rows if row[column]])
            if not chosen:
                continue
            outcome = _upsert(
                session,
                task_id=obj.task_id,
                object_id=obj.id,
                part_code=part_code,
                part_name=part_name,
                part_type=part_type,
                channel_code=channel_code,
                ref_rpm=ref_rpm,
                freq_kind=freq_kind,
                value_hz=float(chosen),
                geom_d=geom_d,
            )
            setattr(summary, outcome, getattr(summary, outcome) + 1)
        summary.parts += 1

    if commit:
        session.commit()
    return summary


# ---------------------------------------------------------------- 导出


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.10g}"


def export_csv_text(
    session: Session,
    task_id: int,
    *,
    object_id: int | None = None,
    channel_code: str | None = None,
) -> str:
    """导出与附件同格式同列序的 CSV 文本（每个部件按挂的通道各出一行）。"""
    rows = list_freqs(session, task_id, object_id=object_id)
    if not rows:
        return _write_csv([])

    displays: dict[str, str] = {}
    for model in session.execute(
        select(ChannelReg).where(ChannelReg.task_id == task_id, ChannelReg.deleted_at.is_(None))
    ).scalars():
        displays.setdefault(model.code, model.remark)

    grouped: dict[tuple[int, str], list[ComponentFreqRow]] = {}
    for row in rows:
        grouped.setdefault((row.object_id, row.part_code), []).append(row)

    out_rows: list[dict[str, str]] = []
    for (_object, _part), part_rows in grouped.items():
        first = part_rows[0]
        values = {row.freq_kind: row.value_hz for row in part_rows}
        for code in first.channels:
            if channel_code is not None and code != channel_code:
                continue
            record = {name: "" for name in CSV_COLUMNS}
            record["序号"] = code
            record["振动通道名称"] = displays.get(code, "")
            record["部件类型"] = first.part_type
            record["监测部件编号"] = first.part_code
            record["监测部件名称"] = first.part_name
            for freq_kind, column in FREQ_KIND_COLUMNS.items():
                record[column] = _fmt(values.get(freq_kind))
            record["D"] = _fmt(first.geom_d)
            out_rows.append(record)
    return _write_csv(out_rows)


def _write_csv(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(CSV_COLUMNS), lineterminator="\r\n", extrasaction="ignore"
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def export_csv_bytes(
    session: Session,
    task_id: int,
    *,
    object_id: int | None = None,
    channel_code: str | None = None,
) -> bytes:
    """GBK 字节（Excel 双击可读）。"""
    text = export_csv_text(session, task_id, object_id=object_id, channel_code=channel_code)
    return text.encode("gbk")


def write_csv(
    session: Session,
    path: str | Path,
    *,
    task_id: int,
    object_id: int | None = None,
    channel_code: str | None = None,
) -> Path:
    """把导出内容写到文件（GBK）。"""
    target = Path(path)
    target.write_bytes(
        export_csv_bytes(session, task_id, object_id=object_id, channel_code=channel_code)
    )
    return target


# ---------------------------------------------------------------- 转速 / 频率解析


def object_id_of_node(session: Session, node_id: int) -> int | None:
    """节点所属监测对象台账 id：沿树往上找 component 节点，优先用引用、否则按名称匹配。"""
    node = tree.get_node(session, node_id)
    for row in reversed(tree.path_nodes(session, node_id)):
        if row.node_type != "component":
            continue
        if row.ref_type == "monitor_object" and row.ref_id:
            return row.ref_id
        for obj in object_registry.list_objects(session, node.task_id):
            if obj.name == row.name:
                return obj.id
        return None
    return None


def resolve_node_rpm(
    session: Session, node_id: int, dataset_id: int | None = None
) -> tuple[float | None, str | None]:
    """转速三级：节点手填 > 批次实测 > 对象额定；都没有返回 (None, None)。

    批次实测按 ``dataset.measured_rpm`` 取（同一批 4 个通道共用一个转速），
    没传批次就是没有实测这一级。
    """
    node = tree.get_node(session, node_id)
    nodes = tree.list_nodes(session, node.task_id)
    node_rpm = tree.node_rpm(nodes, node_id)

    measured = None
    if dataset_id is not None:
        dataset = session.get(Dataset, dataset_id)
        if dataset is not None and dataset.deleted_at is None:
            measured = dataset.measured_rpm

    rated = None
    object_id = object_id_of_node(session, node_id)
    if object_id is not None:
        model = session.get(MonitorObject, object_id)
        if model is not None and model.deleted_at is None:
            rated = model.rated_rpm

    return resolve_rpm(node_rpm=node_rpm, measured_rpm=measured, rated_rpm=rated)


# ---------------------------------------------------------------- 节点手填覆盖

#: 节点属性里的键前缀：``特征频率:<部件编号>:<频率项>`` → Hz
FREQ_ATTR_PREFIX = "特征频率:"
#: 覆盖沿树往上继承几级（通道 → 监测对象 → 设备）
_OVERRIDE_DEPTH = 3


def freq_override_key(part_code: str, freq_kind: str) -> str:
    """节点属性键：``特征频率:Z1_R:om1``。"""
    return f"{FREQ_ATTR_PREFIX}{part_code}:{freq_kind}"


def parse_freq_overrides(attrs: dict | None) -> dict[tuple[str, str], float]:
    """从节点属性里挑出频率覆盖项（非法键/值忽略，不猜）。"""
    found: dict[tuple[str, str], float] = {}
    for key, raw in (attrs or {}).items():
        text = str(key)
        if not text.startswith(FREQ_ATTR_PREFIX):
            continue
        rest = text[len(FREQ_ATTR_PREFIX) :]
        if rest.count(":") != 1:
            continue
        part_code, freq_kind = (piece.strip() for piece in rest.split(":"))
        if not part_code or freq_kind not in FREQ_KINDS:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            found[(part_code, freq_kind)] = value
    return found


def node_freq_overrides(session: Session, node_id: int) -> dict[tuple[str, str], float]:
    """本节点（及往上 3 级祖先）上的频率覆盖项；近的优先。"""
    node = tree.get_node(session, node_id)
    nodes = {row.id: row for row in tree.list_nodes(session, node.task_id)}
    current = nodes.get(node_id)
    found: dict[tuple[str, str], float] = {}
    for _ in range(_OVERRIDE_DEPTH):
        if current is None:
            break
        for key, value in parse_freq_overrides(current.attrs).items():
            found.setdefault(key, value)
        current = nodes.get(current.parent_id) if current.parent_id else None
    return found


def set_node_freq_override(
    session: Session,
    node_id: int,
    part_code: str,
    freq_kind: str,
    value: float | None,
) -> None:
    """写/清节点手填频率覆盖（``value=None`` 清除该项）。"""
    if freq_kind not in FREQ_KINDS:
        raise ComponentFreqError(f"未知频率项：{freq_kind}（可选 {'/'.join(FREQ_KINDS)}）")
    if not part_code.strip():
        raise ComponentFreqError("部件编号不能为空")
    node = tree.get_node(session, node_id)
    attrs = dict(node.attrs)
    key = freq_override_key(part_code.strip(), freq_kind)
    if value is None:
        attrs.pop(key, None)
    else:
        hz = float(value)
        if not math.isfinite(hz) or hz <= 0:
            raise ComponentFreqError("覆盖频率必须是大于 0 的数字（清除请用「清除覆盖」）")
        attrs[key] = f"{hz:g}"
    tree.update_node(session, node_id, attrs=attrs)


def _pick_override(
    overrides: dict[tuple[str, str], float],
    freq_kind: str,
    part_code: str | None,
) -> tuple[str, float] | None:
    """按部件取覆盖值；不给部件时只有唯一覆盖才认（多个部件视为歧义，不猜）。"""
    if part_code is not None:
        value = overrides.get((part_code, freq_kind))
        return (part_code, value) if value is not None else None
    matches = [(part, hz) for (part, kind), hz in overrides.items() if kind == freq_kind]
    return matches[0] if len(matches) == 1 else None


def resolve_component_freq(
    session: Session,
    node_id: int,
    freq_kind: str,
    rpm_actual: float | None = None,
    *,
    part_code: str | None = None,
    dataset_id: int | None = None,
) -> ResolvedFreq | None:
    """节点所属对象的部件频率 → 实际频率值（Hz）。

    优先级：节点手填覆盖 > 对象台账派生（``speed`` 按转速缩放、``fixed`` 直接用）。
    取不到转速或该部件没配这一频率项时返回 ``None``——**不猜**，由调用方灰显提示。
    ``part_code`` 省略时只有唯一匹配才返回，多个部件视为歧义返回 ``None``
    （此时若有唯一的节点手填覆盖，按覆盖返回）。
    """
    if freq_kind not in FREQ_KINDS:
        raise ComponentFreqError(f"未知频率项：{freq_kind}（可选 {'/'.join(FREQ_KINDS)}）")
    object_id = object_id_of_node(session, node_id)
    if object_id is None:
        return None
    rows = list_freqs(
        session,
        tree.get_node(session, node_id).task_id,
        object_id=object_id,
        part_code=part_code,
        freq_kind=freq_kind,
    )
    overrides = node_freq_overrides(session, node_id)
    if part_code is not None and not rows:
        # 台账里没有这一项，但节点手填了 → 手填生效
        picked = _pick_override(overrides, freq_kind, part_code)
        return _override_result(picked, freq_kind) if picked else None
    if part_code is None:
        if len(rows) != 1:
            picked = _pick_override(overrides, freq_kind, None)
            return _override_result(picked, freq_kind) if picked else None
    row = rows[0]
    picked = _pick_override(overrides, freq_kind, row.part_code)
    if picked is None and part_code is not None:
        picked = _pick_override(overrides, freq_kind, part_code)
    if picked is not None:
        return _override_result(picked, freq_kind)
    if row.value_hz <= 0:
        # 附件里的 0 表示该特征频率不适用（如双列角接触球轴承没有 BPFI）：
        # 台账按原值留痕，但解析不出 0 Hz 标记线
        return None
    if row.scaling == "fixed":
        return ResolvedFreq(
            value_hz=row.value_hz,
            freq_kind=freq_kind,
            part_code=row.part_code,
            scaling="fixed",
            rpm_used=None,
            rpm_source="fixed",
        )

    rpm, source = (None, None)
    if rpm_actual is not None:
        value = float(rpm_actual)
        if math.isfinite(value) and value > 0:
            rpm, source = value, "given"
    if rpm is None:
        rpm, source = resolve_node_rpm(session, node_id, dataset_id)
    if rpm is None:
        return None
    return ResolvedFreq(
        value_hz=row.hz_at_1rpm * rpm,
        freq_kind=freq_kind,
        part_code=row.part_code,
        scaling="speed",
        rpm_used=rpm,
        rpm_source=source or "",
    )


def _override_result(picked: tuple[str, float], freq_kind: str) -> ResolvedFreq:
    """节点手填覆盖 → 解析结果（显式 Hz，按固定值处理、不随转速缩放）。"""
    part_code, value = picked
    return ResolvedFreq(
        value_hz=value,
        freq_kind=freq_kind,
        part_code=part_code,
        scaling="fixed",
        rpm_used=None,
        rpm_source="node",
        source="node",
    )


def channel_node_id(session: Session, task_id: int, channel_code: str) -> int | None:
    """按通道号找健康树里的通道节点（同号取第一个）。"""
    for row in tree.list_nodes(session, task_id):
        if row.node_type == "channel" and row.name == channel_code:
            return row.id
    return None


def resolve_freq_marks(
    session: Session,
    task_id: int,
    channel_code: str,
    dataset_id: int | None = None,
) -> list[ResolvedFreq]:
    """该通道上「台账已配置 + 能算出值」的特征频率（供分析页画标记线）。

    只取挂在该通道上的部件（``channel_code`` 多值命中）；节点手填覆盖优先；
    未配置 / 缺转速 / 0 值一律不返回，由界面提示「未配置特征频率」。
    """
    node_id = channel_node_id(session, task_id, channel_code)
    if node_id is None:
        return []
    object_id = object_id_of_node(session, node_id)
    if object_id is None:
        return []
    overrides = node_freq_overrides(session, node_id)
    rpm, rpm_source = resolve_node_rpm(session, node_id, dataset_id)
    marks: list[ResolvedFreq] = []
    for row in list_freqs(session, task_id, object_id=object_id):
        if channel_code not in row.channels:
            continue
        picked = _pick_override(overrides, row.freq_kind, row.part_code)
        if picked is not None:
            marks.append(_override_result(picked, row.freq_kind))
            continue
        if row.value_hz <= 0:
            continue
        if row.scaling == "fixed":
            marks.append(
                ResolvedFreq(
                    value_hz=row.value_hz,
                    freq_kind=row.freq_kind,
                    part_code=row.part_code,
                    scaling="fixed",
                    rpm_used=None,
                    rpm_source="fixed",
                    source="object",
                )
            )
            continue
        if rpm is None:
            continue
        marks.append(
            ResolvedFreq(
                value_hz=row.hz_at_1rpm * rpm,
                freq_kind=row.freq_kind,
                part_code=row.part_code,
                scaling="speed",
                rpm_used=rpm,
                rpm_source=rpm_source or "",
                source="object",
            )
        )
    return marks
