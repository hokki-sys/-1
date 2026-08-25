from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from ..db import control_session
from ..deps import CurrentUser, OwnerCtx, StoreCtx
from ..models.control import Device
from ..security import hash_device_token, new_device_token
from ..templating import templates
from ..tenancy import stores_for_user

router = APIRouter(prefix="/s/{slug}/devices", tags=["devices"])


@router.get("", response_class=HTMLResponse)
def index(request: Request, user: CurrentUser, ctx: StoreCtx, new_token: str = "", ok: str = ""):
    with control_session() as s:
        devices = list(
            s.execute(
                select(Device).where(Device.store_id == ctx.store_id).order_by(Device.id)
            ).scalars()
        )
        for d in devices:
            s.expunge(d)
    return templates.TemplateResponse(
        request, "devices.html",
        {"user": user, "ctx": ctx, "nav": "devices", "devices": devices,
         "new_token": new_token, "ok": ok, "stores": stores_for_user(user.id)},
    )


@router.post("/add")
def add(ctx: OwnerCtx, user: CurrentUser, name: str = Form(...)):
    """토큰은 **이 순간 한 번만** 보여줍니다. 저장은 해시만 합니다."""
    token = new_device_token()
    with control_session() as s:
        s.add(Device(store_id=ctx.store_id, name=name.strip() or "단말",
                     token_hash=hash_device_token(token)))
    return RedirectResponse(f"/s/{ctx.slug}/devices?new_token={token}", status_code=303)


@router.post("/{device_id}/revoke")
def revoke(device_id: int, ctx: OwnerCtx, user: CurrentUser):
    with control_session() as s:
        d = s.get(Device, device_id)
        if d and d.store_id == ctx.store_id:
            d.revoked_at = datetime.now(timezone.utc)
    return RedirectResponse(f"/s/{ctx.slug}/devices?ok=폐기했습니다", status_code=303)
