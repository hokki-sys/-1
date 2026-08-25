from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .deps import LoginRequired, redirect_to_login
from .routers import (
    auth, dashboard, devices, employees, imports, ingest, logs, reports_view, schedule,
)
from .templating import templates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)

settings = get_settings()
app = FastAPI(
    title="출퇴근 관리",
    description="매장별 데이터 분리 · 오프라인 우선 수집 · 근무표 편집기",
    docs_url="/api/docs" if settings.debug else None,
    redoc_url=None,
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

for module in (auth, ingest, dashboard, employees, logs, schedule,
               reports_view, devices, imports):
    app.include_router(module.router)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    return redirect_to_login(exc.next_url)


@app.exception_handler(404)
async def _not_found(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return await _json_error(request, exc, 404)
    return templates.TemplateResponse(
        request, "error.html",
        {"code": 404, "message": "찾을 수 없는 주소입니다"}, status_code=404,
    )


@app.exception_handler(403)
async def _forbidden(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return await _json_error(request, exc, 403)
    return templates.TemplateResponse(
        request, "error.html",
        {"code": 403, "message": "권한이 없습니다"}, status_code=403,
    )


async def _json_error(request: Request, exc, code: int):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": getattr(exc, "detail", "error")}, status_code=code)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}
