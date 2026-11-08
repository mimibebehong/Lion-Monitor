"""README 守卫：公开展示用的项目介绍要完整、可操作，且不含内部/个人信息。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _section(title: str) -> str:
    """取某个二级标题下的正文（到下一个二级标题为止）。"""
    marker = f"\n## {title}\n"
    assert marker in README, f"README 缺少「{title}」章节"
    body = README.split(marker, 1)[1]
    return body.split("\n## ", 1)[0]


def test_readme_has_overview_and_screenshots():
    body = _section("界面预览")
    for image in ("dashboard.png", "trends.png", "analysis-waveform.png", "analysis-fft.png"):
        assert f"screenshots/{image}" in body, f"README 界面预览缺少 {image}"
        assert (ROOT / "screenshots" / image).is_file(), f"截图文件不存在：{image}"


def test_readme_documents_features_and_scope():
    body = _section("功能一览")
    for token in ("健康树", "指标计算", "阈值", "趋势", "数据分析", "备份"):
        assert token in body, f"功能一览缺少：{token}"

    assert "离线" in README
    assert "实时采集" in README, "需要说明实时采集不在当前版本范围内"


def test_readme_has_runnable_quick_start():
    body = _section("快速开始")
    assert "python run.py run app\\main.py" in body
    assert "requirements-win.txt" in body

    testing = _section("快速开始")
    assert "### 测试与静态检查" in testing
    assert "python -m pytest tests" in testing
    assert "python -m ruff check" in testing


def test_readme_documents_data_format_and_logging():
    data = _section("数据格式")
    for token in (".dat", "512 B", "float32", "v_RMS"):
        assert token in data, f"数据格式缺少：{token}"

    features = _section("功能一览")
    assert "warn" in features and "alert" in features, "功能一览缺少阈值级别"

    logs = _section("日志")
    assert "lion-monitor.log" in logs


def test_readme_has_no_internal_docs_or_personal_references():
    personal_name = "\u9a6c\u54e5"  # 以转义形式保存，避免测试文件本身出现姓名
    for stale in (personal_name, "待确认", "未完成", "TODO", "剩余工作", "docs/", "reports/"):
        assert stale not in README, f"README 仍残留内部/个人信息：{stale}"
