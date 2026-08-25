"""탭 이벤트 -> 근무 세션 파생 (설계 D1).

단말은 "출근인지 퇴근인지" 판정하지 않습니다. 카드가 찍힌 사실만 보내고,
짝 맞추기는 여기서 합니다. 그래서 이 함수는 **순수 함수**입니다 —
같은 입력이면 언제 몇 번을 돌려도 같은 결과가 나오고, 규칙을 고치면
과거 데이터를 통째로 다시 계산할 수 있습니다.

이 한 파일이 데스크톱 버전의 세 가지 결함을 대체합니다.
  * 자정을 넘기면 퇴근이 새 출근이 되던 문제 (날짜로 찾지 않고 스트림으로 잇습니다)
  * 실수로 두 번 찍으면 2초짜리 근무가 남던 문제 (min_interval 로 무시합니다)
  * 퇴근 누락 기록이 조용히 사라지던 문제 (MISSING_CHECKOUT 로 남겨 사람이 봅니다)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Iterable, Sequence

from .business_day import DEFAULT_CUTOFF_HOUR, business_date


class SessionStatus(str, Enum):
    OPEN = "open"                        # 출근만 찍힘 — 아직 근무 중
    COMPLETE = "complete"                # 출퇴근 모두 정상
    MISSING_CHECKOUT = "missing_checkout"  # 퇴근을 안 찍고 감 — 사람이 확인해야 함


@dataclass(frozen=True)
class Punch:
    """단말이 보낸 원본 탭 하나. 절대 수정하지 않습니다."""

    id: int
    employee_id: int
    tapped_at: datetime          # 단말이 실제로 찍힌 시각 (설계 D2)
    device_id: str = ""


@dataclass
class DerivedSession:
    employee_id: int
    business_date: date
    check_in: datetime
    check_out: datetime | None
    status: SessionStatus
    check_in_punch_id: int
    check_out_punch_id: int | None = None
    ignored_punch_ids: list[int] = field(default_factory=list)

    @property
    def minutes(self) -> float:
        if self.check_out is None:
            return 0.0
        return (self.check_out - self.check_in).total_seconds() / 60


@dataclass(frozen=True)
class PairingRules:
    #: 이 간격 안에 다시 찍힌 탭은 같은 동작의 반복으로 보고 버립니다.
    min_interval_seconds: int = 60
    #: 이 시간을 넘겨도 퇴근이 안 찍히면 자동으로 닫지 않고 "퇴근 누락"으로 남깁니다.
    max_session_hours: int = 16
    #: 영업일 경계 (설계 D3)
    business_day_cutoff_hour: int = DEFAULT_CUTOFF_HOUR


def derive_sessions(
    punches: Iterable[Punch],
    rules: PairingRules | None = None,
    now: datetime | None = None,
) -> list[DerivedSession]:
    """직원 한 명 이상의 탭 스트림에서 근무 세션을 만듭니다.

    `now` 는 마지막 열린 세션이 OPEN(근무 중)인지 MISSING_CHECKOUT(퇴근 누락)인지
    가르는 데만 씁니다. 넘기지 않으면 마지막 세션은 항상 OPEN 으로 둡니다.
    """
    rules = rules or PairingRules()
    by_employee: dict[int, list[Punch]] = {}
    for p in punches:
        by_employee.setdefault(p.employee_id, []).append(p)

    out: list[DerivedSession] = []
    for employee_id in sorted(by_employee):
        out.extend(_derive_one(by_employee[employee_id], rules, now))
    out.sort(key=lambda s: (s.check_in, s.employee_id))
    return out


def _derive_one(
    punches: Sequence[Punch], rules: PairingRules, now: datetime | None
) -> list[DerivedSession]:
    ordered = sorted(punches, key=lambda p: (p.tapped_at, p.id))
    max_span = timedelta(hours=rules.max_session_hours)
    min_gap = timedelta(seconds=rules.min_interval_seconds)

    sessions: list[DerivedSession] = []
    open_session: DerivedSession | None = None
    last_accepted_at: datetime | None = None
    pending_ignored: list[int] = []

    for p in ordered:
        # 중복 탭: 직전에 받아들인 탭과 너무 가까우면 버립니다.
        if last_accepted_at is not None and p.tapped_at - last_accepted_at < min_gap:
            (open_session.ignored_punch_ids if open_session else pending_ignored).append(p.id)
            continue
        last_accepted_at = p.tapped_at

        if open_session is None:
            open_session = _open(p, rules, pending_ignored)
            pending_ignored = []
        elif p.tapped_at - open_session.check_in > max_span:
            # 너무 오래된 세션은 이 탭으로 닫지 않습니다. 임의 마감은 급여 분쟁의 씨앗입니다.
            open_session.status = SessionStatus.MISSING_CHECKOUT
            sessions.append(open_session)
            open_session = _open(p, rules, [])
        else:
            open_session.check_out = p.tapped_at
            open_session.check_out_punch_id = p.id
            open_session.status = SessionStatus.COMPLETE
            sessions.append(open_session)
            open_session = None

    if open_session is not None:
        too_old = now is not None and now - open_session.check_in > max_span
        open_session.status = (
            SessionStatus.MISSING_CHECKOUT if too_old else SessionStatus.OPEN
        )
        sessions.append(open_session)
    elif pending_ignored:
        # 버려진 탭만 남는 경우는 없지만, 있어도 잃어버리지 않게 마지막 세션에 붙입니다.
        if sessions:
            sessions[-1].ignored_punch_ids.extend(pending_ignored)

    return sessions


def _open(p: Punch, rules: PairingRules, ignored: list[int]) -> DerivedSession:
    return DerivedSession(
        employee_id=p.employee_id,
        business_date=business_date(p.tapped_at, rules.business_day_cutoff_hour),
        check_in=p.tapped_at,
        check_out=None,
        status=SessionStatus.OPEN,
        check_in_punch_id=p.id,
        ignored_punch_ids=list(ignored),
    )
