"""근무시간 계산과 지각 판정.

데스크톱 버전은 지각 판정이 네 군데 복붙돼 있었고 그중 한 곳만 기준이 달랐습니다
(59초 vs 60초). 여기 한 벌만 두고 전부 이걸 씁니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

#: 이 시간을 넘겨 찍으면 지각. 매장 설정으로 덮어쓸 수 있습니다.
DEFAULT_LATE_GRACE_SECONDS = 60


class RoundingMode(str, Enum):
    NONE = "none"                    # 실제 시각 그대로
    CHECKOUT_ONLY = "checkout_only"  # 퇴근만 반올림 (데스크톱 버전과 같은 동작)
    BOTH = "both"                    # 출근·퇴근 모두 반올림 (대칭)


@dataclass(frozen=True)
class RoundingPolicy:
    """정산용 시각 보정 정책.

    데스크톱 버전은 퇴근만 10분 단위로 반올림했습니다. 항상 직원에게 유리한
    방향이라 임금 문제는 없지만 비대칭이고, 화면 안내는 "5분 기준"이라 코드와
    달랐습니다. 여기서는 단위와 대상을 모두 드러내 설정으로 뺍니다.
    """

    unit_minutes: int = 10
    mode: RoundingMode = RoundingMode.CHECKOUT_ONLY

    def __post_init__(self) -> None:
        if self.unit_minutes < 1 or 60 % self.unit_minutes:
            raise ValueError(f"반올림 단위는 60의 약수여야 합니다: {self.unit_minutes}")

    @property
    def label(self) -> str:
        if self.mode is RoundingMode.NONE:
            return "보정 없음 (실제 시각 그대로)"
        target = "퇴근 시각만" if self.mode is RoundingMode.CHECKOUT_ONLY else "출근·퇴근 모두"
        return f"{target} {self.unit_minutes}분 단위 반올림"


def round_to_unit(dt: datetime, unit_minutes: int) -> datetime:
    """가장 가까운 `unit_minutes` 눈금으로 반올림. 정확히 절반이면 올림."""
    base = dt.replace(minute=0, second=0, microsecond=0)
    elapsed = (dt - base).total_seconds() / 60
    steps, rem = divmod(elapsed, unit_minutes)
    if rem >= unit_minutes / 2:
        steps += 1
    return base + timedelta(minutes=steps * unit_minutes)


def settled_minutes(
    check_in: datetime, check_out: datetime, policy: RoundingPolicy | None = None
) -> float:
    """정산 근무 분. 퇴근이 출근보다 이르면 0 을 돌려줍니다 (음수 근무 방지)."""
    policy = policy or RoundingPolicy()
    cin, cout = check_in, check_out
    if policy.mode is RoundingMode.CHECKOUT_ONLY:
        cout = round_to_unit(cout, policy.unit_minutes)
    elif policy.mode is RoundingMode.BOTH:
        cin = round_to_unit(cin, policy.unit_minutes)
        cout = round_to_unit(cout, policy.unit_minutes)
    return max(0.0, (cout - cin).total_seconds() / 60)


def actual_minutes(check_in: datetime, check_out: datetime) -> float:
    return max(0.0, (check_out - check_in).total_seconds() / 60)


def is_late(
    actual_check_in: datetime,
    expected_check_in: datetime | None,
    grace_seconds: int = DEFAULT_LATE_GRACE_SECONDS,
) -> bool:
    """근무표보다 `grace_seconds` 넘게 늦었으면 지각.

    근무표가 없으면 지각이 아닙니다 — 기준이 없는데 판정하면 안 됩니다.
    """
    if expected_check_in is None:
        return False
    return (actual_check_in - expected_check_in).total_seconds() > grace_seconds


def fmt_minutes(total_minutes: float) -> str:
    """330.0 -> '5시간 30분'"""
    total = int(round(total_minutes))
    sign = "-" if total < 0 else ""
    h, m = divmod(abs(total), 60)
    return f"{sign}{h}시간 {m}분"


def fmt_hours(total_minutes: float, digits: int = 1) -> str:
    """330.0 -> '5.5'  (근무표 합계처럼 숫자로 보여줄 때)"""
    text = f"{total_minutes / 60:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text
