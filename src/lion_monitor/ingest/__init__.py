"""数据接入层：现场采集文件/流的读取与校验。"""

from __future__ import annotations

from .vibration import (
    HEADER_SIZE,
    SUPPORTED_FORMAT_VERSIONS,
    VibrationFormatError,
    VibrationMeta,
    VibrationRecord,
    VibrationSlice,
    read_vibration,
    read_vibration_header,
    read_vibration_range,
    scan_vibration_dir,
)

__all__ = [
    "HEADER_SIZE",
    "SUPPORTED_FORMAT_VERSIONS",
    "VibrationFormatError",
    "VibrationMeta",
    "VibrationRecord",
    "VibrationSlice",
    "read_vibration",
    "read_vibration_header",
    "read_vibration_range",
    "scan_vibration_dir",
]
