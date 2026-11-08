"""记住上次选择的数据目录（2026-11-04：自己选，但要记住）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lion_monitor.features import ui_settings  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "ui_settings.json"
    monkeypatch.setenv(ui_settings.SETTINGS_ENV, str(path))
    return path


def test_missing_file_means_no_memory(settings: Path):
    assert not settings.exists()
    assert ui_settings.get_scan_dir() is None


def test_remember_then_read_back(settings: Path):
    ui_settings.remember_scan_dir("D:\\envision\\data\\四通道保存")
    assert ui_settings.get_scan_dir() == "D:\\envision\\data\\四通道保存"
    assert settings.exists()


def test_remember_overwrites_previous(settings: Path):
    ui_settings.remember_scan_dir("/data/old")
    ui_settings.remember_scan_dir("/data/new")
    assert ui_settings.get_scan_dir() == "/data/new"


def test_remember_creates_parent_dir(tmp_path: Path, monkeypatch):
    deep = tmp_path / "a" / "b" / "ui_settings.json"
    monkeypatch.setenv(ui_settings.SETTINGS_ENV, str(deep))
    ui_settings.remember_scan_dir("/data/x")
    assert deep.exists()
    assert ui_settings.get_scan_dir() == "/data/x"


def test_blank_value_clears_memory(settings: Path):
    ui_settings.remember_scan_dir("/data/x")
    ui_settings.remember_scan_dir("   ")
    assert ui_settings.get_scan_dir() is None
    assert json.loads(settings.read_text(encoding="utf-8")) == {}


def test_strips_surrounding_whitespace(settings: Path):
    ui_settings.remember_scan_dir("  /data/x  ")
    assert ui_settings.get_scan_dir() == "/data/x"


def test_corrupt_file_degrades_to_no_memory(settings: Path):
    settings.write_text("{ this is not json", encoding="utf-8")
    assert ui_settings.load_settings() == {}
    assert ui_settings.get_scan_dir() is None


def test_non_object_json_degrades_to_no_memory(settings: Path):
    settings.write_text("[1, 2, 3]", encoding="utf-8")
    assert ui_settings.load_settings() == {}
    assert ui_settings.get_scan_dir() is None


def test_non_string_value_ignored(settings: Path):
    settings.write_text(json.dumps({"scan_dir": 123}), encoding="utf-8")
    assert ui_settings.get_scan_dir() is None


def test_write_failure_does_not_raise(tmp_path: Path, monkeypatch):
    """磁盘只读 / 权限不足时，页面不能因此崩掉。"""
    monkeypatch.setenv(ui_settings.SETTINGS_ENV, str(tmp_path / "ui_settings.json"))

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ui_settings, "_write", boom)
    ui_settings.remember_scan_dir("/data/x")  # 不抛异常即通过
