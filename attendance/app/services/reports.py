"""조회와 집계.

집계 키는 전부 **employee_id** 입니다. 데스크톱 버전은 화면마다 이름으로 묶어서
동명이인의 근무시간이 한 사람으로 합산됐습니다 (F-15).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.sessions import SessionStatus
from ..domain.workhours import actual_minutes, fmt_minutes, is_late, settled_minutes
from ..models.store import Employee, PunchEvent, WorkSession
from ..tenancy import StoreContext
from . import schedules as sched


@dataclass
class SessionRow:
    session_id: int
    employee_id: int
    employee_name: str
    department: str
    business_date: date
    check_in: datetime
    check_out: datetime | None
    status: str
    is_adjusted: bool
    expected_in: datetime | None = None
    expected_out: datetime | None = None
    late: bool = False

    @property
    def actual(self) -> float:
        return actual_minutes(self.check_in, self.check_out) if self.check_out else 0.0

    @property
    def needs_attention(self) -> bool:
        return self.status == SessionStatus.MISSING_CHECKOUT.value


@dataclass
class EmployeeTotal:
    employee_id: int
    name: str
    department: str
    employee_type: str
    actual_minutes: float = 0.0
    settled_minutes: float = 0.0
    sessions: int = 0
    late_count: int = 0
    open_or_missing: int = 0

    @property
    def actual_label(self) -> str:
        return fmt_minutes(self.actual_minutes)

    @property
    def settled_label(self) -> str:
        return fmt_minutes(self.settled_minutes)


def sessions_between(
    db: Session, ctx: StoreContext, start: date, end: date, name_filter: str = ""
) -> list[SessionRow]:
    """영업일 기준 [start, end] 구간의 세션. 예정 시각과 지각 여부를 붙여 돌려줍니다."""
    q = (
        select(WorkSession, Employee)
        .join(Employee, Employee.id == WorkSession.employee_id)
        .where(WorkSession.business_date.between(start, end))
        .order_by(WorkSession.check_in.desc())
    )
    if name_filter:
        q = q.where(Employee.name.ilike(f"%{name_filter}%"))
    rows = db.execute(q).all()

    days = sorted({ws.business_date for ws, _ in rows})
    expected = sched.expected_times(db, ctx, days)
    grace = ctx.late_grace_seconds()

    out: list[SessionRow] = []
    for ws, emp in rows:
        exp = expected.get((emp.id, ws.business_date))
        exp_in, exp_out = exp if exp else (None, None)
        out.append(
            SessionRow(
                session_id=ws.id,
                employee_id=emp.id,
                employee_name=emp.name,
                department=emp.department,
                business_date=ws.business_date,
                check_in=ws.check_in.astimezone(ctx.tz),
                check_out=ws.check_out.astimezone(ctx.tz) if ws.check_out else None,
                status=ws.status,
                is_adjusted=ws.is_adjusted,
                expected_in=exp_in,
                expected_out=exp_out,
                late=is_late(ws.check_in.astimezone(ctx.tz), exp_in, grace),
            )
        )
    return out


def totals(
    db: Session, ctx: StoreContext, start: date, end: date, name_filter: str = ""
) -> list[EmployeeTotal]:
    policy = ctx.rounding()
    acc: dict[int, EmployeeTotal] = {}
    for r in sessions_between(db, ctx, start, end, name_filter):
        t = acc.get(r.employee_id)
        if t is None:
            emp = db.get(Employee, r.employee_id)
            t = acc[r.employee_id] = EmployeeTotal(
                employee_id=r.employee_id,
                name=r.employee_name,
                department=r.department,
                employee_type=emp.employee_type if emp else "",
            )
        t.sessions += 1
        if r.late:
            t.late_count += 1
        if r.check_out is None:
            t.open_or_missing += 1
            continue
        t.actual_minutes += actual_minutes(r.check_in, r.check_out)
        t.settled_minutes += settled_minutes(r.check_in, r.check_out, policy)
    return sorted(acc.values(), key=lambda x: (x.department, x.name))


def needs_attention(db: Session, ctx: StoreContext, limit: int = 20) -> list[SessionRow]:
    """퇴근 누락 목록. **자동 마감하지 않고** 사람이 확인하게 둡니다."""
    rows = db.execute(
        select(WorkSession, Employee)
        .join(Employee, Employee.id == WorkSession.employee_id)
        .where(WorkSession.status == SessionStatus.MISSING_CHECKOUT.value)
        .order_by(WorkSession.check_in.desc())
        .limit(limit)
    ).all()
    return [
        SessionRow(
            session_id=ws.id, employee_id=emp.id, employee_name=emp.name,
            department=emp.department, business_date=ws.business_date,
            check_in=ws.check_in.astimezone(ctx.tz), check_out=None,
            status=ws.status, is_adjusted=ws.is_adjusted,
        )
        for ws, emp in rows
    ]


@dataclass
class DashboardState:
    on_duty: list[SessionRow] = field(default_factory=list)
    done_today: list[SessionRow] = field(default_factory=list)
    late_today: int = 0
    headcount: int = 0
    recent: list[SessionRow] = field(default_factory=list)
    attention: list[SessionRow] = field(default_factory=list)


def dashboard(db: Session, ctx: StoreContext, today: date) -> DashboardState:
    rows = sessions_between(db, ctx, today, today)
    st = DashboardState()
    # 출근 인원은 사람 수로 셉니다. 기록 행 수로 세면 한 명이 두 번 찍었을 때
    # 2명으로 보입니다 (데스크톱 버전 F-14).
    st.headcount = len({r.employee_id for r in rows})
    st.on_duty = [r for r in rows if r.status == SessionStatus.OPEN.value]
    st.done_today = [r for r in rows if r.status == SessionStatus.COMPLETE.value]
    st.late_today = len({r.employee_id for r in rows if r.late})
    st.recent = sorted(rows, key=lambda r: r.check_out or r.check_in, reverse=True)[:8]
    st.attention = needs_attention(db, ctx, limit=5)
    return st


def punches_for_session(db: Session, session_id: int) -> list[PunchEvent]:
    ws = db.get(WorkSession, session_id)
    if ws is None:
        return []
    ids = [i for i in [ws.check_in_punch_id, ws.check_out_punch_id] if i]
    ids += list(ws.ignored_punch_ids or [])
    if not ids:
        return []
    return list(
        db.execute(
            select(PunchEvent).where(PunchEvent.id.in_(ids)).order_by(PunchEvent.tapped_at)
        ).scalars()
    )


def sessions_csv(rows: list[SessionRow], period_label: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"조회 기간: {period_label}"])
    w.writerow([])
    w.writerow(["영업일", "이름", "부서", "출근", "퇴근", "실근무", "예정 출근", "지각", "상태"])
    for r in rows:
        w.writerow([
            r.business_date.isoformat(),
            r.employee_name,
            r.department,
            r.check_in.strftime("%Y-%m-%d %H:%M:%S"),
            r.check_out.strftime("%Y-%m-%d %H:%M:%S") if r.check_out else "",
            fmt_minutes(r.actual) if r.check_out else "",
            r.expected_in.strftime("%H:%M") if r.expected_in else "",
            "지각" if r.late else "",
            {"complete": "정상", "open": "근무 중", "missing_checkout": "퇴근 누락"}.get(r.status, r.status),
        ])
    return buf.getvalue()


def totals_csv(rows: list[EmployeeTotal], period_label: str, policy_label: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"조회 기간: {period_label}"])
    w.writerow([f"정산 기준: {policy_label}"])
    w.writerow([])
    w.writerow(["이름", "부서", "유형", "실근무", "정산근무", "정산(분)", "근무일수", "지각", "미완결"])
    for r in rows:
        w.writerow([
            r.name, r.department, r.employee_type,
            r.actual_label, r.settled_label, round(r.settled_minutes),
            r.sessions, r.late_count, r.open_or_missing,
        ])
    return buf.getvalue()
