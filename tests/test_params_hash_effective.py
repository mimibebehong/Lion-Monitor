"""P5-1（S6）：``params_hash`` 必须含 指标码 / 采样率 / **夹紧后**的生效带通。

原来指纹只哈希「用户填的参数字典」：``band_high=16000`` 与 ``30000`` 在 fs=32000 下
都被 ``_band_filter`` 夹到 15200（同一算法），指纹却不同；指纹里也没有 fs 与指标码，
审计「这个值按什么参数算的」会得出错误结论。

本文件锁住口径：

- 夹紧口径只有一处（``effective_params``），计算层与指纹层共用
- 指纹输入 = 指标码 + 采样率 + 夹紧后的生效参数
- 同输入跨进程 / 跨运行稳定
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lion_monitor.params import default_params, effective_params, params_hash

ROOT = Path(__file__).resolve().parents[1]

FS_32K = 32000.0
NYQUIST_SAFE_32K = 15200.0  # 32000 / 2 × 0.95


def test_effective_params_clamps_band_high_to_nyquist_safe_limit():
    """带通上限超过 fs/2×0.95 → 夹到该值（与计算层同一口径）。"""
    got = effective_params({"band_low": 10.0, "band_high": 30000.0}, FS_32K)
    assert got["band_high"] == NYQUIST_SAFE_32K
    assert got["band_low"] == 10.0


def test_effective_params_leaves_band_below_limit_untouched():
    """没超上限就原样保留（不夹紧参数不许被改）。"""
    got = effective_params({"band_low": 10.0, "band_high": 1000.0}, FS_32K)
    assert got["band_high"] == 1000.0


def test_effective_params_does_not_mutate_input():
    params = {"band_low": 10.0, "band_high": 30000.0}
    effective_params(params, FS_32K)
    assert params["band_high"] == 30000.0


def test_effective_params_without_sample_rate_returns_copy():
    """采样率未知（预览场景）时不做夹紧，但也要返回副本。"""
    params = {"band_low": 10.0, "band_high": 30000.0}
    got = effective_params(params, None)
    assert got == params and got is not params
    assert effective_params(None, FS_32K) == {}


def test_hash_same_when_raw_band_clamps_to_same_value():
    """验收 1：16000 与 30000 在 fs=32000 下夹紧结果相同 → 指纹相同。"""
    narrow = {"band_low": 10.0, "band_high": 16000.0}
    wide = {"band_low": 10.0, "band_high": 30000.0}
    assert params_hash(narrow, code="v_RMS", sample_rate=FS_32K) == params_hash(
        wide, code="v_RMS", sample_rate=FS_32K
    )


def test_hash_changes_with_sample_rate():
    """验收 2：同参数、不同采样率 → 指纹不同（16000 的可用带宽只有 7600）。"""
    params = default_params("v_RMS")
    assert params_hash(params, code="v_RMS", sample_rate=16000.0) != params_hash(
        params, code="v_RMS", sample_rate=FS_32K
    )


def test_hash_changes_with_metric_code():
    """验收 3：同参数、不同指标码 → 指纹不同。"""
    params = {"detrend": True}
    assert params_hash(params, code="a_Peak", sample_rate=FS_32K) != params_hash(
        params, code="a_RMS", sample_rate=FS_32K
    )


def test_hash_stable_across_key_order_and_calls():
    params = {"band_low": 10.0, "band_high": 30000.0, "window_fn": "hanning"}
    reordered = {"window_fn": "hanning", "band_high": 30000.0, "band_low": 10.0}
    first = params_hash(params, code="v_RMS", sample_rate=FS_32K)
    assert first == params_hash(reordered, code="v_RMS", sample_rate=FS_32K)
    assert first == params_hash(params, code="v_RMS", sample_rate=FS_32K)


def test_hash_stable_across_processes():
    """跨进程一致：换一个解释器算同一输入，指纹必须一模一样。"""
    expected = params_hash({"band_low": 10.0, "band_high": 30000.0}, "v_RMS", FS_32K)
    # 便携版 python 的 ._pth 会忽略 PYTHONPATH，子进程里显式补 src 路径。
    script = (
        "import sys;"
        "sys.path.insert(0, " + repr(str(ROOT / "src")) + ");"
        "from lion_monitor.params import params_hash;"
        "print(params_hash({'band_high': 30000.0, 'band_low': 10.0},"
        " 'v_RMS', 32000.0))"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    assert done.stdout.strip() == expected


def test_hash_empty_params_still_blank():
    """空参数仍返回空串（历史行与新行可区分），不受新增维度影响。"""
    assert params_hash(None, code="v_RMS", sample_rate=FS_32K) == ""
    assert params_hash({}, code="v_RMS", sample_rate=FS_32K) == ""
