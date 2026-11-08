"""Windows 原生「浏览文件夹」对话框：路径换算、宿主编译与调用（2026-11-03 定）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lion_monitor.features import win_picker  # noqa: E402


def test_windows_path_from_wsl_drive():
    assert win_picker.windows_path_from_wsl("/mnt/d/hermes_wp/x") == "D:\\hermes_wp\\x"
    assert win_picker.windows_path_from_wsl("/mnt/d") == "D:\\"
    assert win_picker.windows_path_from_wsl("/mnt/d/") == "D:\\"


def test_windows_path_from_wsl_non_drive_returns_none():
    assert win_picker.windows_path_from_wsl("/home/administrator") is None
    assert win_picker.windows_path_from_wsl("") is None
    assert win_picker.windows_path_from_wsl(None) is None


def test_wsl_path_from_windows_drive():
    assert win_picker.wsl_path_from_windows("D:\\hermes_wp\\x") == "/mnt/d/hermes_wp/x"
    assert win_picker.wsl_path_from_windows("d:\\a") == "/mnt/d/a"
    assert win_picker.wsl_path_from_windows("D:\\") == "/mnt/d"


def test_wsl_path_from_windows_unc_back_to_wsl():
    assert (
        win_picker.wsl_path_from_windows("\\\\wsl.localhost\\Ubuntu\\home\\administrator")
        == "/home/administrator"
    )
    assert win_picker.wsl_path_from_windows("\\\\wsl$\\Ubuntu\\home\\a") == "/home/a"


def test_wsl_path_from_windows_handles_cancel_and_junk():
    assert win_picker.wsl_path_from_windows("CANCELLED") is None
    assert win_picker.wsl_path_from_windows("") is None
    assert win_picker.wsl_path_from_windows(None) is None
    assert win_picker.wsl_path_from_windows("not a path") is None


def test_parse_output():
    assert win_picker.parse_output("D:\\a\\b\r\n") == "D:\\a\\b"
    assert win_picker.parse_output("CANCELLED\r\n") is None
    assert win_picker.parse_output("") is None
    assert win_picker.parse_output(None) is None


def test_source_file_ships_with_package():
    """宿主源码必须随包走，否则换机器编译不出来。"""
    assert win_picker.SOURCE.is_file(), win_picker.SOURCE
    text = win_picker.SOURCE.read_text(encoding="utf-8")
    assert "FolderBrowserDialog" in text
    assert "HwndTopmost" in text, "必须把对话框设成 TOPMOST，否则会被浏览器挡住"


def _fake_runner(tmp_path: Path, payload: str, *, calls: list | None = None):
    """假的宿主调用：把结果写进「Windows 输出文件」（这里落在 tmp_path）。"""

    def runner(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        for arg in cmd:
            token = str(arg).rsplit("lion_pick_", 1)[-1].replace(".txt", "")
            if token and token != str(arg):
                (tmp_path / f"lion_pick_{token}.txt").write_text(payload, encoding="utf-16")
                break
        return subprocess.CompletedProcess(cmd, 0)

    return runner


def _patch_helper(monkeypatch, tmp_path):
    """绕过真实编译：假设宿主已就绪、picker 可用。"""
    monkeypatch.setattr(win_picker, "picker_available", lambda: True)
    fake_exe = tmp_path / "helper_dir" / win_picker.HELPER_EXE_NAME
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_text("exe", encoding="utf-8")
    monkeypatch.setattr(win_picker, "compile_helper", lambda **kwargs: fake_exe)
    return fake_exe


def test_pick_folder_returns_platform_path(monkeypatch, tmp_path):
    exe = _patch_helper(monkeypatch, tmp_path)
    calls: list = []
    windows_initial = "D:\\hermes_wp\\Project"
    initial = windows_initial if sys.platform == "win32" else "/mnt/d/hermes_wp/Project"

    result = win_picker.pick_folder(
        initial,
        runner=_fake_runner(tmp_path, windows_initial + "\r\n", calls=calls),
        shared_dir=tmp_path,
        timeout=5,
    )

    if sys.platform == "win32":
        assert result.path == windows_initial, result
    else:
        assert result.path == "/mnt/d/hermes_wp/Project", result
    assert result.error is None and result.cancelled is False
    cmd, kwargs = calls[0]
    assert cmd[0] == str(exe)
    assert cmd[2] == "选择数据目录"
    assert cmd[3] == windows_initial, "起始目录要传 Windows 路径"
    assert kwargs["timeout"] == 5


def test_pick_folder_cancel(monkeypatch, tmp_path):
    _patch_helper(monkeypatch, tmp_path)

    result = win_picker.pick_folder(
        "/mnt/d", runner=_fake_runner(tmp_path, "CANCELLED\r\n"), shared_dir=tmp_path
    )

    assert result.cancelled is True
    assert result.path is None and result.error is None


def test_pick_folder_timeout(monkeypatch, tmp_path):
    _patch_helper(monkeypatch, tmp_path)

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    result = win_picker.pick_folder("/mnt/d", runner=boom, shared_dir=tmp_path)

    assert result.error and "超时" in result.error
    assert not list(tmp_path.glob("lion_pick_*")), list(tmp_path.iterdir())


def test_pick_folder_cleans_up_after_success(monkeypatch, tmp_path):
    _patch_helper(monkeypatch, tmp_path)

    win_picker.pick_folder(
        "/mnt/d", runner=_fake_runner(tmp_path, "D:\\a\r\n"), shared_dir=tmp_path
    )

    assert not list(tmp_path.glob("lion_pick_*")), list(tmp_path.iterdir())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上 UNC 路径本来就可用")
def test_pick_folder_unsupported_selection(monkeypatch, tmp_path):
    _patch_helper(monkeypatch, tmp_path)

    result = win_picker.pick_folder(
        "/mnt/d", runner=_fake_runner(tmp_path, "\\\\someserver\\share\r\n"), shared_dir=tmp_path
    )

    assert result.error and "没法在服务器上访问" in result.error


def test_pick_folder_reports_compile_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(win_picker, "picker_available", lambda: True)
    monkeypatch.setattr(win_picker, "compile_helper", lambda **kwargs: None)

    result = win_picker.pick_folder("/mnt/d", shared_dir=tmp_path)

    assert result.error and "手填目录" in result.error


def test_compile_helper_reuses_fresh_exe(tmp_path):
    helper_dir = tmp_path / "bin"
    helper_dir.mkdir()
    source = tmp_path / "win_pick_folder.cs"
    source.write_text("// src", encoding="utf-8")
    exe = helper_dir / win_picker.HELPER_EXE_NAME
    exe.write_text("exe", encoding="utf-8")

    calls: list = []
    got = win_picker.compile_helper(
        compiler=lambda *a, **k: calls.append(a) or subprocess.CompletedProcess([], 0),
        helper_dir=helper_dir,
        source=source,
    )

    assert got == exe
    assert calls == [], "exe 已是最新，不该再编译"


def test_compile_helper_rebuilds_when_source_newer(tmp_path):
    helper_dir = tmp_path / "bin"
    helper_dir.mkdir()
    source = tmp_path / "win_pick_folder.cs"
    source.write_text("// src2", encoding="utf-8")
    exe = helper_dir / win_picker.HELPER_EXE_NAME
    calls: list = []

    def compiler(cmd, **kwargs):
        calls.append(cmd)
        exe.write_text("exe", encoding="utf-8")  # 模拟 csc 产出
        return subprocess.CompletedProcess(cmd, 0)

    got = win_picker.compile_helper(compiler=compiler, helper_dir=helper_dir, source=source)

    assert got == exe
    assert len(calls) == 1
    joined = " ".join(calls[0])
    assert "/target:winexe" in joined, joined
    assert "win_pick_folder.cs" in joined


def test_compile_helper_returns_none_when_csc_missing(tmp_path):
    source = tmp_path / "win_pick_folder.cs"
    source.write_text("// src", encoding="utf-8")

    got = win_picker.compile_helper(
        compiler=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no csc")),
        helper_dir=tmp_path / "bin",
        source=source,
    )

    assert got is None


def test_picker_available_reflects_csc(monkeypatch, tmp_path):
    monkeypatch.setattr(win_picker, "CSC", tmp_path / "nope.exe")
    assert win_picker.picker_available() is False
    (tmp_path / "csc.exe").write_text("x", encoding="utf-8")
    monkeypatch.setattr(win_picker, "CSC", tmp_path / "csc.exe")
    assert win_picker.picker_available() is True
