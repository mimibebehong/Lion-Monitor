"""安装后铺演示数据（tools/seed_demo.py）的回归测试。

为什么不直接用 Inno 把数据装进 ``data\\``：卸载器会把它当成「我装的」一起删，
用户导入的数据就没了。改成安装后由脚本新建，卸载器不认识 → ``data\\`` 保留。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "tools", ROOT / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import seed_demo  # noqa: E402


def _fake_tree(root: Path) -> None:
    demo = root / "demo_data"
    (demo / "四通道保存" / "批次A").mkdir(parents=True)
    (demo / "四通道保存" / "批次A" / "a.dat").write_bytes(b"x")
    (demo / "lion.db").write_bytes(b"SQLite format 3\x00")


def test_seeds_missing_data(tmp_path: Path) -> None:
    _fake_tree(tmp_path)
    copied = seed_demo._copy_demo_files(tmp_path)
    assert "lion.db" in " ".join(copied)
    assert (tmp_path / "data" / "lion.db").is_file()
    assert (tmp_path / "data" / "四通道保存" / "批次A" / "a.dat").is_file()


def test_upgrade_never_overwrites_user_data(tmp_path: Path) -> None:
    """升级安装：data\\ 里已有库和数据，绝不能被演示数据覆盖。"""
    _fake_tree(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / "lion.db").write_bytes(b"USER DB")
    (data / "四通道保存").mkdir()

    copied = seed_demo._copy_demo_files(tmp_path)

    assert copied == []
    assert (data / "lion.db").read_bytes() == b"USER DB"


def test_no_demo_data_is_noop(tmp_path: Path) -> None:
    assert seed_demo._copy_demo_files(tmp_path) == []


def test_installer_uses_seed_demo_not_inno_files() -> None:
    iss = (ROOT / "installer" / "LionMonitor.iss").read_text(encoding="utf-8")
    assert "seed_demo.py" in iss, "安装后没跑 seed_demo.py"
    assert 'DestDir: "{app}\\data"' not in iss, "不能再用 [Files] 直接装进 data\\（卸载会删库）"
    assert "demo_data\\*" in iss and "data\\lion.db*" in iss
    assert "演示数据/演示库必须排除在主 [Files] 之外"
    # 演示数据被上面那条 Excludes 排掉了，必须另有独立条目装进 {app}\demo_data，
    # 否则 seed_demo 没东西可铺 → 装完是空库（2026-11-06 实测：data\ 只剩探针库）
    assert 'Source: "..\\dist\\demo_data\\*"; DestDir: "{app}\\demo_data"' in iss


def test_build_packs_demo_db(tmp_path: Path, monkeypatch) -> None:
    """build.py 要把演示库一起放进 demo_data\\（安装时才有东西可铺）。"""
    import build

    root = tmp_path / "repo"
    (root / "dist" / "data").mkdir(parents=True)
    (root / "dist" / "data" / "lion.db").write_bytes(b"db")
    src = root / "src_data"
    (src / "批次A").mkdir(parents=True)
    (src / "批次A" / "a.dat").write_bytes(b"x")

    monkeypatch.setattr(build, "ROOT", root)
    monkeypatch.setattr(build, "DIST", root / "dist")
    build.copy_demo_data(src)

    assert (root / "dist" / "demo_data" / "lion.db").is_file()
    assert (root / "dist" / "demo_data" / "四通道保存" / "批次A" / "a.dat").is_file()


def test_build_never_ships_data_dir(tmp_path: Path, monkeypatch) -> None:
    """卸载保数据的真正前提：``dist\\data`` 里除了库文件什么都不许有。

    实测 2026-11-06：老构建把演示 .dat 留在 dist\\data，安装器照装不误，
    卸载后 data\\四通道保存 的 13 个 .dat 全被删（只剩 lion.db）。
    """
    import build

    root = tmp_path / "repo"
    data = root / "dist" / "data"
    (data / "四通道保存" / "批次A").mkdir(parents=True)
    (data / "四通道保存" / "批次A" / "a.dat").write_bytes(b"x")
    (data / "lion.db").write_bytes(b"db")
    (data / "lion.db-wal").write_bytes(b"wal")

    monkeypatch.setattr(build, "ROOT", root)
    monkeypatch.setattr(build, "DIST", root / "dist")
    build.clean_build_artifacts(keep_data=True)

    left = sorted(p.name for p in data.rglob("*") if p.is_file())
    assert left == ["lion.db"], f"dist\\data 还留着会进包的东西：{left}"

    # 有杂质时宁可构建失败，也不要打一个「卸载会丢数据」的包
    (data / "四通道保存").mkdir()
    (data / "四通道保存" / "b.dat").write_bytes(b"x")
    import pytest

    with pytest.raises(build.BuildError):
        build._assert_dist_data_clean()
