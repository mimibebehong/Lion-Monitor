"""安装后铺演示数据：把 ``demo_data\\`` 拷进 ``data\\``，再把库里的路径改到安装目录。

为什么不直接用 Inno 的 [Files] 把数据装进 ``data\\``：
Inno 会记住「这个文件是我装的」，卸载时连库一起删掉——用户导入的数据就没了。
改成安装器只带 ``demo_data\\``（不带 ``data\\``），装完由这个脚本**新建**文件，
卸载器不认识它们，于是 ``data\\`` 自然保留（文档承诺的「重装后数据还在」才成立）。

顺带把 ``dataset.source_dir`` / ``channel.source_path`` 里的绝对路径改到
``data\\四通道保存``，这样装到任何盘、任何目录都能打开原始波形。

用法（安装器 [Run] 第一步）：

    runtime\\python.exe tools\\seed_demo.py --root D:\\LionMonitor
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def _copy_demo_files(root: Path) -> list[str]:
    """只补缺失的东西：``data\\`` 里已有库/数据就不动（升级安装不能覆盖用户数据）。"""
    done: list[str] = []
    demo = root / "demo_data"
    data = root / "data"
    if not demo.is_dir():
        return done
    data.mkdir(parents=True, exist_ok=True)

    db_src = demo / "lion.db"
    db_dst = data / "lion.db"
    if db_src.is_file() and not db_dst.exists():
        shutil.copy2(db_src, db_dst)
        done.append("lion.db")

    wave_src = demo / "四通道保存"
    wave_dst = data / "四通道保存"
    if wave_src.is_dir() and not wave_dst.exists():
        shutil.copytree(wave_src, wave_dst)
        count = sum(1 for item in wave_dst.rglob("*") if item.is_file())
        done.append(f"四通道保存（{count} 个文件）")
    return done


def _relocate(root: Path) -> int:
    """复用 tools\\relocate_demo.py 的重写逻辑（同一个 python 直接跑子进程）。"""
    script = Path(__file__).resolve().parent / "relocate_demo.py"
    if not script.is_file():
        return 0
    done = subprocess.run(  # noqa: S603 - 固定脚本
        [sys.executable, str(script), str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if done.stdout.strip():
        print(done.stdout.strip())
    if done.returncode != 0 and done.stderr.strip():
        print(done.stderr.strip()[-400:])
    return done.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="铺演示数据并修正库里的绝对路径")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    copied = _copy_demo_files(root)
    if copied:
        print("演示数据已就位：" + "、".join(copied))
    else:
        print("data\\ 已有数据，跳过复制（升级安装不覆盖）")
    return _relocate(root)


if __name__ == "__main__":
    raise SystemExit(main())
