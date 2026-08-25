"""탭 -> 근무 세션 재계산.

파생 데이터는 언제든 원본에서 다시 만들 수 있어야 합니다 (설계 D1). 규칙을
고치면 과거 데이터도 다시 계산되고, 그게 이 모듈이 존재하는 이유입니다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..domain.sessions import Punch, derive_sessions
from ..models.store import Employee, PunchEvent, WorkSession
from ..tenancy import StoreContext

#: 재계산할 때 거슬러 올라가는 기본 기간. 실제 시작점은 아래에서 "안전 경계"로 보정합니다.
DEFAULT_LOOKBACK_DAYS = 60


def recompute_employee(
    db: Session,
    ctx: StoreContext,
    employee_id: int,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    now: datetime | None = None,
) -> int:
    """직원 한 명의 세션을 다시 만듭니다. 만들어진 세션 수를 돌려줍니다."""
    rules = ctx.pairing_rules()
    tz = ctx.tz
    now = now or datetime.now(timezone.utc)

    window_from = now - timedelta(days=lookback_days)
    rows, truncated = _load_punches(db, employee_id, window_from)
    if not rows:
        return 0

    start = _safe_start_index(rows, rules.max_session_hours, truncated)
    if start < 0:
        # 잘린 창 안에서 '반드시 출근'인 지점을 못 찾았습니다. 여기서 아무 데서나
        # 시작하면 출근/퇴근이 통째로 한 칸씩 밀립니다. 전체 이력을 읽습니다.
        rows, truncated = _load_punches(db, employee_id, None)
        start = 0
    rows = rows[start:]
    if not rows:
        return 0

    punches = [
        Punch(
            id=r.id,
            employee_id=employee_id,
            tapped_at=r.tapped_at.astimezone(tz).replace(tzinfo=None),
            device_id=r.device_name,
        )
        for r in rows
    ]
    derived = derive_sessions(punches, rules, now=now.astimezone(tz).replace(tzinfo=None))

    # 관리자가 손댄 세션은 재계산이 덮어쓰지 않습니다. 보정은 사람의 판단이라
    # 규칙이 되돌리면 안 됩니다.
    window_start = rows[0].tapped_at
    protected = set(
        db.execute(
            select(WorkSession.id).where(
                WorkSession.employee_id == employee_id,
                WorkSession.check_in >= window_start,
                WorkSession.is_adjusted.is_(True),
            )
        ).scalars()
    )
    db.execute(
        delete(WorkSession).where(
            WorkSession.employee_id == employee_id,
            WorkSession.check_in >= window_start,
            WorkSession.is_adjusted.is_(False),
        )
    )
    protected_dates = set(
        db.execute(
            select(WorkSession.business_date).where(WorkSession.id.in_(protected))
        ).scalars()
    ) if protected else set()

    created = 0
    for s in derived:
        if s.business_date in protected_dates:
            continue  # 그 영업일은 사람이 정리해 둔 상태를 존중합니다
        db.add(
            WorkSession(
                employee_id=s.employee_id,
                business_date=s.business_date,
                check_in=s.check_in.replace(tzinfo=tz),
                check_out=s.check_out.replace(tzinfo=tz) if s.check_out else None,
                status=s.status.value,
                check_in_punch_id=s.check_in_punch_id,
                check_out_punch_id=s.check_out_punch_id,
                ignored_punch_ids=s.ignored_punch_ids,
            )
        )
        created += 1
    db.flush()
    return created


def recompute_all(
    db: Session, ctx: StoreContext, lookback_days: int = DEFAULT_LOOKBACK_DAYS
) -> dict[int, int]:
    """매장 전체 재계산. 규칙(영업일 컷오프, 디바운스 등)을 바꾼 뒤에 씁니다."""
    ids = list(db.execute(select(Employee.id)).scalars())
    return {i: recompute_employee(db, ctx, i, lookback_days) for i in ids}


def _load_punches(
    db: Session, employee_id: int, window_from: datetime | None
) -> tuple[list[PunchEvent], bool]:
    """(창 안의 탭, 창 밖에 더 있는지)."""
    q = select(PunchEvent).where(PunchEvent.employee_id == employee_id)
    if window_from is not None:
        q = q.where(PunchEvent.tapped_at >= window_from)
    rows = list(db.execute(q.order_by(PunchEvent.tapped_at, PunchEvent.id)).scalars())
    if window_from is None:
        return rows, False
    truncated = db.execute(
        select(PunchEvent.id).where(
            PunchEvent.employee_id == employee_id,
            PunchEvent.tapped_at < window_from,
        ).limit(1)
    ).first() is not None
    return rows, truncated


def _safe_start_index(
    rows: list[PunchEvent], max_session_hours: int, truncated: bool
) -> int:
    """출근/퇴근 짝이 밀리지 않을 **가장 이른** 시작 지점. 없으면 -1.

    창을 잘라 계산할 때, 첫 탭이 사실은 창 밖에서 시작한 근무의 '퇴근'이면
    이후 전부가 한 칸씩 밀립니다. 반드시 '출근'인 게 보장된 지점부터 시작해야
    합니다.

    * 창 밖에 탭이 없으면(`truncated=False`) 0번이 그 직원의 첫 탭이므로
      정의상 출근입니다.
    * 잘렸다면, 최대 근무시간보다 긴 공백 **바로 뒤**의 탭은 반드시 출근입니다.
      앞에서부터 훑어 처음 만나는 지점을 씁니다 — 뒤에서부터 찾으면 마지막
      공백을 잡아 그 앞 기록을 통째로 버리게 됩니다.
    * 그런 공백이 하나도 없으면 -1. 짐작하지 않고 호출자가 창을 넓힙니다.
    """
    if not truncated:
        return 0
    gap = timedelta(hours=max_session_hours)
    for i in range(1, len(rows)):
        if rows[i].tapped_at - rows[i - 1].tapped_at > gap:
            return i
    return -1
