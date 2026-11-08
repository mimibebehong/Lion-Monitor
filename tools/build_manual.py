"""把 docs\\用户使用手册.md 生成 PDF（顺带留一份带样式的 HTML）。

为什么单独一个脚本：手册是给现场看的，改完 md 必须能一条命令重出 PDF，
不能靠人记一堆 Edge 参数。

用法（在仓库根目录）：

    dist\\runtime\\python.exe tools\\build_manual.py            # 出 PDF
    dist\\runtime\\python.exe tools\\build_manual.py --html-only  # 只出 HTML

依赖：``markdown``（仅构建机器需要，不装进发行包）：

    dist\\runtime\\python.exe -m pip install markdown

PDF 由 Windows 自带 Edge 的 headless 打印生成，不引入额外依赖。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "用户使用手册.md"
OUT_DIR = ROOT / ".build"
PDF = ROOT / "docs" / "manual" / "用户使用手册.pdf"

CSS = """
@page { size: A4; margin: 18mm 15mm 16mm 15mm; }
* { box-sizing: border-box; }
body {
  font-family: "Microsoft YaHei", "微软雅黑", "Segoe UI", sans-serif;
  font-size: 10.5pt; line-height: 1.75; color: #1c2530; margin: 0;
}
h1 { font-size: 22pt; color: #0b3d62; border-bottom: 3px solid #2f8fd6;
     padding-bottom: 8px; margin: 0 0 6px; }
h2 { font-size: 15pt; color: #0b3d62; margin: 22px 0 8px;
     border-left: 5px solid #2f8fd6; padding-left: 9px; page-break-after: avoid; }
h3 { font-size: 12.5pt; color: #12496f; margin: 16px 0 6px; page-break-after: avoid; }
p, li { orphans: 3; widows: 3; }
a { color: #1c6fb3; text-decoration: none; }
code { font-family: Consolas, "Cascadia Mono", monospace; font-size: 9.5pt;
       background: #eef4f9; border: 1px solid #dbe6ef; border-radius: 3px;
       padding: 1px 4px; color: #0d3f61; }
pre { background: #f5f8fb; border: 1px solid #dbe6ef; border-left: 4px solid #2f8fd6;
      border-radius: 4px; padding: 9px 11px; overflow: auto; page-break-inside: avoid; }
pre code { background: none; border: none; padding: 0; font-size: 9pt; }
table { width: 100%; border-collapse: collapse; margin: 10px 0 14px;
        font-size: 9.8pt; page-break-inside: avoid; }
th { background: #e8f2fa; color: #0b3d62; font-weight: 600; text-align: left; }
th, td { border: 1px solid #cfdde9; padding: 5px 8px; vertical-align: top; }
tbody tr:nth-child(even) { background: #fafcfe; }
blockquote { margin: 10px 0; padding: 7px 12px; background: #f4f9fd;
             border-left: 4px solid #7cb8de; color: #33506a; }
hr { border: none; border-top: 1px solid #dbe6ef; margin: 18px 0; }
ul, ol { padding-left: 22px; }
li { margin: 2px 0; }
"""

EDGE_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


def render_html() -> Path:
    try:
        import markdown
    except ModuleNotFoundError:
        sys.exit(
            "缺少 markdown 模块。先装一次（不进发行包）：\n"
            "  dist\\runtime\\python.exe -m pip install markdown"
        )
    body = markdown.markdown(
        SOURCE.read_text(encoding="utf-8"),
        extensions=["tables", "toc", "fenced_code", "sane_lists", "def_list"],
        extension_configs={"toc": {"title": "", "toc_depth": "2-2"}},
    )
    html = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>Lion-Monitor 用户使用手册 v0.1.0</title>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / "manual.html"
    target.write_text(html, encoding="utf-8")
    return target


def render_pdf(html: Path) -> Path:
    edge = next((p for p in EDGE_CANDIDATES if p.exists()), None) or shutil.which("msedge")
    if not edge:
        sys.exit("找不到 Microsoft Edge，无法打印 PDF；可先用 --html-only 只出 HTML。")
    PDF.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 - 固定参数，路径来自本仓库
        [
            str(edge),
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={PDF}",
            html.resolve().as_uri(),
        ],
        check=True,
    )
    return PDF


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 Lion-Monitor 用户使用手册 PDF")
    parser.add_argument("--html-only", action="store_true", help="只生成 HTML，不打印 PDF")
    args = parser.parse_args(argv)

    if not SOURCE.is_file():
        sys.exit(f"缺少手册源文件：{SOURCE}")
    html = render_html()
    print(f"HTML：{html}（{html.stat().st_size // 1024} KB）")
    if args.html_only:
        return 0
    pdf = render_pdf(html)
    print(f"PDF ：{pdf}（{pdf.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
