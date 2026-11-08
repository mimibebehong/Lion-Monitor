"""示例：自定义指标插件（峭度）。

放到 ~/.lion_monitor/metrics_plugins/ 下，再在「指标配置」页点「重新扫描插件」，
指标码 kurtosis 就会出现在「新增指标」的指标码下拉里。
"""

CODE = "kurtosis"
UNIT = ""
TITLE = "峭度"
DESCRIPTION = "四阶矩，对冲击/点蚀敏感（正态 ≈ 3，纯正弦 ≈ 1.5）"
PARAMS_SCHEMA = [
    {"key": "window_s", "label": "计算时长（s）", "kind": "number", "min": 0.05, "step": 0.5},
    {"key": "step_s", "label": "计算步长（s）", "kind": "number", "min": 0.05, "step": 0.5},
]


def compute(window, sample_rate, params):
    import numpy as np

    x = np.asarray(window, dtype="float64")
    sigma = float(x.std())
    if sigma <= 0:
        return 0.0
    return float(np.mean((x - x.mean()) ** 4) / sigma**4)
