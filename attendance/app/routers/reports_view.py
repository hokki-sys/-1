from __future__ import annotations

from datetime import date

from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from ..deps import CurrentUser, StoreCtx, StoreDB
from ..domain.business_day import month_range, week_range
from ..services import reports
from ..templating import templates
from ..tenancy import stores_for_user

router = APIRouter(prefix="/s/{slug}/reports", tags=["reports"])


def _period(period: str, anchor: str) -> tuple[date, date, str]:
    try:
        d = date.fromisoformat(anchor) if anchor else date.today()
    except ValueError:
        d = date.today()
    if period == "daily":
        return d, d, f"{d:%Y년 %m월 %d일}"
    if period == "monthly":
        s, e = month_range(d.year, d.month)
        return s, e, f"{d:%Y년 %m월}"
    s, e = week_range(d)
    return s, e, f"{s} ~ {e}"


@router.get("", response_class=HTMLResponse)
def index(request: Request, user: CurrentUser, ctx: StoreCtx, db: StoreDB,
          period: str = "weekly", anchor: str = ""):
    s, e, label = _period(period, anchor)
    return templates.TemplateResponse(
        request, "reports.html",
        {"user": user, "ctx": ctx, "nav": "reports",
         "rows": reports.totals(db, ctx, s, e), "period": period,
         "anchor": anchor or date.today().isoformat(), "label": label,
         "policy_label": ctx.rounding().label, "stores": stores_for_user(user.id)},
    )


@router.get("/export.csv")
def export(user: CurrentUser, ctx: StoreCtx, db: StoreDB,
           period: str = "weekly", anchor: str = ""):
    s, e, label = _period(period, anchor)
    csv_text = reports.totals_csv(
        reports.totals(db, ctx, s, e), label, ctx.rounding().label
    )
    filename = f"근무시간_{ctx.slug}_{s}_{e}.csv"
    return Response(
        content="﻿" + csv_text,
        media_type="text/csv; charset=utf-8",
        # 한글 파일명은 반드시 퍼센트 인코딩해야 합니다. HTTP 헤더는 latin-1 만 담습니다.
        headers={
            "Content-Disposition":
                "attachment; filename*=UTF-8''" + quote(filename)
        },
    )
