"""근무표 편집기 (설계 D8).

핵심 규칙 하나: **발행(published)된 근무표만 지각 판정의 기준이 됩니다.**
짜다 만 초안이 판정에 새면 안 됩니다 — 데스크톱 버전은 OCR 을 적용하는 순간
바로 반영돼서 이 중간 상태가 아예 없었습니다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..domain.business_day import week_start
from ..domain.schedule import ScheduleEntry, ScheduleWarning, validate_week
from ..models.store import (
    AuditLog, Employee, ScheduleEntryRow, ScheduleRevision, ScheduleWeek, ShiftPreset,
)
from ..tenancy import StoreContext

STATUS_DRAFT, STATUS_PUBLISHED = "draft", "published"


def get_week(db: Session, monday: date, create: bool = False) -> ScheduleWeek | None:
    monday = week_start(monday)
    week = db.execute(
        select(ScheduleWeek).where(ScheduleWeek.week_start == monday)
    ).scalar_one_or_none()
    if week is None and create:
        week = ScheduleWeek(week_start=monday, status=STATUS_DRAFT)
        db.add(week)
        db.flush()
    return week


def load_entries(db: Session, monday: date) -> list[ScheduleEntry]:
    """도메인 객체로 올려서 합계·검증에 바로 쓸 수 있게 합니다."""
    monday = week_start(monday)
    rows = db.execute(
        select(ScheduleEntryRow, Employee)
        .join(Employee, Employee.id == ScheduleEntryRow.employee_id)
        .join(ScheduleWeek, ScheduleWeek.id == ScheduleEntryRow.week_id)
        .where(ScheduleWeek.week_start == monday)
        .order_by(ScheduleEntryRow.work_date, ScheduleEntryRow.start_time)
    ).all()
    return [
        ScheduleEntry(
            employee_id=r.employee_id,
            employee_name=e.name,
            work_date=r.work_date,
            start=r.start_time,
            end=r.end_time,
            department=r.department or e.department,
        )
        for r, e in rows
    ]


def save_entries(
    db: Session,
    ctx: StoreContext,
    monday: date,
    entries: list[ScheduleEntry],
    actor: str,
) -> ScheduleWeek:
    """주차 전체를 통째로 교체합니다. 이미 발행된 주차면 변경 이력을 남깁니다."""
    monday = week_start(monday)
    week = get_week(db, monday, create=True)
    before = _snapshot(load_entries(db, monday))

    db.execute(delete(ScheduleEntryRow).where(ScheduleEntryRow.week_id == week.id))
    db.flush()
    for e in entries:
        db.add(
            ScheduleEntryRow(
                week_id=week.id,
                employee_id=e.employee_id,
                work_date=e.work_date,
                start_time=e.start,
                end_time=e.end,
                department=e.department,
            )
        )
    db.flush()

    if week.status == STATUS_PUBLISHED:
        after = _snapshot(entries)
        diff = _diff(before, after)
        if diff:
            db.add(
                ScheduleRevision(
                    week_id=week.id,
                    actor=actor,
                    summary=f"{len(diff)}건 변경",
                    diff={"changes": diff},
                )
            )
            _audit(db, actor, "SCHEDULE_REVISED",
                   f"{monday} 주차 발행본 {len(diff)}건 변경")
    return week


def publish(db: Session, monday: date, actor: str) -> ScheduleWeek:
    week = get_week(db, monday, create=True)
    week.status = STATUS_PUBLISHED
    week.published_at = datetime.now(tz=None).astimezone()
    week.published_by = actor
    db.flush()
    _audit(db, actor, "SCHEDULE_PUBLISHED", f"{week.week_start} 주차 발행")
    return week


def unpublish(db: Session, monday: date, actor: str) -> ScheduleWeek | None:
    week = get_week(db, monday)
    if week is None:
        return None
    week.status = STATUS_DRAFT
    db.flush()
    _audit(db, actor, "SCHEDULE_UNPUBLISHED", f"{week.week_start} 주차 초안으로 되돌림")
    return week


def copy_previous_week(db: Session, monday: date) -> list[ScheduleEntry]:
    """지난주 복사 — 편집기에서 가장 많이 쓰게 될 기능입니다.

    식당 근무표는 주마다 거의 같아서, 복사 후 두세 칸 고치는 게 작업의 전부입니다.
    """
    monday = week_start(monday)
    prev = load_entries(db, monday - timedelta(days=7))
    return [
        ScheduleEntry(
            employee_id=e.employee_id,
            employee_name=e.employee_name,
            work_date=e.work_date + timedelta(days=7),
            start=e.start,
            end=e.end,
            department=e.department,
        )
        for e in prev
    ]


def warnings_for(
    db: Session, ctx: StoreContext, monday: date, entries: list[ScheduleEntry]
) -> list[ScheduleWarning]:
    open_t, close_t = ctx.open_close()
    return validate_week(
        entries,
        week_start(monday),
        thresholds=ctx.thresholds(),
        open_time=open_t,
        close_time=close_t,
        departments_requiring_cover=ctx.departments(),
    )


def expected_times(
    db: Session, ctx: StoreContext, business_dates: list[date]
) -> dict[tuple[int, date], tuple[datetime, datetime]]:
    """(직원, 날짜) -> (예정 출근, 예정 퇴근). **발행된 주차만** 돌려줍니다."""
    if not business_dates:
        return {}
    rows = db.execute(
        select(ScheduleEntryRow)
        .join(ScheduleWeek, ScheduleWeek.id == ScheduleEntryRow.week_id)
        .where(
            ScheduleWeek.status == STATUS_PUBLISHED,
            ScheduleEntryRow.work_date.in_(business_dates),
        )
        .order_by(ScheduleEntryRow.work_date, ScheduleEntryRow.start_time)
    ).scalars()
    out: dict[tuple[int, date], tuple[datetime, datetime]] = {}
    for r in rows:
        key = (r.employee_id, r.work_date)
        if key in out:
            continue  # 하루 2탕이면 첫 근무를 기준으로 봅니다
        start = datetime.combine(r.work_date, r.start_time).replace(tzinfo=ctx.tz)
        end_day = r.work_date + (timedelta(days=1) if r.end_time <= r.start_time else timedelta())
        end = datetime.combine(end_day, r.end_time).replace(tzinfo=ctx.tz)
        out[key] = (start, end)
    return out


def presets(db: Session) -> list[ShiftPreset]:
    return list(
        db.execute(select(ShiftPreset).order_by(ShiftPreset.sort_order, ShiftPreset.id)).scalars()
    )


def _snapshot(entries: list[ScheduleEntry]) -> dict[str, str]:
    """격자 한 칸을 한 단위로 봅니다.

    10-22 를 12-22 로 고친 것은 사람에게 '한 칸 변경'이지 '삭제 + 추가'가
    아닙니다. 변경 이력이 사람이 세는 방식과 같아야 읽힙니다.
    """
    cells: dict[str, list[str]] = {}
    for e in sorted(entries, key=lambda x: (x.work_date, x.start)):
        cells.setdefault(f"{e.employee_id}|{e.work_date}", []).append(e.label)
    return {k: " ".join(v) for k, v in cells.items()}


def _diff(before: dict[str, str], after: dict[str, str]) -> list[dict]:
    out = []
    for k in sorted(set(before) | set(after)):
        b, a = before.get(k), after.get(k)
        if b != a:
            emp, day = k.split("|")
            out.append({"employee_id": int(emp), "date": day,
                        "before": b or "", "after": a or ""})
    return out


def _audit(db: Session, actor: str, action: str, details: str) -> None:
    db.add(AuditLog(actor=actor, action=action, details=details))
