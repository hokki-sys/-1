"""단말용 API. 사람이 아니라 기계가 부르는 두 개의 엔드포인트뿐입니다."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from ..db import SessionLocal
from ..services.ingest import (
    CLOCK_SKEW_ALERT_SECONDS, authenticate_device, ingest_punch, record_heartbeat,
)
from sqlalchemy import text

router = APIRouter(prefix="/api", tags=["device"])


class PunchIn(BaseModel):
    #: 멱등 키. 단말이 생성하며, 재전송해도 기록이 늘지 않습니다.
    client_event_id: str = Field(min_length=8, max_length=64)
    card_uid: str = Field(min_length=1, max_length=64)
    #: 단말이 실제로 찍힌 시각. 서버 도착 시각을 쓰지 않는 이유는 설계 D2 참고.
    tapped_at: datetime


class PunchBatch(BaseModel):
    punches: list[PunchIn] = Field(max_length=500)
    device_time: datetime | None = None
    queue_depth: int = 0


class PunchAck(BaseModel):
    client_event_id: str
    accepted: bool
    duplicate: bool
    known_card: bool
    employee_name: str | None
    message: str


class BatchAck(BaseModel):
    results: list[PunchAck]
    clock_skew_seconds: float
    clock_warning: str | None = None


def _auth(token: str | None):
    auth = authenticate_device((token or "").removeprefix("Bearer ").strip())
    if auth is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "단말 토큰이 유효하지 않습니다")
    return auth


@router.post("/punches", response_model=BatchAck)
def submit_punches(
    body: PunchBatch,
    authorization: Annotated[str | None, Header()] = None,
) -> BatchAck:
    auth = _auth(authorization)
    skew = record_heartbeat(auth.device_id, body.device_time, body.queue_depth)

    results: list[PunchAck] = []
    with SessionLocal() as db:
        db.execute(text(f"SET LOCAL search_path TO {auth.store_ctx.schema}"))
        try:
            for p in body.punches:
                r = ingest_punch(
                    db, auth.store_ctx,
                    client_event_id=p.client_event_id,
                    card_uid=p.card_uid,
                    tapped_at=p.tapped_at,
                    device_name=auth.device_name,
                )
                results.append(PunchAck(
                    client_event_id=p.client_event_id, accepted=r.accepted,
                    duplicate=r.duplicate, known_card=r.known_card,
                    employee_name=r.employee_name, message=r.message,
                ))
            db.commit()
        except Exception:
            db.rollback()
            raise

    warning = None
    if abs(skew) > CLOCK_SKEW_ALERT_SECONDS:
        warning = f"단말 시계가 서버와 {abs(skew):.0f}초 어긋나 있습니다"
    return BatchAck(results=results, clock_skew_seconds=skew, clock_warning=warning)


@router.post("/heartbeat")
def heartbeat(
    device_time: datetime | None = None,
    queue_depth: int = 0,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    auth = _auth(authorization)
    skew = record_heartbeat(auth.device_id, device_time, queue_depth)
    return {
        "ok": True,
        "store": auth.store_ctx.name,
        "device": auth.device_name,
        "clock_skew_seconds": skew,
        "clock_ok": abs(skew) <= CLOCK_SKEW_ALERT_SECONDS,
    }
