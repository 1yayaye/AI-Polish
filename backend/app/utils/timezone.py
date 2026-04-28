from datetime import datetime, time, timedelta, timezone
from typing import Optional


CHINA_TZ = timezone(timedelta(hours=8))


def now_china() -> datetime:
    return datetime.now(CHINA_TZ)


def utc_naive_from_china(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def china_day_start_utc_naive(value: Optional[datetime] = None) -> datetime:
    current = value or now_china()
    day_start = datetime.combine(current.date(), time.min, tzinfo=CHINA_TZ)
    return utc_naive_from_china(day_start)


def china_days_ago_utc_naive(days: int, value: Optional[datetime] = None) -> datetime:
    current = value or now_china()
    target = current - timedelta(days=days)
    return utc_naive_from_china(target)
