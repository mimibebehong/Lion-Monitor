"""离线把 .venv-phm 中的依赖播种到 Lion-Monitor 的 venv（同 Python 3.11.15，cp311 ABI 兼容）。

用法：python seed_venv.py <src_site_packages> <dst_site_packages>
只用硬链接（cp -al 等价），失败时回退为复制。
"""

from __future__ import annotations

import importlib.metadata as md
import shutil
import sys
from pathlib import Path

ROOTS = ["numpy", "pandas", "pydantic", "pyyaml", "pytest", "matplotlib", "scipy"]


def norm(name: str) -> str:
    """PEP 503 归一化：连字符/下划线/点 等价。"""
    for ch in ("_", "."):
        name = name.replace(ch, "-")
    return name.strip().lower()


def closure(roots: list[str]) -> dict[str, md.Distribution]:
    dists = {norm(d.metadata["Name"]): d for d in md.distributions()}
    need: dict[str, md.Distribution] = {}
    stack = [norm(r) for r in roots]
    while stack:
        name = stack.pop()
        if name in need or name not in dists:
            continue
        d = dists[name]
        need[name] = d
        for req in d.requires or []:
            if "extra ==" in req:
                continue
            dep = req.split(";")[0].split("[")[0]
            for sep in (">", "=", "<", "!", "~", " "):
                dep = dep.split(sep)[0]
            dep = norm(dep)
            if dep:
                stack.append(dep)
    return need


def main() -> int:
    src = Path(sys.argv[1]).resolve()
    dst = Path(sys.argv[2]).resolve()
    assert src.is_dir() and dst.is_dir(), "需要两个已存在的 site-packages 目录"

    # 以 src 的 site-packages 为元数据库解析依赖闭包
    sys.path.insert(0, str(src))
    need = closure(ROOTS)

    linked, copied, missing = 0, 0, []
    for name, dist in sorted(need.items()):
        files = [f for f in (dist.files or [])]
        if not files:
            missing.append(f"{name}(无文件清单)")
            continue
        for rel in files:
            s = src / rel
            if not s.exists():
                missing.append(f"{name}:{rel}")
                continue
            d = dst / rel
            if d.exists() or d.is_symlink():
                continue
            d.parent.mkdir(parents=True, exist_ok=True)
            try:
                d.hardlink_to(s)
                linked += 1
            except OSError:
                try:
                    if s.is_dir():
                        shutil.copytree(s, d, dirs_exist_ok=True)
                    else:
                        shutil.copy2(s, d)
                    copied += 1
                except OSError as exc:
                    missing.append(f"{name}:{rel}({exc})")

    print(f"包数={len(need)} 硬链接={linked} 复制={copied} 缺失={len(missing)}")
    for m in missing[:10]:
        print("  缺失", m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
