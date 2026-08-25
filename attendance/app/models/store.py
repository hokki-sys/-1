"""매장 스키마 — 매장마다 이 테이블 한 벌씩.

스키마 이름을 모델에 박지 않습니다. 세션이 `search_path` 를 고정하므로
같은 매핑이 매장마다 다른 스키마를 가리킵니다.
"""
from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, LargeBinary,
    String, Text, Time, UniqueConstraint, func, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class StoreBase(DeclarativeBase):
    pass


class Employee(StoreBase):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    card_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    employee_type: Mapped[str] = mapped_column(String(20), default="아르바이트")
    department: Mapped[str] = mapped_column(String(40), default="")
    #: 물리 삭제하지 않습니다. 근로 관련 기록은 보존 의무가 있고, 직원을 지우면
    #: 급여 근거가 통째로 사라지던 것이 데스크톱 버전 F-16 이었습니다.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # 활성 직원 사이에서만 카드가 유일하면 됩니다.
        # 퇴사자가 쓰던 카드는 새 직원에게 재발급할 수 있어야 하고, 카드가 아직
        # 없는 직원(NULL)은 여러 명이어도 됩니다.
        Index(
            "uq_employee_active_card",
            "card_uid",
            unique=True,
            postgresql_where=text("is_active AND card_uid IS NOT NULL"),
        ),
    )


class PunchEvent(StoreBase):
    """단말이 보낸 원본 탭. **절대 수정하지 않습니다** (설계 D1).

    수정이 필요하면 adjustments 에 새 레코드를 쌓습니다.
    """

    __tablename__ = "punch_events"
    __table_args__ = (
        # 멱등 키. 네트워크가 불안정할수록 중복 전송은 정상 상태입니다.
        UniqueConstraint("client_event_id", name="uq_punch_client_event"),
        Index("ix_punch_tapped_at", "tapped_at"),
        Index("ix_punch_emp_tapped", "employee_id", "tapped_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_event_id: Mapped[str] = mapped_column(String(64))
    card_uid: Mapped[str] = mapped_column(String(64))
    #: 등록되지 않은 카드도 일단 받습니다. 나중에 카드를 등록하면 재계산으로 살아납니다.
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    #: 단말이 실제로 찍힌 시각. 급여 계산의 근거입니다 (설계 D2).
    tapped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: 서버 도착 시각. 감사와 지연 추적용이며 근무시간 계산에는 쓰지 않습니다.
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    device_name: Mapped[str] = mapped_column(String(80), default="")


class WorkSession(StoreBase):
    """탭에서 파생된 근무 세션. 원본이 바뀌면 통째로 다시 만듭니다."""

    __tablename__ = "work_sessions"
    __table_args__ = (
        Index("ix_session_bdate", "business_date"),
        Index("ix_session_emp_bdate", "employee_id", "business_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    business_date: Mapped[date] = mapped_column(Date)
    check_in: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    check_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20))   # open | complete | missing_checkout
    check_in_punch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    check_out_punch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ignored_punch_ids: Mapped[list] = mapped_column(JSON, default=list)
    #: 관리자 보정이 적용된 세션인지. True 면 재계산이 덮어쓰지 않습니다.
    is_adjusted: Mapped[bool] = mapped_column(Boolean, default=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Adjustment(StoreBase):
    """관리자 보정. 원본 위에 덮어쓰지 않고 여기 쌓입니다.

    "누가 언제 무엇을 왜 고쳤나" 가 구조적으로 남습니다.
    """

    __tablename__ = "adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    business_date: Mapped[date] = mapped_column(Date)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(20))   # edit | add | void
    check_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    check_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ------------------------------------------------------------------ 근무표 (D8)

class ScheduleWeek(StoreBase):
    __tablename__ = "schedule_weeks"
    __table_args__ = (UniqueConstraint("week_start", name="uq_schedule_week"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_start: Mapped[date] = mapped_column(Date)   # 항상 월요일
    #: draft 는 지각 판정에 쓰이지 않습니다. published 만 기준이 됩니다.
    status: Mapped[str] = mapped_column(String(16), default="draft")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScheduleEntryRow(StoreBase):
    """근무표 한 칸. 하루에 여러 건 허용 — 하루 2탕이 조용히 덮어써지던 F-11 해소."""

    __tablename__ = "schedule_entries"
    __table_args__ = (Index("ix_sched_entry_date", "work_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_id: Mapped[int] = mapped_column(ForeignKey("schedule_weeks.id", ondelete="CASCADE"))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    work_date: Mapped[date] = mapped_column(Date)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    department: Mapped[str] = mapped_column(String(40), default="")


class ShiftPreset(StoreBase):
    """매장마다 쓰는 근무 패턴. 매번 10:00 을 타이핑하게 만들면 아무도 안 씁니다."""

    __tablename__ = "shift_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(40))
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class ScheduleRevision(StoreBase):
    """발행 후 변경 이력. 근무표가 바뀐 걸 직원이 몰라 지각으로 찍히면 안 됩니다."""

    __tablename__ = "schedule_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_id: Mapped[int] = mapped_column(ForeignKey("schedule_weeks.id", ondelete="CASCADE"))
    actor: Mapped[str] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(Text, default="")
    diff: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScheduleImport(StoreBase):
    """근무표 사진 가져오기 (설계 D7). 편집기가 생긴 뒤로는 신규 매장 온보딩용입니다."""

    __tablename__ = "schedule_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    image_data: Mapped[bytes] = mapped_column(LargeBinary)
    image_mime: Mapped[str] = mapped_column(String(40), default="image/jpeg")
    provider: Mapped[str] = mapped_column(String(40), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    tiles: Mapped[int] = mapped_column(Integer, default=1)
    #: AI 원본 응답을 그대로 보관합니다. 인식 품질을 나중에 검증할 근거가 됩니다.
    raw_response: Mapped[str] = mapped_column(Text, default="")
    parsed: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|done|failed
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(StoreBase):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(60))
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
