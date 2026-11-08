"""SQLAlchemy 表结构（SQLite，单人单机）。

约定：
- 全表软删除用 ``deleted_at``（NULL = 有效）
- 时间统一用本地时间 datetime.now
- 指标值一行一个计算点，不聚合（决策 7）
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有表的基类。"""


class TestTask(Base):
    """测试任务。"""

    __tablename__ = "test_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="draft")
    operator: Mapped[str] = mapped_column(String(64), default="")
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TaskNode(Base):
    """健康树节点：任务 → 设备 → 监测对象 → 通道 → 指标。

    阈值不落节点表（阈值独立按 节点 × 指标 存，见 threshold）。
    ``path`` 用祖先 id 串（如 ``1/2/5``），便于按子树前缀查询。
    """

    __tablename__ = "task_node"
    __table_args__ = (UniqueConstraint("task_id", "path", name="uq_task_node_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("test_task.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_node.id"), nullable=True, index=True
    )
    node_type: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(128))
    attrs_json: Mapped[str] = mapped_column(Text, default="{}")
    ref_type: Mapped[str] = mapped_column(String(16), default="")
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    path: Mapped[str] = mapped_column(String(255), default="")
    ref_type: Mapped[str] = mapped_column(String(32), default="")
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Dataset(Base):
    """数据集 = 一次采集批次（一个文件夹 = 4 个通道文件）。"""

    __tablename__ = "dataset"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("test_task.id"), index=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    source_dir: Mapped[str] = mapped_column(Text, default="")
    channel_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    validity: Mapped[str] = mapped_column(String(16), default="pass")
    validity_note: Mapped[str] = mapped_column(Text, default="")
    #: 批次实测转速（工频 / 编码器），转速三级里的 ``measured`` 一级（CF-1）
    measured_rpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Channel(Base):
    """通道 = 一个源文件。"""

    __tablename__ = "channel"
    __table_args__ = (UniqueConstraint("dataset_id", "code", name="uq_channel_dataset_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    source_path: Mapped[str] = mapped_column(Text, default="")
    source_sha256: Mapped[str] = mapped_column(String(64), default="")
    sample_rate_hz: Mapped[float] = mapped_column(Float, default=0.0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    validity: Mapped[str] = mapped_column(String(16), default="pass")
    validity_note: Mapped[str] = mapped_column(Text, default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MetricValue(Base):
    """指标值：每次计算一行。

    方案 A（2026-10-31 拍板）**值按节点存**：``node_id`` 是健康树的通道节点 id，
    同一通道号挂在多个监测对象下时各算各的、各存一行；历史行 ``node_id`` 为空。

    参数快照（方案文档 2.3）：``params_hash`` 是生效参数的 sha1 前 12 位（含指标码 +
    采样率 + 夹紧后的生效带通），``effective_params_json`` 是夹紧后实际生效参数的 JSON
    （历史行为空），``rpm_used``/``rpm_source`` 记录本次计算所用转速及来源
    （node/measured/rated），``stale``=1 表示参数或转速基准已变、该历史值待重算。
    """

    __tablename__ = "metric_value"
    __table_args__ = (
        UniqueConstraint(
            "channel_id", "code", "computed_at", "node_id", name="uq_metric_value_node_point"
        ),
        # P3 / S3 复合索引：按 store.metrics 的实际取数模式建
        #   计数/最新值/越限行：WHERE node_id IN (...) AND code IN (...)，组内按 computed_at 取最新
        #   逐点取数（list_values / 趋势页）：channel_id + code + computed_at >= 窗口起点
        # 单列索引（channel_id / code / node_id / computed_at）已被这两个索引覆盖或没有使用
        # 场景，留着反而会让 SQLite 的计划器挑它们（code 只有 3 个值 → 每条都扫 1/3 张表）。
        Index("ix_metric_value_node_code_time", "node_id", "code", "computed_at"),
        Index("ix_metric_value_channel_code_time", "channel_id", "code", "computed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channel.id"))
    node_id: Mapped[int | None] = mapped_column(ForeignKey("task_node.id"), nullable=True)
    code: Mapped[str] = mapped_column(String(32))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16), default="")
    level: Mapped[str] = mapped_column(String(16), default="unset")
    computed_at: Mapped[datetime] = mapped_column(DateTime)
    params_hash: Mapped[str] = mapped_column(String(40), default="", server_default=text("''"))
    effective_params_json: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    rpm_used: Mapped[float | None] = mapped_column(Float, nullable=True)
    rpm_source: Mapped[str] = mapped_column(String(16), default="", server_default=text("''"))
    stale: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Threshold(Base):
    """**历史遗留表**：不再写入、无人读取（G5）。

    生效阈值已全量迁到 :class:`MetricLimit`（节点 × 指标，沿树继承）。
    本表只保留历史行，不要往里写，也不要拿它判级。
    """

    __tablename__ = "threshold"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "scope_type", "scope_code", "metric_code", name="uq_threshold_scope"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("test_task.id"), index=True)
    scope_type: Mapped[str] = mapped_column(String(16), default="task")
    scope_code: Mapped[str] = mapped_column(String(32), default="")
    metric_code: Mapped[str] = mapped_column(String(32))
    warn: Mapped[float] = mapped_column(Float)
    alert: Mapped[float] = mapped_column(Float)
    direction: Mapped[str] = mapped_column(String(8), default=">")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DeletionLog(Base):
    """删除留痕（软删除 / 彻底删除 / 恢复）。

    G6：节点级回收站要在留痕里存**子树快照**（``payload`` JSON：节点属性 + 子节点 +
    该节点上的指标授权与阈值），恢复时按快照重建；``expires_at`` 是保留期到期时间
    （默认 7 天，见 ``store.recycle.RECYCLE_RETENTION_DAYS``），启动时据此清理。
    """

    __tablename__ = "deletion_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int] = mapped_column(Integer)
    entity_name: Mapped[str] = mapped_column(String(255), default="")
    mode: Mapped[str] = mapped_column(String(16), default="soft")
    cascade_counts: Mapped[str] = mapped_column(Text, default="{}")
    operator: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[str] = mapped_column(Text, default="{}", server_default=text("'{}'"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ChangeLog(Base):
    """参数 / 阈值变更留痕（T3）。

    一条 = 一个字段的一次真实变化：``old_value`` / ``new_value`` 都按 JSON 存
    （数字/布尔/字符串/字典都能原样读回）；参数（dict）变化合成一条，old / new 只含
    改动键。写入集中在 :mod:`lion_monitor.store.audit`，页面与 store 层不各写一份。
    """

    __tablename__ = "change_log"
    __table_args__ = (
        # 页面按「实体类型 + 倒序」取最近 200 条，按这个组合索引扫
        Index("ix_change_log_entity", "entity_type", "entity_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int] = mapped_column(Integer)
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    note: Mapped[str] = mapped_column(Text, default="")


class Sensor(Base):
    """传感器台账：一个通道一台传感器。"""

    __tablename__ = "sensor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("test_task.id"), index=True)
    node_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_node.id"), nullable=True, index=True
    )
    channel_code: Mapped[str] = mapped_column(String(32), default="", server_default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    sensitivity: Mapped[str] = mapped_column(String(64), default="")
    range_g: Mapped[str] = mapped_column(String(64), default="")
    mounting: Mapped[str] = mapped_column(String(64), default="")
    calibrated_on: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="启用")
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class MetricAssign(Base):
    """指标授予：通道 × 指标 + 计算参数。

    ``params_json`` 只存**覆盖项**（与生效默认不同的键）；``params_locked=1`` 时
    该 JSON 是全量固化快照，此后指标定义改默认值不影响本节点。
    """

    __tablename__ = "metric_assign"
    __table_args__ = (
        # 部分唯一索引（G3）：deleted_at 不能进唯一列——SQLite 里 NULL 不等于 NULL，
        # 同键未软删的行可以插两条，metric_jobs 字典会静默取最后一条。
        Index(
            "uq_metric_assign_active",
            "node_id",
            "metric_code",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("test_task.id"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("task_node.id"), index=True)
    metric_code: Mapped[str] = mapped_column(String(32))
    unit: Mapped[str] = mapped_column(String(16), default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    params_locked: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class MetricLimit(Base):
    """阈值：节点 × 指标，读取时沿树向上继承。"""

    __tablename__ = "metric_limit"
    __table_args__ = (
        # 部分唯一索引（G3）：理由同 MetricAssign
        Index(
            "uq_metric_limit_active",
            "node_id",
            "metric_code",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("test_task.id"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("task_node.id"), index=True)
    metric_code: Mapped[str] = mapped_column(String(32))
    warn: Mapped[float] = mapped_column(Float, default=0.0)
    alert: Mapped[float] = mapped_column(Float, default=0.0)
    direction: Mapped[str] = mapped_column(String(8), default=">")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class MonitorObject(Base):
    """监测对象台账：独立主数据，健康树只引用不复制。"""

    __tablename__ = "monitor_object"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("test_task.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    obj_type: Mapped[str] = mapped_column(String(32), default="")
    part: Mapped[str] = mapped_column(String(64), default="")
    #: 额定转速（设计文档 §2.5）：转速三级里的 ``rated`` 一级（CF-1）
    rated_rpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class ComponentFreq(Base):
    """部件频率台账：挂在监测对象上，归一化存储（设计文档 §2.4 / 任务书 CF-1）。

    归一化口径：``hz_at_1rpm = value_hz / ref_rpm``（等价于「阶次 ÷ 60」）。
    ``scaling=speed`` 运行期按实际转速缩放（``f = hz_at_1rpm × rpm_actual``），
    ``fixed`` 直接用 ``value_hz``（与转速无关）。

    一个部件可被多个通道监测：``channel_code`` 存逗号分隔的通道号（如
    ``"Chan_1,Chan_8"``），通道显示名落在 ``channel_reg.remark``，同部件跨通道
    仍然只有一条部件频率记录。唯一键 ``(object_id, part_code, freq_kind)``
    （``deleted_at IS NULL`` 部分唯一索引）。
    """

    __tablename__ = "component_freq"
    __table_args__ = (
        Index(
            "uq_component_freq_active",
            "object_id",
            "part_code",
            "freq_kind",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index("ix_component_freq_object_part", "object_id", "part_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("test_task.id"), index=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("monitor_object.id"), index=True)
    part_code: Mapped[str] = mapped_column(String(64))
    part_name: Mapped[str] = mapped_column(String(128), default="")
    #: 附件原始口径（齿轮 / 轴承 / 行星齿轮 / 轴）
    part_type: Mapped[str] = mapped_column(String(32), default="")
    #: 逗号分隔的通道号（一个部件可被多个通道监测）
    channel_code: Mapped[str] = mapped_column(String(255), default="")
    #: gear | bearing | planet | shaft | other
    component_kind: Mapped[str] = mapped_column(String(16), default="other")
    ref_rpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    hz_at_1rpm: Mapped[float] = mapped_column(Float, default=0.0)
    #: speed | fixed
    scaling: Mapped[str] = mapped_column(String(8), default="speed")
    #: om1 | om2 | omx | om2x | bpfi | bpfo | ftf | bsf
    freq_kind: Mapped[str] = mapped_column(String(16))
    #: 导入时的原始频率值（留痕；导出按它原样输出）
    value_hz: Mapped[float] = mapped_column(Float, default=0.0)
    #: 附件 D 列（几何尺寸）
    geom_d: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class ChannelReg(Base):
    """通道台账：``object_id`` 为空 = 自由通道（不归属任何监测对象）。"""

    __tablename__ = "channel_reg"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("test_task.id"), index=True)
    #: 归属是**绑定关系**（现场口径）：为空表示自由，不归属任何监测对象
    object_id: Mapped[int | None] = mapped_column(
        ForeignKey("monitor_object.id"), nullable=True, index=True
    )
    code: Mapped[str] = mapped_column(String(32))
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class MetricDef(Base):
    """指标台账：指标码 + 单位 + 计算参数。

    ``params_schema_json`` 声明表单字段（key/label/group/type/default/min/options/...），
    ``params_default_json`` 是本指标的定义默认值（节点未覆盖时生效）。
    """

    __tablename__ = "metric_def"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("test_task.id"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    unit: Mapped[str] = mapped_column(String(16), default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    params_schema_json: Mapped[str] = mapped_column(Text, default="[]")
    params_default_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
