"""测试夹具：按实测的 512 字节头部格式构造 .dat 文件。"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

HEADER_SIZE = 512


def build_header(
    *,
    channel: str = "CH3",
    sample_rate_hz: float = 32000.0,
    sample_count: int = 0,
    started_at: str = "20261010145151",
    site: str = "Farm",
    device_ip: str = "10.10.10.165",
    rule: str = "Rule1.d",
    version: int = 20,
    scale: float = 1.0,
) -> bytes:
    buf = bytearray(HEADER_SIZE)
    struct.pack_into("<H", buf, 0x00, version)
    struct.pack_into("<H", buf, 0x02, 64)
    struct.pack_into("<I", buf, 0x04, 131072)
    struct.pack_into("<f", buf, 0x08, scale)
    struct.pack_into("<I", buf, 0x40, 12)

    for slot, text in enumerate((rule, site, device_ip, channel), start=0):
        raw = text.encode("ascii")
        assert len(raw) < 16, f"字符串槽位最长 15 字节: {text}"
        buf[0x50 + slot * 16 : 0x50 + slot * 16 + len(raw)] = raw

    started = started_at.encode("ascii")
    buf[0xD0 : 0xD0 + len(started)] = started

    struct.pack_into("<I", buf, 0x0F0, sample_count)
    struct.pack_into("<f", buf, 0x110, sample_rate_hz)
    return bytes(buf)


def write_dat(
    path: Path,
    *,
    samples: np.ndarray | None = None,
    **header_kwargs: object,
) -> Path:
    """写一个 .dat 文件；samples 为 None 时只写头部。"""
    if samples is None:
        body = b""
        count = int(header_kwargs.pop("sample_count", 0))
    else:
        arr = np.asarray(samples, dtype="<f4")
        body = arr.tobytes()
        count = arr.size
    count = int(header_kwargs.pop("sample_count", count))
    header = build_header(sample_count=count, **header_kwargs)  # type: ignore[arg-type]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + body)
    return path
