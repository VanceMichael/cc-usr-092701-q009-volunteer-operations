"""时间工具：统一使用带时区的 ISO8601 字符串。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def overlap_minutes(start_a: str, end_a: str, start_b: str, end_b: str) -> float:
    """两个时间段重叠的分钟数；不重叠（含端点相接）返回 0。"""
    lo = max(parse(start_a), parse(start_b))
    hi = min(parse(end_a), parse(end_b))
    return max(0.0, (hi - lo).total_seconds() / 60.0)


def gap_hours(prev_end: str, next_start: str) -> float:
    """前一段结束到下一段开始之间的小时间隔。"""
    return (parse(next_start) - parse(prev_end)).total_seconds() / 3600.0


def duration_minutes(start: str, end: str) -> int:
    return int((parse(end) - parse(start)).total_seconds() // 60)


def add_hours(value: str, hours: float) -> str:
    return iso(parse(value) + timedelta(hours=hours))


def shift_label(start: str, end: str) -> str:
    """按班次自身时区显示（如 +08:00），避免换算成 UTC 后凌晨显示。"""
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end)
    if s.tzinfo is None:
        s = s.replace(tzinfo=timezone.utc)
    if e.tzinfo is None:
        e = e.replace(tzinfo=timezone.utc)
    return f"{s.strftime('%m-%d %H:%M')}~{e.strftime('%H:%M')}"
