from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from ..deps import CurrentUser, ManagerCtx, StoreCtx, StoreDB
from ..models.store import AuditLog, Employee
from ..services.sessions import recompute_employee
from ..templating import templates
from ..tenancy import stores_for_user

router = APIRouter(prefix="/s/{slug}/employees", tags=["employees"])


@router.get("", response_class=HTMLResponse)
def index(request: Request, user: CurrentUser, ctx: StoreCtx, db: StoreDB,
          show_inactive: bool = False, error: str = "", ok: str = ""):
    q = select(Employee).order_by(Employee.department, Employee.name)
    if not show_inactive:
        q = q.where(Employee.is_active.is_(True))
    return templates.TemplateResponse(
        request, "employees.html",
        {
            "user": user, "ctx": ctx, "nav": "employees",
            "employees": list(db.execute(q).scalars()),
            "show_inactive": show_inactive, "error": error, "ok": ok,
            "stores": stores_for_user(user.id),
        },
    )


@router.post("/add")
def add(user: CurrentUser, ctx: ManagerCtx, db: StoreDB,
        name: str = Form(...), card_uid: str = Form(""),
        employee_type: str = Form("아르바이트"), department: str = Form("")):
    name, card = name.strip(), card_uid.strip()
    if not name:
        return _back(ctx.slug, error="이름을 입력하세요")
    if card and _card_taken(db, card, None):
        return _back(ctx.slug, error=f"카드 {card} 는 이미 다른 직원이 쓰고 있습니다")

    emp = Employee(name=name, card_uid=card or None,
                   employee_type=employee_type, department=department.strip())
    db.add(emp)
    db.flush()
    db.add(AuditLog(actor=user.email, action="EMPLOYEE_ADDED",
                    details=f"{name} (카드 {card or '없음'}, {department or '부서 없음'})"))
    if card:
        # 이 카드로 이미 들어와 있던 미등록 탭이 있으면 되살립니다.
        _claim_orphan_punches(db, ctx, emp)
    return _back(ctx.slug, ok=f"{name} 등록했습니다")


@router.post("/{emp_id}/update")
def update(emp_id: int, user: CurrentUser, ctx: ManagerCtx, db: StoreDB,
           name: str = Form(...), card_uid: str = Form(""),
           employee_type: str = Form("아르바이트"), department: str = Form("")):
    emp = db.get(Employee, emp_id)
    if emp is None:
        return _back(ctx.slug, error="직원을 찾을 수 없습니다")
    card = card_uid.strip()
    if card and _card_taken(db, card, emp_id):
        return _back(ctx.slug, error=f"카드 {card} 는 이미 다른 직원이 쓰고 있습니다")

    changes = []
    for field, new in (("name", name.strip()), ("card_uid", card or None),
                       ("employee_type", employee_type), ("department", department.strip())):
        old = getattr(emp, field)
        if old != new:
            changes.append(f"{field}: {old or '없음'} -> {new or '없음'}")
            setattr(emp, field, new)
    if changes:
        db.add(AuditLog(actor=user.email, action="EMPLOYEE_UPDATED",
                        details=f"{emp.name}: " + ", ".join(changes)))
        if card:
            _claim_orphan_punches(db, ctx, emp)
    return _back(ctx.slug, ok="수정했습니다")


@router.post("/{emp_id}/deactivate")
def deactivate(emp_id: int, user: CurrentUser, ctx: ManagerCtx, db: StoreDB):
    """퇴사 처리. **기록은 지우지 않습니다.**

    데스크톱 버전은 직원을 지우면 출퇴근 기록이 CASCADE 로 함께 사라졌습니다.
    근로 관련 기록에는 보존 의무가 있고, 급여 근거가 통째로 날아가면 안 됩니다.
    """
    emp = db.get(Employee, emp_id)
    if emp is None:
        return _back(ctx.slug, error="직원을 찾을 수 없습니다")
    emp.is_active = False
    emp.deactivated_at = datetime.now(timezone.utc)
    emp.card_uid = None      # 카드는 회수 — 다른 직원에게 재발급할 수 있게
    db.add(AuditLog(actor=user.email, action="EMPLOYEE_DEACTIVATED",
                    details=f"{emp.name} 퇴사 처리 (기록은 보존)"))
    return _back(ctx.slug, ok=f"{emp.name} 퇴사 처리했습니다. 근무 기록은 그대로 남습니다")


@router.post("/{emp_id}/reactivate")
def reactivate(emp_id: int, user: CurrentUser, ctx: ManagerCtx, db: StoreDB):
    emp = db.get(Employee, emp_id)
    if emp is None:
        return _back(ctx.slug, error="직원을 찾을 수 없습니다")
    emp.is_active = True
    emp.deactivated_at = None
    db.add(AuditLog(actor=user.email, action="EMPLOYEE_REACTIVATED", details=emp.name))
    return _back(ctx.slug, ok=f"{emp.name} 복직 처리했습니다")


def _card_taken(db, card: str, exclude_id: int | None) -> bool:
    q = select(Employee.id).where(
        Employee.card_uid == card, Employee.is_active.is_(True)
    )
    if exclude_id:
        q = q.where(Employee.id != exclude_id)
    return db.execute(q).first() is not None


def _claim_orphan_punches(db, ctx, emp: Employee) -> None:
    """등록 전에 찍혀 주인이 없던 탭을 이 직원에게 붙이고 재계산합니다.

    수집 단계에서 미등록 카드도 버리지 않고 남겨둔 덕분에 가능한 일입니다.
    """
    from ..models.store import PunchEvent

    orphans = list(
        db.execute(
            select(PunchEvent).where(
                PunchEvent.card_uid == emp.card_uid, PunchEvent.employee_id.is_(None)
            )
        ).scalars()
    )
    for p in orphans:
        p.employee_id = emp.id
    db.flush()
    if orphans:
        db.add(AuditLog(actor="system", action="PUNCHES_CLAIMED",
                        details=f"{emp.name} 등록으로 미등록 탭 {len(orphans)}건 연결"))
    recompute_employee(db, ctx, emp.id)


def _back(slug: str, ok: str = "", error: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in (("ok", ok), ("error", error)) if v})
    return RedirectResponse(f"/s/{slug}/employees" + (f"?{qs}" if qs else ""), status_code=303)
