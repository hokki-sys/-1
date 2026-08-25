"""단말 -> 서버 수집 (설계 D1·D2).

단말은 판정하지 않습니다. 카드가 찍힌 사실만 보내고, 서버가 짝을 맞춥니다.
수집은 **멱등**입니다 — 네트워크가 불안정할수록 중복 전송은 정상 상태이므로,
같은 client_event_id 가 또 오면 성공으로 응답하고 무시합니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import control_session
from ..models.control import Device, Store
from ..models.store import Employee, PunchEvent
from ..security import hash_device_token
from ..tenancy import StoreContext, merged_settings
from .sessions import DEFAULT_LOOKBACK_DAYS, recompute_employee

#: 단말 시계가 이만큼 어긋나면 관리자에게 알립니다 (설계 D2).
CLOCK_SKEW_ALERT_SECONDS = 120


@dataclass
class DeviceAuth:
    device_id: int
    device_name: str
    store_ctx: StoreContext


@dataclass
class IngestResult:
    accepted: bool
    duplicate: bool
    employee_name: str | None
    employee_id: int | None
    known_card: bool
    punch_id: int | None
    message: str


def authenticate_device(raw_token: str) -> DeviceAuth | None:
    """단말 토큰 -> 매장 컨텍스트. 권한은 '탭 전송' 하나뿐입니다."""
    if not raw_token:
        return None
    token_hash = hash_device_token(raw_token)
    with control_session() as s:
        row = s.execute(
            select(Device, Store)
            .join(Store, Store.id == Device.store_id)
            .where(Device.token_hash == token_hash, Device.revoked_at.is_(None))
        ).first()
        if row is None:
            return None
        device, store = row
        if not store.is_active:
            return None
        return DeviceAuth(
            device_id=device.id,
            device_name=device.name,
            store_ctx=StoreContext(
                store_id=store.id,
                slug=store.slug,
                name=store.name,
                timezone=store.timezone,
                role="device",
                settings=merged_settings(store.settings),
            ),
        )


def record_heartbeat(device_id: int, device_time: datetime | None, queue_depth: int = 0) -> float:
    """단말 시계 편차를 추적합니다. 틀어진 걸 **모르는 게** 더 위험합니다."""
    now = datetime.now(timezone.utc)
    skew = (device_time - now).total_seconds() if device_time else 0.0
    with control_session() as s:
        device = s.get(Device, device_id)
        if device:
            device.last_seen_at = now
            device.clock_skew_seconds = skew
            device.queue_depth = queue_depth
    return skew


def ingest_punch(
    db: Session,
    ctx: StoreContext,
    *,
    client_event_id: str,
    card_uid: str,
    tapped_at: datetime,
    device_name: str = "",
) -> IngestResult:
    """탭 하나를 받습니다. 이미 받은 이벤트면 조용히 성공 처리합니다."""
    existing = db.execute(
        select(PunchEvent).where(PunchEvent.client_event_id == client_event_id)
    ).scalar_one_or_none()
    if existing is not None:
        name = _employee_name(db, existing.employee_id)
        return IngestResult(
            accepted=True, duplicate=True, employee_name=name,
            employee_id=existing.employee_id, known_card=existing.employee_id is not None,
            punch_id=existing.id, message="이미 받은 기록입니다",
        )

    if tapped_at.tzinfo is None:
        tapped_at = tapped_at.replace(tzinfo=ctx.tz)

    employee = db.execute(
        select(Employee).where(
            Employee.card_uid == card_uid, Employee.is_active.is_(True)
        )
    ).scalar_one_or_none()

    punch = PunchEvent(
        client_event_id=client_event_id,
        card_uid=card_uid,
        employee_id=employee.id if employee else None,
        tapped_at=tapped_at,
        device_name=device_name,
    )
    # 세이브포인트 안에서 넣습니다. 배치로 여러 건이 오는데, 그중 하나가 중복이라고
    # 트랜잭션 전체를 되돌리면 같이 온 정상 기록까지 날아갑니다. 단말이 재전송을
    # 겹쳐 보내는 건 정상 상태이므로 여기서 흡수해야 합니다.
    try:
        with db.begin_nested():
            db.add(punch)
            db.flush()
    except IntegrityError:
        again = db.execute(
            select(PunchEvent).where(PunchEvent.client_event_id == client_event_id)
        ).scalar_one_or_none()
        return IngestResult(
            accepted=True, duplicate=True,
            employee_name=_employee_name(db, again.employee_id) if again else None,
            employee_id=again.employee_id if again else None,
            known_card=bool(again and again.employee_id),
            punch_id=again.id if again else None,
            message="이미 받은 기록입니다",
        )

    if employee is None:
        # 등록되지 않은 카드도 버리지 않고 남깁니다. 나중에 카드를 등록하면
        # 재계산으로 이 기록이 살아납니다.
        return IngestResult(
            accepted=True, duplicate=False, employee_name=None, employee_id=None,
            known_card=False, punch_id=punch.id, message="등록되지 않은 카드입니다",
        )

    # 오래 오프라인이던 단말이 몇 주치를 한 번에 올릴 수 있습니다. 기본 창을
    # 그대로 쓰면 그 탭들이 세션이 되지 못하고 사라진 것처럼 보입니다.
    recompute_employee(db, ctx, employee.id, lookback_days=_lookback_for(tapped_at))
    return IngestResult(
        accepted=True, duplicate=False, employee_name=employee.name,
        employee_id=employee.id, known_card=True, punch_id=punch.id,
        message=f"{employee.name}님 기록되었습니다",
    )


def _lookback_for(tapped_at: datetime) -> int:
    """이 탭을 포함할 만큼 창을 넓힙니다. 기본 창보다 좁아지지는 않습니다."""
    age_days = (datetime.now(timezone.utc) - tapped_at).days
    return max(DEFAULT_LOOKBACK_DAYS, age_days + 2)


def _employee_name(db: Session, employee_id: int | None) -> str | None:
    if employee_id is None:
        return None
    emp = db.get(Employee, employee_id)
    return emp.name if emp else None
