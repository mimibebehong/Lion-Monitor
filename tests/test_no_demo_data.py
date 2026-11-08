"""演示数据清理守卫：``app/`` 与 ``src/`` 不得再引用 ``demo_data``。

验收口径（2026-10-31 要求）：
- ``grep -rn "demo_data" app/ src/`` 结果为 0；
- 演示数据模块与 session_state 版健康树存储（已被 ``store/tree.py`` 取代）一并移除；
- 健康树读写全部走真实库 ``data/lion.db``。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
CODE_DIRS = ("app", "src")


def test_app_and_src_do_not_reference_demo_data():
    offenders = [
        f"{path.relative_to(ROOT)}:{lineno}:{line.strip()}"
        for name in CODE_DIRS
        for path in sorted((ROOT / name).rglob("*.py"))
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "demo_data" in line
    ]
    assert offenders == []


def test_demo_data_module_is_removed():
    assert not (APP / "demo_data.py").exists()


def test_session_state_tree_store_is_removed():
    assert not (APP / "tree_store.py").exists()
