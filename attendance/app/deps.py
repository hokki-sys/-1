"""요청 -> 사용자 -> 매장 컨텍스트.

라우터는 여기서 나온 컨텍스트만 신뢰합니다. 컨텍스트를 못 만들면 쿼리는
시작조차 하지 않습니다.
"""
from __future__ import annotations

from typing import Annotated, Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .config import get_settings
from .db import SessionLocal
from .models.control import ROLE_MANAGER, ROLE_OWNER, User
from .security import read_session
from .tenancy import StoreContext, get_user, load_context
from sqlalchemy import text

_settings = get_settings()


class LoginRequired(Exception):
    def __init__(self, next_url: str = "/"):
        self.next_url = next_url


def current_user(request: Request) -> User:
    token = request.cookies.get(_settings.session_cookie, "")
    uid = read_session(token) if token else None
    user = get_user(uid) if uid else None
    if user is None:
        raise LoginRequired(str(request.url.path))
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def store_ctx(slug: str, user: CurrentUser) -> StoreContext:
    ctx = load_context(user.id, slug)
    if ctx is None:
        # 권한이 없는 매장과 없는 매장을 구분해 알려주지 않습니다.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "매장을 찾을 수 없습니다")
    return ctx


StoreCtx = Annotated[StoreContext, Depends(store_ctx)]


def store_db(ctx: StoreCtx) -> Iterator[Session]:
    """이 트랜잭션에서는 그 매장 스키마만 보입니다 (설계 D4).

    search_path 에 public 을 넣지 않는 것이 핵심입니다. 다른 매장 테이블은
    이름으로도 닿지 않으므로, WHERE store_id 를 빠뜨려 데이터가 새는 사고가
    성립하지 않습니다.
    """
    with SessionLocal() as s:
        s.execute(text(f"SET LOCAL search_path TO {ctx.schema}"))
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise


StoreDB = Annotated[Session, Depends(store_db)]


def require_manager(ctx: StoreCtx) -> StoreContext:
    if not ctx.at_least(ROLE_MANAGER):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "권한이 없습니다")
    return ctx


def require_owner(ctx: StoreCtx) -> StoreContext:
    if not ctx.at_least(ROLE_OWNER):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "사장 권한이 필요합니다")
    return ctx


ManagerCtx = Annotated[StoreContext, Depends(require_manager)]
OwnerCtx = Annotated[StoreContext, Depends(require_owner)]


def redirect_to_login(next_url: str) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(f"/login?next={quote(next_url)}", status_code=303)
