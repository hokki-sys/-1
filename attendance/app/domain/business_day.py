"""영업일 계산 (설계 D3).

식당은 자정을 넘겨 닫습니다. 새벽 1시 퇴근은 달력상 오늘이지만 근무는 어제 것입니다.
컷오프 시각(기본 05:00) 이전은 전날 영업일로 봅니다.

모든 함수는 매장 시간대로 변환된 aware datetime 을 받습니다.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

DEFAULT_CUTOFF_HOUR = 5


def business_date(dt: datetime, cutoff_hour: int = DEFAULT_CUTOFF_HOUR) -> date:
    """`dt` 가 속한 영업일.

    >>> business_date(datetime(2026, 8, 26, 2, 0))   # 새벽 2시
    datetime.date(2026, 8, 25)                       # -> 전날 영업일
    """
    _check_cutoff(cutoff_hour)
    return (dt - timedelta(hours=cutoff_hour)).date()


def business_day_bounds(
    bday: date, cutoff_hour: int = DEFAULT_CUTOFF_HOUR
) -> tuple[datetime, datetime]:
    """영업일의 시작/끝 naive datetime. 끝은 배타적(exclusive)입니다."""
    _check_cutoff(cutoff_hour)
    start = datetime.combine(bday, time(hour=cutoff_hour))
    return start, start + timedelta(days=1)


def week_start(d: date) -> date:
    """그 날짜가 속한 주의 월요일."""
    return d - timedelta(days=d.weekday())


def week_range(d: date) -> tuple[date, date]:
    """(월요일, 일요일) — 양끝 포함."""
    mon = week_start(d)
    return mon, mon + timedelta(days=6)


def month_range(year: int, month: int) -> tuple[date, date]:
    """(1일, 말일) — 양끝 포함."""
    first = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first, nxt - timedelta(days=1)


def _check_cutoff(cutoff_hour: int) -> None:
    if not 0 <= cutoff_hour <= 12:
        raise ValueError(f"영업일 컷오프는 0~12시 사이여야 합니다: {cutoff_hour}")
