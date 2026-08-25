"""근무표 사진 가져오기 (설계 D7·D8).

편집기가 생긴 뒤로 이 기능은 **주력이 아니라 온보딩 경로**입니다. 신규 매장이
쓰던 종이 근무표를 처음 한 번 들여올 때만 씁니다.

인식 결과는 DB 에 바로 넣지 않고 **편집기의 초안으로 띄웁니다.** 그래서 별도
검토 화면이 필요 없습니다 — 편집기가 곧 검토 화면입니다.
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from ..config import get_settings
from ..deps import CurrentUser, ManagerCtx, StoreCtx, StoreDB
from ..domain.business_day import week_start
from ..domain.schedule import ScheduleEntry, parse_time
from ..models.store import AuditLog, Employee, ScheduleImport
from ..services import schedules as sched
from ..services.ocr import build_provider
from ..templating import templates
from ..tenancy import stores_for_user

router = APIRouter(prefix="/s/{slug}/import", tags=["import"])

DAY_OFFSET = {d: i for i, d in enumerate("월화수목금토일")}
MAX_UPLOAD_BYTES = 12 * 1024 * 1024


@router.get("", response_class=HTMLResponse)
def index(request: Request, user: CurrentUser, ctx: StoreCtx, db: StoreDB,
          ok: str = "", error: str = ""):
    settings = get_settings()
    history = list(
        db.execute(
            select(ScheduleImport).order_by(ScheduleImport.created_at.desc()).limit(10)
        ).scalars()
    )
    return templates.TemplateResponse(
        request, "import.html",
        {"user": user, "ctx": ctx, "nav": "import", "history": history,
         "ok": ok, "error": error, "stores": stores_for_user(user.id),
         "server_enabled": settings.ocr_enabled,
         "store_enabled": bool(ctx.settings.get("ocr_import_enabled")),
         "model": settings.deepseek_vision_model,
         "default_week": week_start(date.today() + timedelta(days=7)).isoformat()},
    )


@router.post("/upload")
async def upload(user: CurrentUser, ctx: ManagerCtx, db: StoreDB,
                 week: str = Form(...), image: UploadFile = File(...)):
    settings = get_settings()
    if not settings.ocr_enabled:
        return _back(ctx.slug, error="서버에 OCR 키가 설정되어 있지 않습니다")
    if not ctx.settings.get("ocr_import_enabled"):
        return _back(ctx.slug, error="이 매장은 근무표 가져오기가 꺼져 있습니다")

    data = await image.read()
    if not data:
        return _back(ctx.slug, error="이미지가 비어 있습니다")
    if len(data) > MAX_UPLOAD_BYTES:
        return _back(ctx.slug, error=f"이미지가 너무 큽니다 ({len(data)//1024//1024}MB)")

    try:
        monday = week_start(date.fromisoformat(week))
    except ValueError:
        return _back(ctx.slug, error="주차 날짜가 올바르지 않습니다")

    provider = build_provider(ctx.departments())
    record = ScheduleImport(
        week_start=monday, image_data=data,
        image_mime=image.content_type or "image/jpeg",
        provider=provider.name, model=provider.model, status="pending",
    )
    db.add(record)
    db.flush()

    result = provider.extract_schedule(data, record.image_mime)
    record.raw_response = result.raw_response      # AI 원본 응답을 그대로 보관
    record.parsed = result.parsed
    record.tiles = result.tiles
    record.status = "done" if result.ok else "failed"
    record.error = result.error
    db.add(AuditLog(
        actor=user.email, action="SCHEDULE_IMPORTED",
        details=f"{monday} 주차, {result.tiles}조각, "
                f"{'성공' if result.ok else '실패: ' + result.error}",
    ))
    if not result.ok:
        return _back(ctx.slug, error=result.error or "근무 항목을 찾지 못했습니다")

    entries, unknown = _to_entries(db, monday, result.parsed)
    # 초안으로 저장만 하고 발행하지 않습니다. 사람이 편집기에서 확인해야 합니다.
    sched.save_entries(db, ctx, monday, entries, user.email)

    msg = f"{len(entries)}건을 초안으로 불러왔습니다"
    if unknown:
        msg += f" · 미등록 이름 {len(unknown)}명({', '.join(sorted(unknown)[:4])})은 건너뛰었습니다"
    return RedirectResponse(
        f"/s/{ctx.slug}/schedule?week={monday}&ok={msg}", status_code=303
    )


def _to_entries(db, monday: date, parsed: dict) -> tuple[list[ScheduleEntry], set[str]]:
    """인식 결과를 편집기 항목으로. 이름은 양끝 공백을 떼고 정확히 맞춥니다."""
    people = {
        e.name.strip(): e
        for e in db.execute(
            select(Employee).where(Employee.is_active.is_(True))
        ).scalars()
    }
    entries: list[ScheduleEntry] = []
    unknown: set[str] = set()
    for item in parsed.get("schedule_details", []):
        name = (item.get("name") or "").strip()
        emp = people.get(name)
        if emp is None:
            unknown.add(name)
            continue
        offset = DAY_OFFSET.get((item.get("day") or "")[:1])
        if offset is None:
            continue
        try:
            start = parse_time(item.get("start_time") or "")
            end_raw = (item.get("end_time") or "").strip()
            # 퇴근 시각이 없으면 매장 마감 시간으로 채우고 사람이 고치게 둡니다.
            end = parse_time(end_raw) if end_raw else parse_time("22:00")
        except ValueError:
            continue
        entries.append(ScheduleEntry(
            employee_id=emp.id, employee_name=emp.name,
            work_date=monday + timedelta(days=offset),
            start=start, end=end,
            department=(item.get("department") or emp.department or "").strip(),
        ))
    return entries, unknown


def _back(slug: str, ok: str = "", error: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in (("ok", ok), ("error", error)) if v})
    return RedirectResponse(f"/s/{slug}/import" + (f"?{qs}" if qs else ""), status_code=303)
