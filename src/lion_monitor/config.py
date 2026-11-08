"""配置模型：产线、工位、采集通道与报警阈值。

设计原则：所有产线相关参数外部化到 YAML，代码只负责校验与加载。
路径类默认值（如数据接入扫描目录）集中在本模块，可被环境变量/页面上填入的值覆盖。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

__all__ = [
    "ChannelConfig",
    "StationConfig",
    "LineConfig",
    "DEFAULT_SCAN_DIR",
    "load_line_config",
    "resolve_scan_dir",
]

#: 数据接入页默认扫描目录（卷绕产线 envis 四通道导出目录，Windows 路径）；
#: 换机器/盘符时用 LION_SCAN_DIR 覆盖，或用页面上的「浏览目录」重新选（选过会记住）。
DEFAULT_SCAN_DIR = r"D:\hermes_wp\Project\锂电产线\envision\data\四通道保存"


def resolve_scan_dir(override: str | None = None) -> str:
    """定数据接入的默认扫描目录：显式覆盖优先（支持 ~），否则用内置默认值。"""
    text = (override or "").strip()
    if text:
        return str(Path(text).expanduser())
    return DEFAULT_SCAN_DIR


class ChannelConfig(BaseModel):
    """单个采集通道及其报警阈值。"""

    name: str
    unit: str = ""
    warn_low: float | None = None
    warn_high: float | None = None
    alert_low: float | None = None
    alert_high: float | None = None

    @model_validator(mode="after")
    def _check_bounds(self) -> ChannelConfig:
        if (
            self.alert_low is not None
            and self.warn_low is not None
            and self.alert_low > self.warn_low
        ):
            raise ValueError(f"{self.name}: alert_low 必须 <= warn_low")
        if (
            self.alert_high is not None
            and self.warn_high is not None
            and self.alert_high < self.warn_high
        ):
            raise ValueError(f"{self.name}: alert_high 必须 >= warn_high")
        return self

    def classify(self, value: float) -> str:
        """返回该数值的报警等级：ok / warn / alert。"""
        if self.alert_low is not None and value < self.alert_low:
            return "alert"
        if self.alert_high is not None and value > self.alert_high:
            return "alert"
        if self.warn_low is not None and value < self.warn_low:
            return "warn"
        if self.warn_high is not None and value > self.warn_high:
            return "warn"
        return "ok"


class StationConfig(BaseModel):
    """产线上的一个工位（如涂布、辊压、化成柜）。"""

    id: str
    name: str
    sample_rate_hz: float = Field(default=1.0, gt=0)
    channels: list[ChannelConfig] = Field(default_factory=list)

    def channel(self, name: str) -> ChannelConfig:
        for ch in self.channels:
            if ch.name == name:
                return ch
        raise KeyError(f"工位 {self.id} 不存在通道 {name}")


class LineConfig(BaseModel):
    """整条产线配置。"""

    id: str
    name: str
    stations: list[StationConfig] = Field(default_factory=list)

    def station(self, station_id: str) -> StationConfig:
        for st in self.stations:
            if st.id == station_id:
                return st
        raise KeyError(f"产线 {self.id} 不存在工位 {station_id}")


def load_line_config(path: str | Path) -> LineConfig:
    """从 YAML 文件加载产线配置。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return LineConfig.model_validate(raw)
