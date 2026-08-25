"""근무표 편집기 (설계 D8).

격자는 행=직원, 열=요일. 한 주를 통째로 저장하고, 발행해야 지각 판정의
기준이 됩니다. 경고는 저장을 막지 않습니다 — 인원 공백이나 시간 경계는
알고도 그렇게 짜는 경우가 많고, 막으면 시스템을 우회하게 됩니다.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from ..deps import CurrentUser, ManagerCtx, StoreCtx, StoreDB
from ..domain.business_day import week_start
from ..domain.schedule import ScheduleEntry, build_week, parse_cell
from ..models.store import Employee, ScheduleRevision
from ..services import schedules as sched
from ..templating import templates
from ..tenancy import stores_for_user

router = APIRouter(prefix="/s/{slug}/schedule", tags=["schedule"])


def _monday(value: str) -> date:
    try:
        return week_start(date.fromisoformat(value)) if value else week_start(date.today())
    except ValueError:
        return week_start(date.today())


def _render(request, user, ctx, db, monday, entries=None, ok="", error="", template="schedule.html"):
    entries = sched.load_entries(db, monday) if entries is None else entries
    week = sched.get_week(db, monday)
    employees = list(
        db.execute(
            select(Employee).where(Employee.is_active.is_(True))
            .order_by(Employee.department, Employee.name)
        ).scalars()
    )
    rows = build_week(entries, monday)
    # 근무가 하나도 없는 직원도 격자에 나와야 배정할 수 있습니다.
    present = {r.employee_id for r in rows}
    from ..domain.schedule import EmployeeWeek
    days = [monday + timedelta(days=i) for i in range(7)]
    for e in employees:
        if e.id not in present:
            rows.append(EmployeeWeek(e.id, e.name, e.department, {d: [] for d in days}))
    rows.sort(key=lambda r: (r.department, r.employee_name))

    revisions = list(
        db.execute(
            select(ScheduleRevision)
            .where(ScheduleRevision.week_id == (week.id if week else -1))
            .order_by(ScheduleRevision.created_at.desc()).limit(5)
        ).scalars()
    ) if week else []

    return templates.TemplateResponse(
        request, template,
        {
            "user": user, "ctx": ctx, "nav": "schedule",
            "monday": monday, "days": days, "rows": rows,
            "week": week, "employees": employees,
            "presets": sched.presets(db),
            "warnings": sched.warnings_for(db, ctx, monday, entries),
            "thresholds": ctx.thresholds(), "revisions": revisions,
            "ok": ok, "error": error, "stores": stores_for_user(user.id),
            "prev_week": (monday - timedelta(days=7)).isoformat(),
            "next_week": (monday + timedelta(days=7)).isoformat(),
        },
    )


@router.get("", response_class=HTMLResponse)
def index(request: Request, user: CurrentUser, ctx: StoreCtx, db: StoreDB,
          week: str = "", ok: str = "", error: str = ""):
    return _render(request, user, ctx, db, _monday(week), ok=ok, error=error)


@router.get("/print", response_class=HTMLResponse)
def print_view(request: Request, user: CurrentUser, ctx: StoreCtx, db: StoreDB, week: str = ""):
    """A4 인쇄용. 주방 직원은 앱을 안 봅니다 — 벽에 붙일 종이가 나와야 씁니다."""
    return _render(request, user, ctx, db, _monday(week), template="schedule_print.html")


@router.post("/save")
def save(request: Request, user: CurrentUser, ctx: ManagerCtx, db: StoreDB,
         week: str = Form(...), grid: str = Form(...)):
    """격자를 통째로 저장합니다. grid 는 {"<직원id>|<날짜>": "10-22", ...}."""
    monday = _monday(week)
    try:
        raw = json.loads(grid)
    except json.JSONDecodeError:
        return _redirect(ctx.slug, monday, error="근무표 데이터를 읽지 못했습니다")

    names = {
        e.id: (e.name, e.department)
        for e in db.execute(select(Employee)).scalars()
    }
    entries: list[ScheduleEntry] = []
    bad: list[str] = []
    for key, value in raw.items():
        text = (value or "").strip()
        if not text:
            continue
        try:
            emp_id_s, day_s = key.split("|", 1)
            emp_id, work_date = int(emp_id_s), date.fromisoformat(day_s)
            shifts = parse_cell(text)
        except (ValueError, KeyError):
            bad.append(f"{key} = {text}")
            continue
        if emp_id not in names:
            bad.append(f"알 수 없는 직원 {emp_id}")
            continue
        name, dept = names[emp_id]
        for start, end in shifts:
            entries.append(ScheduleEntry(emp_id, name, work_date, start, end, dept))

    sched.save_entries(db, ctx, monday, entries, user.email)
    msg = f"{len(entries)}건 저장했습니다"
    if bad:
        msg += f" (읽지 못한 칸 {len(bad)}개: {', '.join(bad[:3])})"
    return _redirect(ctx.slug, monday, ok=msg)


@router.post("/publish")
def publish(user: CurrentUser, ctx: ManagerCtx, db: StoreDB, week: str = Form(...)):
    monday = _monday(week)
    sched.publish(db, monday, user.email)
    return _redirect(ctx.slug, monday,
                     ok="발행했습니다. 이제부터 이 근무표가 지각 판정의 기준입니다")


@router.post("/unpublish")
def unpublish(user: CurrentUser, ctx: ManagerCtx, db: StoreDB, week: str = Form(...)):
    monday = _monday(week)
    sched.unpublish(db, monday, user.email)
    return _redirect(ctx.slug, monday, ok="초안으로 되돌렸습니다. 지각 판정에 쓰이지 않습니다")


@router.post("/copy-previous")
def copy_previous(request: Request, user: CurrentUser, ctx: ManagerCtx, db: StoreDB,
                  week: str = Form(...)):
    """지난주 복사 — 편집기에서 가장 많이 쓰게 될 버튼입니다."""
    monday = _monday(week)
    entries = sched.copy_previous_week(db, monday)
    if not entries:
        return _redirect(ctx.slug, monday, error="지난주에 저장된 근무표가 없습니다")
    sched.save_entries(db, ctx, monday, entries, user.email)
    return _redirect(ctx.slug, monday, ok=f"지난주에서 {len(entries)}건 복사했습니다. 확인 후 발행하세요")


def _redirect(slug: str, monday: date, ok: str = "", error: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in
                    (("week", monday.isoformat()), ("ok", ok), ("error", error)) if v})
    return RedirectResponse(f"/s/{slug}/schedule?{qs}", status_code=303)
