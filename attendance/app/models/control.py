"""공용 스키마 — 매장 목록, 사람 계정, 권한, 단말.

**계정은 매장 스키마 안에 두지 않습니다.** 두 매장을 겸하는 점장이 계정을 두 개
갖게 되고 사장님은 매장 수만큼 로그인해야 하기 때문입니다. 사람은 공용,
운영 데이터는 매장별 — 이 선이 D4 의 핵심입니다.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ..config import CONTROL_SCHEMA


class ControlBase(DeclarativeBase):
    __table_args__ = {"schema": CONTROL_SCHEMA}


class Store(ControlBase):
    __tablename__ = "stores"
    __table_args__ = {"schema": CONTROL_SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(48), unique=True)   # -> store_<slug> 스키마
    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Seoul")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: 매장별 정책. 고정값으로 박지 않는 것들이 전부 여기 들어갑니다.
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    roles: Mapped[list["UserStoreRole"]] = relationship(back_populates="store")
    devices: Mapped[list["Device"]] = relationship(back_populates="store")


DEFAULT_STORE_SETTINGS: dict = {
    "business_day_cutoff_hour": 5,
    "min_interval_seconds": 60,
    "max_session_hours": 16,
    "late_grace_seconds": 60,
    "rounding_unit_minutes": 10,
    "rounding_mode": "checkout_only",
    "weekly_short_hours": 15.0,
    "weekly_standard_hours": 40.0,
    "open_time": "10:00",
    "close_time": "22:00",
    "departments": ["홀", "주방"],
    # 근무표 사진이 해외로 전송됩니다. 매장별로 켜고 끌 수 있어야 합니다 (D7 법규).
    "ocr_import_enabled": False,
}


class User(ControlBase):
    __tablename__ = "users"
    __table_args__ = {"schema": CONTROL_SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    roles: Mapped[list["UserStoreRole"]] = relationship(back_populates="user")


class UserStoreRole(ControlBase):
    """누가 어느 매장에 무슨 권한을 갖는가."""

    __tablename__ = "user_store_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "store_id", name="uq_user_store"),
        {"schema": CONTROL_SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(f"{CONTROL_SCHEMA}.users.id", ondelete="CASCADE"))
    store_id: Mapped[int] = mapped_column(ForeignKey(f"{CONTROL_SCHEMA}.stores.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))   # owner | manager | staff
    #: staff 인 경우 매장 스키마의 employees.id — 자기 기록만 보게 하는 데 씁니다.
    employee_ref: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped[User] = relationship(back_populates="roles")
    store: Mapped[Store] = relationship(back_populates="roles")


ROLE_OWNER, ROLE_MANAGER, ROLE_STAFF = "owner", "manager", "staff"
ROLE_RANK = {ROLE_STAFF: 0, ROLE_MANAGER: 1, ROLE_OWNER: 2}


class Device(ControlBase):
    """매장 단말. **사람 계정을 단말에 넣지 않습니다.**

    분실하면 이 토큰만 폐기하면 끝이고, 권한은 '탭 전송' 하나뿐이라
    털려도 흘러나갈 게 없습니다.
    """

    __tablename__ = "devices"
    __table_args__ = {"schema": CONTROL_SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey(f"{CONTROL_SCHEMA}.stores.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 단말 시계가 서버와 얼마나 어긋나 있는지 (설계 D2). 크게 벌어지면 경보 대상.
    clock_skew_seconds: Mapped[float] = mapped_column(default=0.0)
    queue_depth: Mapped[int] = mapped_column(Integer, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    store: Mapped[Store] = relationship(back_populates="devices")

    @property
    def is_online(self) -> bool:
        from datetime import timedelta, timezone
        if self.revoked_at or self.last_seen_at is None:
            return False
        return datetime.now(timezone.utc) - self.last_seen_at < timedelta(minutes=5)
