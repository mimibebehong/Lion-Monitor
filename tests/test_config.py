"""配置模型与阈值判定的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lion_monitor.config import (
    DEFAULT_SCAN_DIR,
    ChannelConfig,
    LineConfig,
    load_line_config,
    resolve_scan_dir,
)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "example_line.yaml"


def test_load_example_line_config() -> None:
    line = load_line_config(CONFIG_PATH)
    assert isinstance(line, LineConfig)
    assert line.id == "LINE-A"
    assert len(line.stations) == 3


def test_station_lookup_and_channels() -> None:
    line = load_line_config(CONFIG_PATH)
    coating = line.station("ST-COATING")
    assert coating.name == "涂布机"
    assert coating.sample_rate_hz == 10
    ch = coating.channel("areal_density")
    assert ch.unit == "mg/cm2"
    with pytest.raises(KeyError):
        coating.channel("not_exist")


def test_classify_levels() -> None:
    ch = ChannelConfig(name="v", warn_low=10, warn_high=20, alert_low=8, alert_high=22)
    assert ch.classify(15) == "ok"
    assert ch.classify(10) == "ok"
    assert ch.classify(9.5) == "warn"
    assert ch.classify(7.9) == "alert"
    assert ch.classify(20.5) == "warn"
    assert ch.classify(22.1) == "alert"


def test_invalid_threshold_order_rejected() -> None:
    with pytest.raises(ValueError):
        ChannelConfig(name="bad", warn_low=10, alert_low=12)
    with pytest.raises(ValueError):
        ChannelConfig(name="bad", warn_high=20, alert_high=18)


def test_missing_config_file() -> None:
    with pytest.raises(FileNotFoundError):
        load_line_config(CONFIG_PATH.parent / "nope.yaml")


def test_scan_dir_default_and_override(tmp_path: Path) -> None:
    """数据接入扫描目录：缺省配置返回内置默认值，给了配置值则用配置值。"""
    assert DEFAULT_SCAN_DIR.endswith("四通道保存")
    assert resolve_scan_dir() == DEFAULT_SCAN_DIR
    assert resolve_scan_dir("") == DEFAULT_SCAN_DIR
    assert resolve_scan_dir("  ") == DEFAULT_SCAN_DIR
    assert resolve_scan_dir(str(tmp_path)) == str(tmp_path)
