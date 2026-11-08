"""安装自检（阶段 2）：把 §16 的三平台差异变成可执行检查。

设计要点：
- 逻辑放 ``src/lion_monitor/deploy/selfcheck.py``，页面「环境自检」按钮与安装器共用一套；
  ``tools/selfcheck.py`` 只是 CLI 薄壳。
- Windows 专有项（浏览器、Server Core、防火墙）在非 Windows 上返回 ``skip``，WSL 也能跑测试。
- 失败项必须带人话建议（``fix``），马哥要求「不要没反应」。
"""

from __future__ import annotations

import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from lion_monitor.deploy import selfcheck as sc

# ---------------------------------------------------------------- Python / 依赖


def test_check_python_ok() -> None:
    check = sc.check_python((3, 10))
    assert check.status == sc.OK
    assert check.detail


def test_check_python_too_old() -> None:
    check = sc.check_python((99, 0))
    assert check.status == sc.FAIL
    assert "99" in check.fix or "Python" in check.fix


def test_check_modules_all_present() -> None:
    check = sc.check_modules(("json", "sqlite3"), label="核心依赖")
    assert check.status == sc.OK
    assert "2" in check.detail


def test_check_modules_missing_required_is_fail() -> None:
    check = sc.check_modules(("json", "不存在的模块_xyz"), label="核心依赖")
    assert check.status == sc.FAIL
    assert "不存在的模块_xyz" in check.detail
    assert check.fix


def test_check_modules_missing_optional_is_warn() -> None:
    check = sc.check_optional_modules(("json", "不存在的可选模块_xyz"))
    assert check.status == sc.WARN
    assert "不存在的可选模块_xyz" in check.detail


def test_check_optional_modules_all_present_is_ok() -> None:
    check = sc.check_optional_modules(("json", "sqlite3"))
    assert check.status == sc.OK


# ---------------------------------------------------------------- 目录 / 数据库


def test_check_writable_creates_missing_dir(tmp_path: Path) -> None:
    target = tmp_path / "深" / "一层" / "logs"
    check = sc.check_writable(target, label="日志目录")
    assert check.status == sc.OK
    assert target.is_dir()
    assert not list(target.iterdir()), "试写文件必须清理干净"


def test_check_writable_rejects_path_that_is_a_file(tmp_path: Path) -> None:
    blocker = tmp_path / "占位"
    blocker.write_text("x", encoding="utf-8")
    check = sc.check_writable(blocker, label="数据目录")
    assert check.status == sc.FAIL
    assert check.fix


def test_check_sqlite_roundtrip(tmp_path: Path) -> None:
    check = sc.check_sqlite(tmp_path / "probe.db")
    assert check.status == sc.OK
    assert (tmp_path / "probe.db").exists()


def test_check_sqlite_rejects_unwritable_location(tmp_path: Path) -> None:
    blocker = tmp_path / "占位"
    blocker.write_text("x", encoding="utf-8")
    check = sc.check_sqlite(blocker / "probe.db")
    assert check.status == sc.FAIL
    assert check.fix


# ---------------------------------------------------------------- 端口


def test_check_port_free() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
    check = sc.check_port(free_port)
    assert check.status == sc.OK
    assert str(free_port) in check.detail


def test_check_port_busy_reports_process_hint() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        busy_port = sock.getsockname()[1]
        check = sc.check_port(busy_port)
    assert check.status == sc.FAIL
    assert str(busy_port) in check.detail
    assert check.fix


def _serve(handler_factory) -> tuple[HTTPServer, int]:
    import threading

    server = handler_factory()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


def _streamlit_like(index_status: int) -> HTTPServer:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
            if self.path == "/_stcore/health":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(index_status)
                self.end_headers()
                self.wfile.write(b"Internal Server Error" if index_status == 500 else b"ok")

        def log_message(self, *args: object) -> None:
            pass

    return HTTPServer(("127.0.0.1", 0), _Handler)


def test_check_port_ok_when_our_app_already_running() -> None:
    """端口上跑的就是本程序、首页也正常时不能报「被占用」。

    现场场景：装完先双击了启动器，再跑自检 —— 报占用只会让人白折腾。
    """
    server, port = _serve(lambda: _streamlit_like(200))
    try:
        check = sc.check_port(port)
    finally:
        server.shutdown()
        server.server_close()
    assert check.status == sc.OK
    assert "Lion-Monitor" in check.detail


def test_check_port_installer_mode_rejects_running_instance() -> None:
    """安装器口径：装文件前端口上还有实例 = 失败（否则会覆盖运行中的程序文件）。"""
    server, port = _serve(lambda: _streamlit_like(200))
    try:
        check = sc.check_port(port, expect_stopped=True)
    finally:
        server.shutdown()
        server.server_close()
    assert check.status == sc.FAIL
    assert "安装前必须先把它停掉" in check.detail
    assert check.fix and "sc stop LionMonitor" in check.fix


def test_check_port_flags_broken_instance_500() -> None:
    """健康检查过了但首页 500 —— 就是现场看到的 Internal Server Error。"""
    server, port = _serve(lambda: _streamlit_like(500))
    try:
        check = sc.check_port(port)
    finally:
        server.shutdown()
        server.server_close()
    assert check.status == sc.FAIL
    assert "500" in check.detail
    assert check.fix and "修复安装" in check.fix


def test_check_program_files_reports_missing(tmp_path: Path) -> None:
    check = sc.check_program_files(tmp_path)
    assert check.status == sc.FAIL
    assert "app/main.py" in check.detail or "app/main.py" in (check.fix or "") + check.detail
    assert check.fix


def test_check_program_files_ok_when_all_present(tmp_path: Path) -> None:
    for name in sc.REQUIRED_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    check = sc.check_program_files(tmp_path)
    assert check.status == sc.OK
    assert str(len(sc.REQUIRED_FILES)) in check.detail


def test_required_files_cover_index_and_entry() -> None:
    """首页 500 最常见的原因：入口脚本或 streamlit 静态资源缺失。"""
    assert "app/main.py" in sc.REQUIRED_FILES
    assert any("streamlit/static/index.html" in name for name in sc.REQUIRED_FILES)


def test_run_selfcheck_includes_program_files_check(tmp_path: Path) -> None:
    checks = sc.run_selfcheck(project_root=tmp_path, port=0, scan_dir=None)
    keys = [check.key for check in checks]
    assert "program_files" in keys
    assert keys.index("program_files") < keys.index("port"), "先查文件完整性，再查端口"


def test_runtime_import_check_detects_broken_env(tmp_path: Path) -> None:
    """真导入一遍才抓得到「包装了一半」—— find_spec 是看不出来的。

    2026-11-06 现场就是这个坑：plotly 的 graph_objs\\layout\\template\\data 少了，
    streamlit 导入时 AttributeError，浏览器只有 Internal Server Error，
    而按 find_spec 判的自检全绿。
    """
    check = sc.check_runtime_import(tmp_path)  # tmp 里没有 runtime\python.exe → 用当前解释器
    assert check.status in {sc.OK, sc.FAIL}
    if check.status == sc.FAIL:
        assert check.fix
    # 关键：这一项必须在自检清单里，且排在端口检查之前
    keys = [item.key for item in sc.run_selfcheck(tmp_path, port=0)]
    assert "runtime_import" in keys
    assert keys.index("runtime_import") < keys.index("port")


def test_safe_stdio_survives_stream_without_reconfigure() -> None:
    """stdout 被换成不支持 reconfigure 的对象时不能让自检崩掉。"""

    class _Plain:
        def write(self, text: str) -> int:
            return len(text)

        def flush(self) -> None:
            pass

    original = sc.sys.stdout
    sc.sys.stdout = _Plain()  # type: ignore[assignment]
    try:
        sc._safe_stdio()
    finally:
        sc.sys.stdout = original


# ---------------------------------------------------------------- 扫描目录


def test_check_scan_dir_lists_subdirs(tmp_path: Path) -> None:
    (tmp_path / "批次A").mkdir()
    (tmp_path / "批次B").mkdir()
    (tmp_path / "文件.txt").write_text("x", encoding="utf-8")
    check = sc.check_scan_dir(tmp_path)
    assert check.status == sc.OK
    assert "2" in check.detail, "只数子目录，不算文件"


def test_check_scan_dir_missing(tmp_path: Path) -> None:
    check = sc.check_scan_dir(tmp_path / "不存在")
    assert check.status == sc.FAIL
    assert "不存在" in check.detail
    assert check.fix


def test_check_scan_dir_none_is_skip() -> None:
    check = sc.check_scan_dir(None)
    assert check.status == sc.SKIP


# ---------------------------------------------------------------- 平台相关项


def test_check_browser_returns_known_status() -> None:
    check = sc.check_browser()
    assert check.status in {sc.OK, sc.WARN, sc.SKIP}
    if check.status == sc.WARN:
        assert "Edge" in check.fix or "浏览器" in check.fix


def test_check_desktop_session_status() -> None:
    check = sc.check_desktop_session()
    assert check.status in {sc.OK, sc.FAIL, sc.SKIP}


def test_check_firewall_status() -> None:
    check = sc.check_firewall(8501)
    assert check.status in {sc.OK, sc.WARN, sc.SKIP}


def test_check_disk_space(tmp_path: Path) -> None:
    check = sc.check_disk(tmp_path, min_free_mb=1)
    assert check.status == sc.OK
    assert "MB" in check.detail


def test_local_ipv4_returns_strings() -> None:
    ips = sc.local_ipv4()
    assert isinstance(ips, list)
    assert all(isinstance(ip, str) and ip.count(".") == 3 for ip in ips)


# ---------------------------------------------------------------- 汇总 / 报告


def _fake(status: str, key: str = "k", title: str = "标题") -> sc.Check:
    return sc.Check(key=key, title=title, status=status, detail="d", fix="f")


def test_summarize_exit_code_ok() -> None:
    code, text = sc.summarize([_fake(sc.OK, "a", "甲"), _fake(sc.WARN, "b", "乙")])
    assert code == 0
    assert "甲" in text and "乙" in text


def test_summarize_exit_code_fail() -> None:
    code, text = sc.summarize([_fake(sc.OK, "a", "甲"), _fake(sc.FAIL, "b", "乙")])
    assert code == 1
    assert "乙" in text


def test_summarize_mentions_skip_count() -> None:
    _, text = sc.summarize([_fake(sc.OK), _fake(sc.SKIP)])
    assert "跳过" in text


def test_run_selfcheck_collects_unique_keys(tmp_path: Path) -> None:
    checks = sc.run_selfcheck(project_root=tmp_path, port=0, scan_dir=tmp_path)
    assert len(checks) >= 8
    keys = [c.key for c in checks]
    assert len(keys) == len(set(keys)), "每项检查的 key 必须唯一"
    assert all(c.title and c.status in {sc.OK, sc.WARN, sc.FAIL, sc.SKIP} for c in checks)


def test_run_selfcheck_reports_port_from_argument(tmp_path: Path) -> None:
    checks = sc.run_selfcheck(project_root=tmp_path, port=0, scan_dir=None)
    port_check = next(c for c in checks if c.key == "port")
    assert port_check.status == sc.OK


def test_main_prints_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    code = sc.main(["--json", "--project-root", str(tmp_path), "--scan-dir", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == code
    assert isinstance(payload["checks"], list)
    assert payload["checks"][0]["status"] in {"ok", "warn", "fail", "skip"}
