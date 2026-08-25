"""매장 컨텍스트와 권한 확인.

라우터는 여기서 받은 `StoreContext` 만 신뢰합니다. 컨텍스트를 못 만들면
쿼리는 시작조차 하지 않습니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .config import schema_for
from .db import control_session
from .domain.schedule import HourThresholds
from .domain.sessions import PairingRules
from .domain.workhours import RoundingMode, RoundingPolicy
from .models.control import (
    DEFAULT_STORE_SETTINGS, ROLE_RANK, Store, User, UserStoreRole,
)


@dataclass(frozen=True)
class StoreContext:
    store_id: int
    slug: str
    name: str
    timezone: str
    role: str
    settings: dict

    @property
    def schema(self) -> str:
        return schema_for(self.slug)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def at_least(self, role: str) -> bool:
        return ROLE_RANK.get(self.role, -1) >= ROLE_RANK.get(role, 99)

    # ---- 정책을 도메인 객체로 변환 (고정값을 코드에 박지 않기 위한 통로) ----

    def pairing_rules(self) -> PairingRules:
        s = self.settings
        return PairingRules(
            min_interval_seconds=int(s["min_interval_seconds"]),
            max_session_hours=int(s["max_session_hours"]),
            business_day_cutoff_hour=int(s["business_day_cutoff_hour"]),
        )

    def rounding(self) -> RoundingPolicy:
        return RoundingPolicy(
            unit_minutes=int(self.settings["rounding_unit_minutes"]),
            mode=RoundingMode(self.settings["rounding_mode"]),
        )

    def thresholds(self) -> HourThresholds:
        return HourThresholds(
            weekly_short_hours=float(self.settings["weekly_short_hours"]),
            weekly_standard_hours=float(self.settings["weekly_standard_hours"]),
        )

    def late_grace_seconds(self) -> int:
        return int(self.settings["late_grace_seconds"])

    def open_close(self) -> tuple[time, time]:
        return (
            time.fromisoformat(self.settings["open_time"]),
            time.fromisoformat(self.settings["close_time"]),
        )

    def departments(self) -> tuple[str, ...]:
        return tuple(self.settings.get("departments") or ())


def merged_settings(raw: dict | None) -> dict:
    """저장된 설정에 기본값을 덮어씌웁니다. 새 설정 키가 생겨도 기존 매장이 깨지지 않습니다."""
    out = dict(DEFAULT_STORE_SETTINGS)
    out.update(raw or {})
    return out


def load_context(user_id: int, slug: str) -> StoreContext | None:
    """이 사용자가 이 매장에 접근할 수 있으면 컨텍스트를, 아니면 None."""
    with control_session() as s:
        row = s.execute(
            select(Store, UserStoreRole)
            .join(UserStoreRole, UserStoreRole.store_id == Store.id)
            .where(
                Store.slug == slug,
                Store.is_active.is_(True),
                UserStoreRole.user_id == user_id,
            )
        ).first()
        if row is None:
            return None
        store, role = row
        return StoreContext(
            store_id=store.id,
            slug=store.slug,
            name=store.name,
            timezone=store.timezone,
            role=role.role,
            settings=merged_settings(store.settings),
        )


def stores_for_user(user_id: int) -> list[tuple[Store, str]]:
    with control_session() as s:
        rows = s.execute(
            select(Store, UserStoreRole.role)
            .join(UserStoreRole, UserStoreRole.store_id == Store.id)
            .where(UserStoreRole.user_id == user_id, Store.is_active.is_(True))
            .order_by(Store.name)
        ).all()
        return [(r[0], r[1]) for r in rows]


def get_user(user_id: int) -> User | None:
    with control_session() as s:
        u = s.get(User, user_id)
        if u and u.is_active:
            s.expunge(u)
            return u
        return None
