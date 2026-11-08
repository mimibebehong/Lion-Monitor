"""老库指标定义补参数声明（S4W-3）：``params_schema_json`` 是后加列，老行只拿到空声明。

现象：``metric_def`` 的三条内置指标（v_RMS / a_Peak / a_RMS）建于 ``params_schema_json``
列新增之前（``_add_metric_def_params`` 只做 ``ALTER TABLE ... DEFAULT '[]'``，没有回填），
于是健康树「监测指标」页签里：

- 指标节点显示「未绑定参数」（``metric_assign`` 没有行）
- 点「绑定参数」后，``_render_bound_metric`` 见 ``schema`` 为空，只打印
  「该指标没有声明参数 schema，按系统默认值计算」并 return → **「计算时长」输入框不出现**

口径（本次回填）：

- 只补**空声明**（``[]`` / 空串 / NULL）且指标码在内置 ``PARAM_SCHEMA`` 里的行；
  已有自定义声明的行原样保留（用户可能按需裁过字段）
- 定义默认值只在为空时补，已有自定义默认值不动
- 未知指标码（自定义指标）保持原样，不替它猜 schema
- 幂等：第二次执行返回 0，库内容不变
"""

from __future__ import annotations

import json

import pytest

from lion_monitor.store import db as store_db

#: 三条内置指标：老库里的参数声明是空的
BUILTIN_CODES = ("v_RMS", "a_Peak", "a_RMS")

LEGACY_INSERT = (
    "INSERT INTO metric_def"
    " (task_id, code, unit, params_json, enabled, created_at, deleted_at,"
    "  params_schema_json, params_default_json)"
    " VALUES (?, ?, ?, '{}', 1, '2026-10-29 20:24:27', NULL, ?, ?)"
)


def _seed_legacy(engine, rows: list[tuple[str, str, str, str]]) -> None:
    """按老库形态写入指标定义：(code, unit, schema_json, defaults_json)。"""
    with engine.begin() as conn:
        for code, unit, schema_json, defaults_json in rows:
            conn.exec_driver_sql(LEGACY_INSERT, (1, code, unit, schema_json, defaults_json))


def _read(engine, code: str) -> tuple[list, dict]:
    """读出某指标的 (参数声明, 定义默认值)。"""
    with engine.begin() as conn:
        row = conn.exec_driver_sql(
            "SELECT params_schema_json, params_default_json FROM metric_def WHERE code = ?",
            (code,),
        ).fetchone()
    assert row is not None, f"库里没有指标 {code}"
    return json.loads(row[0] or "[]"), json.loads(row[1] or "{}")


def _schema_item(schema: list, key: str) -> dict:
    for item in schema:
        if item.get("key") == key:
            return item
    raise AssertionError(f"参数声明里没有 {key}：{[i.get('key') for i in schema]}")


@pytest.fixture()
def legacy_db(tmp_path):
    """老库：三条内置指标，参数声明与默认值都是空的。"""
    engine = store_db.make_engine(tmp_path / "legacy_schema.db")
    store_db.init_db(engine)
    _seed_legacy(
        engine,
        [
            ("v_RMS", "mm/s", "[]", "{}"),
            ("a_Peak", "g", "[]", "{}"),
            ("a_RMS", "g", "[]", "{}"),
        ],
    )
    return engine


def test_init_db_backfills_builtin_schema(legacy_db):
    """``init_db`` 要给三条内置指标补上声明与默认值，界面才渲染得出「计算时长」。"""
    store_db.init_db(legacy_db)

    for code in BUILTIN_CODES:
        schema, defaults = _read(legacy_db, code)
        assert schema, f"{code} 的参数声明仍是空的"
        window_item = _schema_item(schema, "window_s")
        assert window_item["label"] == "计算时长"
        assert window_item["unit"] == "s"
        assert float(window_item["default"]) == 2.0
        assert window_item["group"] == "basic"
        assert _schema_item(schema, "step_s")["default"] == 2.0
        assert float(defaults["window_s"]) == 2.0
        assert float(defaults["step_s"]) == 2.0


def test_backfill_is_idempotent(legacy_db):
    """第二次执行不再改动，且函数本身返回 0（可安全挂启动路径）。"""
    store_db.init_db(legacy_db)
    first_schema, first_defaults = _read(legacy_db, "v_RMS")

    assert store_db.migrate_metric_def_schema(legacy_db) == 0

    assert _read(legacy_db, "v_RMS") == (first_schema, first_defaults)


def test_backfill_keeps_custom_schema(tmp_path):
    """已有自定义声明/默认值不许被覆盖；但内置新增参数（如 a_RMS 带通）会补进来。"""
    engine = store_db.make_engine(tmp_path / "custom_schema.db")
    store_db.init_db(engine)
    custom_schema = [
        {"key": "window_s", "label": "计算时长", "unit": "s", "type": "float", "default": 5.0}
    ]
    _seed_legacy(
        engine,
        [("v_RMS", "mm/s", json.dumps(custom_schema, ensure_ascii=False), '{"window_s": 5.0}')],
    )

    store_db.init_db(engine)

    schema, defaults = _read(engine, "v_RMS")
    # 原有项原样保留（window_s 的自定义默认 5.0 不被改）
    assert schema[0] == custom_schema[0]
    assert defaults["window_s"] == 5.0
    # 缺的内置项被补齐（否则 a_RMS 的带通参数永远出不来）
    keys = [item["key"] for item in schema]
    assert "step_s" in keys and "band_low" in keys and "band_high" in keys


def test_backfill_skips_unknown_code(tmp_path):
    """自定义指标（不在内置 schema 里）没有可依据的声明，保持空。"""
    engine = store_db.make_engine(tmp_path / "unknown_code.db")
    store_db.init_db(engine)
    _seed_legacy(engine, [("kurtosis", "", "[]", "{}")])

    store_db.init_db(engine)

    assert _read(engine, "kurtosis") == ([], {})
