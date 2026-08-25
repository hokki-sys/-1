"""근무표 편집기의 계산과 검증 (설계 D8).

편집기가 저장하기 전에 사장님에게 보여줄 것들입니다. 경고는 **막지 않고 알리기만**
합니다 — 인원 공백이나 시간 경계는 알고도 그렇게 짜는 경우가 많고, 저장을 막으면
시스템을 우회하게 됩니다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum

_HHMM_RE = re.compile(r"^(\d{1,2})\s*[:.]\s*(\d{1,2})$")
_RANGE_SEP = re.compile(r"\s*[-–~—]\s*")


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"


@dataclass(frozen=True)
class ScheduleEntry:
    """근무표 한 칸. 하루에 여러 건 들어갈 수 있습니다.

    데스크톱 버전은 UNIQUE(직원, 날짜) 라서 오전 홀 + 저녁 주방처럼 하루 두 번
    잡히면 앞 스케줄이 조용히 덮어써졌습니다. 여기서는 그냥 두 건입니다.
    """

    employee_id: int
    employee_name: str
    work_date: date
    start: time
    end: time
    department: str = ""

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start

    @property
    def start_dt(self) -> datetime:
        return datetime.combine(self.work_date, self.start)

    @property
    def end_dt(self) -> datetime:
        base = datetime.combine(self.work_date, self.end)
        return base + timedelta(days=1) if self.crosses_midnight else base

    @property
    def minutes(self) -> float:
        return (self.end_dt - self.start_dt).total_seconds() / 60

    @property
    def label(self) -> str:
        """격자 칸에 그대로 넣고 다시 읽을 수 있는 표기. 하이픈은 ASCII 여야
        parse_range 가 되돌려 읽습니다."""
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


@dataclass
class ScheduleWarning:
    severity: Severity
    message: str
    employee_id: int | None = None
    work_date: date | None = None


@dataclass(frozen=True)
class HourThresholds:
    """주간 근무시간 기준선.

    기본값은 주휴수당(15시간)과 법정 근로시간(40시간) 경계입니다. 사업장 규모에
    따라 적용 규정이 달라지므로 **고정값으로 박지 않고** 매장 설정으로 둡니다.
    """

    weekly_short_hours: float = 15.0
    weekly_standard_hours: float = 40.0
    near_margin_minutes: float = 60.0  # 경계에서 이 정도 안쪽이면 알려줍니다


@dataclass
class EmployeeWeek:
    employee_id: int
    employee_name: str
    department: str
    by_date: dict[date, list[ScheduleEntry]] = field(default_factory=dict)

    @property
    def total_minutes(self) -> float:
        return sum(e.minutes for day in self.by_date.values() for e in day)

    @property
    def days_worked(self) -> int:
        return sum(1 for day in self.by_date.values() if day)


def parse_time(value: str) -> time:
    """'10', '10:00', '1000', '9:30' 을 받아줍니다. 사람이 치는 값이라 관대하게."""
    s = (value or "").strip()
    if not s:
        raise ValueError("시간이 비어 있습니다")
    m = _HHMM_RE.match(s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    elif s.isdigit() and len(s) <= 2:
        hour, minute = int(s), 0
    elif s.isdigit() and len(s) in (3, 4):
        hour, minute = int(s[:-2]), int(s[-2:])
    else:
        raise ValueError(f"시간 형식을 알 수 없습니다: {value!r}")
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        raise ValueError(f"시간 범위를 벗어났습니다: {value!r}")
    return time(hour % 24, minute)


def parse_range(value: str) -> tuple[time, time]:
    """'10-22', '10:00~22:00', '17–22' → (time, time)"""
    parts = [p for p in _RANGE_SEP.split((value or "").strip()) if p]
    if len(parts) != 2:
        raise ValueError(f"근무 시간 범위를 알 수 없습니다: {value!r}")
    return parse_time(parts[0]), parse_time(parts[1])


def parse_cell(value: str) -> list[tuple[time, time]]:
    """한 칸에 여러 근무가 들어올 수 있습니다: "10-14 17-22".

    오전 홀 + 저녁 주방처럼 하루 두 번 잡히는 경우인데, 데스크톱 버전은
    이걸 표현할 방법 자체가 없었습니다 (UNIQUE(직원, 날짜)).
    """
    text = (value or "").strip()
    if not text:
        return []
    chunks = [c for c in re.split(r"[\s,/]+", text) if c]
    return [parse_range(c) for c in chunks]


def build_week(entries: list[ScheduleEntry], monday: date) -> list[EmployeeWeek]:
    """편집기 격자에 그대로 얹을 수 있는 형태로 묶습니다."""
    days = [monday + timedelta(days=i) for i in range(7)]
    rows: dict[int, EmployeeWeek] = {}
    for e in entries:
        row = rows.get(e.employee_id)
        if row is None:
            row = rows[e.employee_id] = EmployeeWeek(
                employee_id=e.employee_id,
                employee_name=e.employee_name,
                department=e.department,
                by_date={d: [] for d in days},
            )
        row.by_date.setdefault(e.work_date, []).append(e)
    for row in rows.values():
        for day in row.by_date.values():
            day.sort(key=lambda x: x.start)
    return sorted(rows.values(), key=lambda r: (r.department, r.employee_name))


def validate_week(
    entries: list[ScheduleEntry],
    monday: date,
    thresholds: HourThresholds | None = None,
    open_time: time | None = None,
    close_time: time | None = None,
    departments_requiring_cover: tuple[str, ...] = (),
) -> list[ScheduleWarning]:
    """저장을 막지 않는 경고 목록."""
    th = thresholds or HourThresholds()
    out: list[ScheduleWarning] = []
    out += _overlaps(entries)
    out += _hour_thresholds(entries, monday, th)
    if open_time and close_time:
        out += _coverage_gaps(
            entries, monday, open_time, close_time, departments_requiring_cover
        )
    return out


def _overlaps(entries: list[ScheduleEntry]) -> list[ScheduleWarning]:
    out: list[ScheduleWarning] = []
    by_emp: dict[int, list[ScheduleEntry]] = {}
    for e in entries:
        by_emp.setdefault(e.employee_id, []).append(e)
    for items in by_emp.values():
        items = sorted(items, key=lambda x: x.start_dt)
        for a, b in zip(items, items[1:]):
            if b.start_dt < a.end_dt:
                out.append(
                    ScheduleWarning(
                        Severity.WARNING,
                        f"{a.employee_name} {a.work_date:%m/%d} {a.label} 과 "
                        f"{b.work_date:%m/%d} {b.label} 의 시간이 겹칩니다",
                        employee_id=a.employee_id,
                        work_date=a.work_date,
                    )
                )
    return out


def _hour_thresholds(
    entries: list[ScheduleEntry], monday: date, th: HourThresholds
) -> list[ScheduleWarning]:
    out: list[ScheduleWarning] = []
    for row in build_week(entries, monday):
        hours = row.total_minutes / 60
        if hours > th.weekly_standard_hours:
            out.append(
                ScheduleWarning(
                    Severity.WARNING,
                    f"{row.employee_name} 주 {hours:g}시간 — "
                    f"{th.weekly_standard_hours:g}시간을 넘습니다",
                    employee_id=row.employee_id,
                )
            )
        elif abs(row.total_minutes - th.weekly_short_hours * 60) <= th.near_margin_minutes:
            out.append(
                ScheduleWarning(
                    Severity.INFO,
                    f"{row.employee_name} 주 {hours:g}시간 — "
                    f"{th.weekly_short_hours:g}시간 경계에 있습니다",
                    employee_id=row.employee_id,
                )
            )
    return out


def _coverage_gaps(
    entries: list[ScheduleEntry],
    monday: date,
    open_time: time,
    close_time: time,
    departments: tuple[str, ...],
) -> list[ScheduleWarning]:
    """영업시간 중 배정된 사람이 아무도 없는 30분 구간을 찾습니다."""
    out: list[ScheduleWarning] = []
    step = timedelta(minutes=30)
    for i in range(7):
        day = monday + timedelta(days=i)
        for dept in departments or ("",):
            todays = [
                e
                for e in entries
                if e.work_date == day and (not dept or e.department == dept)
            ]
            if not todays:
                continue  # 그 날 그 부서가 통째로 쉬는 건 경고 대상이 아닙니다
            cursor = datetime.combine(day, open_time)
            end = datetime.combine(day, close_time)
            if end <= cursor:
                end += timedelta(days=1)
            gap_start = None
            while cursor < end:
                covered = any(e.start_dt <= cursor < e.end_dt for e in todays)
                if not covered and gap_start is None:
                    gap_start = cursor
                elif covered and gap_start is not None:
                    out.append(_gap_warning(day, dept, gap_start, cursor))
                    gap_start = None
                cursor += step
            if gap_start is not None:
                out.append(_gap_warning(day, dept, gap_start, end))
    return out


def _gap_warning(
    day: date, dept: str, start: datetime, end: datetime
) -> ScheduleWarning:
    where = f"{dept} " if dept else ""
    return ScheduleWarning(
        Severity.WARNING,
        f"{day:%m/%d} {start:%H:%M}–{end:%H:%M} {where}배정된 사람이 없습니다",
        work_date=day,
    )
