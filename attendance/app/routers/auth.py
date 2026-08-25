from __future__ import annotations

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from ..config import get_settings
from ..db import control_session
from ..deps import CurrentUser
from ..models.control import User
from ..security import issue_session, verify_password
from ..templating import templates
from ..tenancy import stores_for_user

router = APIRouter(tags=["auth"])
_settings = get_settings()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse(
        request, "login.html", {"next": next, "error": error}
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    with control_session() as s:
        user = s.execute(
            select(User).where(User.email == email.strip().lower())
        ).scalar_one_or_none()
        ok = bool(user and user.is_active and verify_password(password, user.password_hash))
        uid = user.id if ok and user else None

    if not ok:
        # 계정이 없는 것과 비밀번호가 틀린 것을 구분해 알려주지 않습니다.
        return templates.TemplateResponse(
            request, "login.html",
            {"next": next, "error": "이메일 또는 비밀번호가 맞지 않습니다"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    resp = RedirectResponse(next or "/", status_code=303)
    resp.set_cookie(
        _settings.session_cookie, issue_session(uid),
        max_age=_settings.session_max_age, httponly=True, samesite="lax",
        secure=_settings.secure_cookies,
    )
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(_settings.session_cookie)
    return resp


@router.get("/", response_class=HTMLResponse)
def home(request: Request, user: CurrentUser):
    stores = stores_for_user(user.id)
    if len(stores) == 1:
        return RedirectResponse(f"/s/{stores[0][0].slug}/", status_code=303)
    return templates.TemplateResponse(
        request, "stores.html", {"user": user, "stores": stores}
    )
