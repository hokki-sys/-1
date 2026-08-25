"""출퇴근 기록 조회와 보정.

원본 탭은 수정하지 않습니다. 관리자 보정은 adjustments 에 쌓이고, 세션에는
'사람이 손댐' 표시가 붙어 재계산이 덮어쓰지 않습니다 (설계 D1).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..deps import CurrentUser, ManagerCtx, StoreCtx, StoreDB
from ..models.store import Adjustment, AuditLog, WorkSession
from ..services import reports
from ..templating import templates
from ..tenancy import stores_for_user

router = APIRouter(prefix="/s/{slug}/logs", tags=["logs"])


def _range(start: str, end: str) -> tuple[date, date]:
    today = date.today()
    try:
        s = date.fromisoformat(start) if start else today - timedelta(days=30)
        e = date.fromisoformat(end) if end else today
    except ValueError:
        s, e = today - timedelta(days=30), today
    return (e, s) if s > e else (s, e)


@router.get("", response_class=HTMLResponse)
def index(request: Request, user: CurrentUser, ctx: StoreCtx, db: StoreDB,
          start: str = "", end: str = "", q: str = "", ok: str = "", error: str = ""):
    s, e = _range(start, end)
    rows = reports.sessions_between(db, ctx, s, e, q.strip())
    return templates.TemplateResponse(
        request, "logs.html",
        {"user": user, "ctx": ctx, "nav": "logs", "rows": rows,
         "start": s, "end": e, "q": q, "ok": ok, "error": error,
         "totals": reports.totals(db, ctx, s, e, q.strip()),
         "stores": stores_for_user(user.id)},
    )


@router.get("/export.csv")
def export(user: CurrentUser, ctx: StoreCtx, db: StoreDB,
           start: str = "", end: str = "", q: str = ""):
    s, e = _range(start, end)
    rows = reports.sessions_between(db, ctx, s, e, q.strip())
    csv_text = reports.sessions_csv(rows, f"{s} ~ {e}")
    filename = f"출퇴근기록_{ctx.slug}_{s}_{e}.csv"
    return Response(
        # 엑셀이 UTF-8 을 알아보게 BOM 을 붙입니다.
        content="﻿" + csv_text,
        media_type="text/csv; charset=utf-8",
        # 한글 파일명은 반드시 퍼센트 인코딩해야 합니다. HTTP 헤더는 latin-1 만 담습니다.
        headers={
            "Content-Disposition":
                "attachment; filename*=UTF-8''" + quote(filename)
        },
    )


@router.get("/{session_id}/trail", response_class=HTMLResponse)
def trail(session_id: int, request: Request, user: CurrentUser, ctx: StoreCtx, db: StoreDB):
    """이 세션이 어떤 원본 탭에서 나왔는지 보여줍니다."""
    ws = db.get(WorkSession, session_id)
    punches = reports.punches_for_session(db, session_id)
    return templates.TemplateResponse(
        request, "partials/trail.html",
        {"ctx": ctx, "ws": ws, "punches": punches,
         "ignored": set(ws.ignored_punch_ids or []) if ws else set()},
    )


@router.post("/{session_id}/adjust")
def adjust(session_id: int, user: CurrentUser, ctx: ManagerCtx, db: StoreDB,
           check_in: str = Form(...), check_out: str = Form(""), reason: str = Form("")):
    ws = db.get(WorkSession, session_id)
    if ws is None:
        return _back(ctx.slug, error="기록을 찾을 수 없습니다")
    try:
        cin = datetime.fromisoformat(check_in).replace(tzinfo=ctx.tz)
        cout = datetime.fromisoformat(check_out).replace(tzinfo=ctx.tz) if check_out.strip() else None
    except ValueError:
        return _back(ctx.slug, error="시간 형식이 올바르지 않습니다 (예: 2026-08-25 10:00)")
    if cout and cout <= cin:
        return _back(ctx.slug, error="퇴근이 출근보다 빠릅니다")
    if not reason.strip():
        return _back(ctx.slug, error="보정 사유를 적어주세요 — 나중에 근거가 됩니다")

    before = f"{ws.check_in:%Y-%m-%d %H:%M} ~ " + (
        f"{ws.check_out:%H:%M}" if ws.check_out else "(없음)"
    )
    db.add(Adjustment(
        employee_id=ws.employee_id, business_date=ws.business_date, session_id=ws.id,
        kind="edit", check_in=cin, check_out=cout, reason=reason.strip(), actor=user.email,
    ))
    ws.check_in, ws.check_out = cin, cout
    ws.status = "complete" if cout else "missing_checkout"
    ws.is_adjusted = True     # 재계산이 이 세션을 덮어쓰지 않게 합니다
    after = f"{cin:%Y-%m-%d %H:%M} ~ " + (f"{cout:%H:%M}" if cout else "(없음)")
    db.add(AuditLog(
        actor=user.email, action="SESSION_ADJUSTED",
        details=f"세션 {ws.id}: {before} -> {after}. 사유: {reason.strip()}",
    ))
    return _back(ctx.slug, ok="보정했습니다. 원본 탭은 그대로 남아 있습니다")


def _back(slug: str, ok: str = "", error: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in (("ok", ok), ("error", error)) if v})
    return RedirectResponse(f"/s/{slug}/logs" + (f"?{qs}" if qs else ""), status_code=303)
