"""预约域错误，可由 HTTP 层映射为确定的状态码。"""

from __future__ import annotations


class SchedulingError(Exception):
    """预约域所有错误的基类。"""


class Conflict(SchedulingError):
    """设备时间区间与已有预约重叠。"""


class InvalidState(SchedulingError):
    """预约当前状态不允许该操作（如开始后被普通用户改写）。"""


class QuotaExceeded(SchedulingError):
    """团队周配额不足。"""
