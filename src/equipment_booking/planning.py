"""区间重叠、ISO 周切分与配额统计的纯函数。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from .clock import week_start


def canonical_json(payload: Any) -> str:
    """审计哈希使用的规范 JSON：键排序、无空白、非 ASCII 保留。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """半开区间 ``[start, end)`` 重叠判定；首尾相接（a.end == b.start）不算冲突。"""
    return start_a < end_b and start_b < end_a


def intersection_minutes(
    start: datetime,
    end: datetime,
    window_start: datetime,
    window_end: datetime,
) -> int:
    """预约区间与统计窗口的重合分钟数（两端截断到窗口内）。"""
    clipped_start = max(start, window_start)
    clipped_end = min(end, window_end)
    if clipped_end <= clipped_start:
        return 0
    seconds = (clipped_end - clipped_start).total_seconds()
    return int(round(seconds / 60))


def spanned_weeks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """预约覆盖到的全部 UTC ISO 周窗口 ``[周一 00:00, 下周一 00:00)``。"""
    first = week_start(start)
    last = week_start(end - timedelta(microseconds=1))
    weeks: list[tuple[datetime, datetime]] = []
    cursor = first
    while cursor <= last:
        weeks.append((cursor, cursor + timedelta(days=7)))
        cursor += timedelta(days=7)
    return weeks
