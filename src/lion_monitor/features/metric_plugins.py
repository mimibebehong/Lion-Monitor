"""自定义指标算法插件（2026-11-03：接口我定，用户自己新增算法）。

放一个 ``.py`` 到插件目录即可新增一个可算的指标码，不用改项目代码、不用重启算法逻辑
（每次计算前按文件 mtime 热加载，改完存盘就生效）。

用法：把下面的模板存成 ``~/.lion_monitor/metrics_plugins/kurtosis.py``

.. code-block:: python

    CODE = "kurtosis"            # 必填：指标码（字母开头，任务内唯一，不能和内置重名）
    UNIT = ""                    # 选填：单位（显示在趋势纵轴 / 阈值）
    TITLE = "峭度"                # 选填：中文名（界面展示）
    DESCRIPTION = "四阶矩，冲击敏感"  # 选填：一句话说明
    PARAMS_SCHEMA = [             # 选填：指标节点上要渲染的参数（沿用内置 schema 格式）
        {"key": "window_s", "label": "计算时长（s）", "kind": "number", "min": 0.05},
        {"key": "step_s", "label": "计算步长（s）", "kind": "number", "min": 0.05},
    ]

    def compute(window, sample_rate, params):
        \"\"\"算一个值。

        window      : numpy.ndarray[float64]，一个窗的样本（已去直流）
        sample_rate : float，采样率 Hz
        params      : dict，该指标节点上生效的参数（含 window_s / step_s）
        返回        : float，必须有限（NaN / inf 会被判为计算失败）
        \"\"\"
        import numpy as np

        x = np.asarray(window, dtype="float64")
        sigma = float(x.std())
        if sigma <= 0:
            return 0.0
        return float(np.mean((x - x.mean()) ** 4) / sigma**4)

约定：
- 逐窗调用一次，窗长 / 步长由健康树上的指标参数决定（``window_s`` / ``step_s``）；
- 一个窗只返回一个浮点值，多个点由窗切分自动产生；
- 抛异常或返回非有限值 → 该文件判为计算失败并写明原因（不会静默出 0）；
- 插件码不能和内置指标（``v_RMS`` / ``a_Peak`` / ``a_RMS``）同名，冲突会报错并跳过该文件。
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..constants import METRIC_UNITS

__all__ = [
    "MetricPlugin",
    "PluginError",
    "available_codes",
    "compute_plugin_value",
    "install_plugin",
    "load_plugins",
    "plugin_dir",
    "remove_plugin",
    "unit_of",
    "validate_code_known",
]

_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,40}$")

#: 插件目录的环境变量名（测试用临时目录覆盖）
PLUGIN_DIR_ENV = "LION_METRIC_PLUGINS"

#: 缓存：(文件指纹, 插件表, 错误表)
_CACHE: dict[str, Any] = {"stamp": None, "plugins": {}, "errors": []}


class PluginError(Exception):
    """插件加载或计算失败。"""


@dataclass(frozen=True, slots=True)
class MetricPlugin:
    """一个已加载的自定义指标算法。"""

    code: str
    unit: str
    title: str
    description: str
    params_schema: list[dict[str, Any]] = field(default_factory=list)
    compute: Callable[[np.ndarray, float, dict[str, Any]], float] = lambda *_: 0.0
    path: Path | None = None

    @property
    def label(self) -> str:
        return (
            f"{self.code}（{self.title}）" if self.title and self.title != self.code else self.code
        )


def plugin_dir() -> Path:
    """插件目录：默认 ``~/.lion_monitor/metrics_plugins``，可用 ``LION_METRIC_PLUGINS`` 覆盖。"""
    raw = os.environ.get(PLUGIN_DIR_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".lion_monitor" / "metrics_plugins"


def _stamp(files: list[Path]) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(files)
        if path.exists()
    )


def _load_one(path: Path) -> MetricPlugin:
    """导入单个插件文件并校验；任何不合规都抛 :class:`PluginError`。"""
    name = f"lion_metric_plugin_{path.stem}_{path.stat().st_mtime_ns}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PluginError("无法作为 Python 模块加载")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - 插件代码不可信，任何异常都只判该文件失败
        sys.modules.pop(name, None)
        raise PluginError(f"导入失败：{type(exc).__name__}: {exc}") from exc

    code = getattr(module, "CODE", None)
    if not isinstance(code, str) or not _CODE_RE.match(code):
        raise PluginError("缺少合法的 CODE（字母开头，字母/数字/下划线，2-64 字符）")
    if code in METRIC_UNITS:
        raise PluginError(f"CODE「{code}」与内置指标重名（内置：{' / '.join(METRIC_UNITS)}）")
    compute = getattr(module, "compute", None)
    if not callable(compute):
        raise PluginError("缺少 compute(window, sample_rate, params) 函数")

    schema = getattr(module, "PARAMS_SCHEMA", []) or []
    if not isinstance(schema, list) or any(not isinstance(item, dict) for item in schema):
        raise PluginError("PARAMS_SCHEMA 必须是对象数组")
    unit = getattr(module, "UNIT", "") or ""
    title = getattr(module, "TITLE", "") or code
    description = getattr(module, "DESCRIPTION", "") or ""
    return MetricPlugin(
        code=code,
        unit=str(unit),
        title=str(title),
        description=str(description),
        params_schema=[dict(item) for item in schema],
        compute=compute,
        path=path,
    )


def load_plugins(force: bool = False) -> tuple[dict[str, MetricPlugin], list[tuple[Path, str]]]:
    """扫描插件目录，返回 ``(插件表, 错误表)``；文件没变就用缓存（不重复导入）。"""
    folder = plugin_dir()
    if not folder.is_dir():
        # 首次使用自动建目录，用户把 .py 丢进去就行（失败就算了，不影响计算）
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    files = (
        [path for path in folder.glob("*.py") if not path.name.startswith(("_", "."))]
        if folder.is_dir()
        else []
    )
    stamp = _stamp(files)
    if not force and _CACHE["stamp"] == stamp:
        return dict(_CACHE["plugins"]), list(_CACHE["errors"])

    plugins: dict[str, MetricPlugin] = {}
    errors: list[tuple[Path, str]] = []
    for path in files:
        try:
            plugin = _load_one(path)
        except PluginError as exc:
            errors.append((path, str(exc)))
            continue
        if plugin.code in plugins:
            errors.append(
                (
                    path,
                    f"CODE「{plugin.code}」与 {plugins[plugin.code].path.name} 重复，已忽略本文件",
                )
            )
            continue
        plugins[plugin.code] = plugin

    _CACHE.update({"stamp": stamp, "plugins": plugins, "errors": errors})
    return dict(plugins), list(errors)


def install_plugin(filename: str, source: str | bytes, *, overwrite: bool = False) -> MetricPlugin:
    """把上传的算法源码装进插件目录（先校验、后落盘）。

    - 只收单个 ``.py``，文件名不能以下划线开头
    - ``CODE`` 必须合法且不与内置指标重名；与其他插件重名要改码
    - 同名文件已存在时需 ``overwrite=True``
    - 校验不通过不会在插件目录留下任何文件
    """
    name = Path(str(filename or "")).name
    if not name.endswith(".py") or name.startswith("_"):
        raise PluginError("只接受单个 .py 文件（文件名不能以下划线开头）")
    text = source.decode("utf-8") if isinstance(source, bytes) else str(source)
    if not text.strip():
        raise PluginError("文件是空的")

    folder = plugin_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if target.exists() and not overwrite:
        raise PluginError(f"插件目录里已有 {name}：要替换请勾选「覆盖同名文件」")

    # 先落到隐藏临时文件做校验（glob("*.py") 不收隐藏文件），通过后再改名到位
    staging = folder / ".incoming"
    staging.mkdir(exist_ok=True)
    tmp = staging / name
    tmp.write_text(text, encoding="utf-8")
    try:
        plugin = _load_one(tmp)
        if plugin.code in METRIC_UNITS:
            raise PluginError(f"CODE「{plugin.code}」与内置指标重名，请改个码")
        others, _errors = load_plugins()
        clash = others.get(plugin.code)
        if (
            clash is not None
            and clash.path is not None
            and clash.path.resolve() != target.resolve()
        ):
            raise PluginError(
                f"CODE「{plugin.code}」已被 {clash.path.name} 占用，请改码或先删掉那个文件"
            )
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)

    plugins, _errors = load_plugins(force=True)
    return plugins.get(plugin.code, plugin)


def remove_plugin(code: str) -> str:
    """删掉插件的 ``.py`` 文件（彻底删除算法），返回被删文件名。"""
    plugins, _errors = load_plugins()
    plugin = plugins.get(code)
    if plugin is None or plugin.path is None:
        raise PluginError(f"没有找到指标码「{code}」对应的插件文件")
    name = plugin.path.name
    plugin.path.unlink(missing_ok=True)
    load_plugins(force=True)
    return name


def available_codes() -> dict[str, str]:
    """可用指标码 → 单位：内置三个 + 已加载插件。"""
    codes = dict(METRIC_UNITS)
    plugins, _errors = load_plugins()
    for code, plugin in plugins.items():
        codes.setdefault(code, plugin.unit)
    return codes


def unit_of(code: str) -> str:
    """指标码的单位；插件没写单位就空串（不抛错）。"""
    if code in METRIC_UNITS:
        return METRIC_UNITS[code]
    plugins, _errors = load_plugins()
    plugin = plugins.get(code)
    return plugin.unit if plugin else ""


def validate_code_known(code: str) -> None:
    """指标码必须有可算的算法（内置或插件），否则抛 :class:`PluginError`。"""
    if code in METRIC_UNITS:
        return
    plugins, _errors = load_plugins()
    if code not in plugins:
        raise PluginError(
            f"指标码「{code}」没有对应算法：既不是内置指标（{' / '.join(METRIC_UNITS)}），"
            f"插件目录 {plugin_dir()} 里也没有这个码"
        )


def compute_plugin_value(code: str, window: np.ndarray, sample_rate: float, params: dict) -> float:
    """调用插件算一个窗的值；异常与非有限结果都转成 :class:`PluginError`。"""
    plugins, _errors = load_plugins()
    plugin = plugins.get(code)
    if plugin is None:
        raise PluginError(f"指标码「{code}」没有对应的插件算法")
    try:
        value = plugin.compute(
            np.asarray(window, dtype="float64"), float(sample_rate), dict(params)
        )
    except Exception as exc:  # noqa: BLE001 - 插件代码不可信
        raise PluginError(
            f"插件 {plugin.path.name if plugin.path else code} 计算失败："
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PluginError(f"插件 {code} 返回值不是数字：{value!r}") from exc
    if not np.isfinite(result):
        raise PluginError(f"插件 {code} 返回非有限值（{result}）")
    return result
