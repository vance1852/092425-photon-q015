"""可注入时间源与 UTC 工具。预约域所有时间均使用 UTC。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class FrozenClock:
    current: datetime

    def now(self) -> datetime:
        if self.current.tzinfo is None:
            raise ValueError("冻结时钟必须带时区")
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


def parse_utc(value: str) -> datetime:
    """解析 ISO 8601 时间；必须显式携带时区并归一化为 UTC。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("时间必须是 ISO 8601 字符串")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析时间: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("时间必须显式携带时区（Z 或 +00:00），系统只接受 UTC")
    return parsed.astimezone(timezone.utc)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def week_start(value: datetime) -> datetime:
    """返回包含 value 的 ISO 周（UTC）周一 00:00。"""
    moment = value.astimezone(timezone.utc)
    monday = moment - timedelta(days=moment.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


_ISO_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")


def parse_week(value: str) -> datetime:
    """接受 'YYYY-Www'、UTC 日期或 ISO 时间，返回该周周一 00:00 UTC。"""
    text = value.strip()
    match = _ISO_WEEK.match(text)
    if match:
        year, week_number = int(match.group(1)), int(match.group(2))
        return datetime.fromisocalendar(year, week_number, 1).replace(tzinfo=timezone.utc)
    if len(text) == 10:
        parsed = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    else:
        parsed = parse_utc(text)
    return week_start(parsed)
