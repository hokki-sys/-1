from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select

from ..db import SessionLocal, control_session
from ..deps import CurrentUser, StoreCtx, StoreDB
from ..domain.business_day import business_date
from ..models.control import Device
from ..services import reports
from ..templating import templates
from ..tenancy import stores_for_user
from sqlalchemy import text

router = APIRouter(prefix="/s/{slug}", tags=["dashboard"])

STREAM_INTERVAL_SECONDS = 5


def _today(ctx) -> "datetime.date":
    now = datetime.now(timezone.utc).astimezone(ctx.tz)
    return business_date(now.replace(tzinfo=None), ctx.settings["business_day_cutoff_hour"])


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: CurrentUser, ctx: StoreCtx, db: StoreDB):
    today = _today(ctx)
    state = reports.dashboard(db, ctx, today)
    with control_session() as s:
        devices = list(
            s.execute(select(Device).where(Device.store_id == ctx.store_id)).scalars()
        )
        for d in devices:
            s.expunge(d)
    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "user": user, "ctx": ctx, "state": state, "today": today,
            "devices": devices, "stores": stores_for_user(user.id), "nav": "dashboard",
        },
    )


@router.get("/stream")
async def stream(request: Request, user: CurrentUser, ctx: StoreCtx):
    """대시보드 실시간 갱신.

    폴링이 아니라 SSE 입니다 — 흐름이 서버에서 브라우저 한 방향뿐이고,
    재연결이 브라우저에 내장돼 있어 WebSocket 보다 다룰 게 적습니다.
    """

    async def gen():
        last = None
        while True:
            if await request.is_disconnected():
                return
            payload = await asyncio.to_thread(_snapshot, ctx)
            if payload != last:
                last = payload
                yield f"data: {payload}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(STREAM_INTERVAL_SECONDS)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _snapshot(ctx) -> str:
    today = _today(ctx)
    with SessionLocal() as db:
        db.execute(text(f"SET LOCAL search_path TO {ctx.schema}"))
        st = reports.dashboard(db, ctx, today)
        return json.dumps(
            {
                "headcount": st.headcount,
                "on_duty": [
                    {"name": r.employee_name, "since": r.check_in.strftime("%H:%M"),
                     "late": r.late}
                    for r in st.on_duty
                ],
                "recent": [
                    {"name": r.employee_name,
                     "kind": "퇴근" if r.check_out else "출근",
                     "at": (r.check_out or r.check_in).strftime("%H:%M"),
                     "late": r.late}
                    for r in st.recent
                ],
                "late": st.late_today,
                "attention": len(st.attention),
            },
            ensure_ascii=False,
        )
