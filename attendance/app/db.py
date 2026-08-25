"""매장별 스키마 라우팅 (설계 D4).

요청이 들어오면 **제일 앞에서** 매장을 확정하고 그 트랜잭션의 `search_path` 를
고정합니다. 그 뒤로는 쿼리에 store_id 를 쓸 일이 없으니, 빠뜨려서 남의 매장
데이터가 새는 사고 자체가 성립하지 않습니다.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import CONTROL_SCHEMA, get_settings, schema_for

_SAFE_SCHEMA = re.compile(r"^[a-z][a-z0-9_]{0,48}$")

_settings = get_settings()
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
    echo=False,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _assert_safe(schema: str) -> str:
    """스키마 이름은 바인딩 파라미터로 못 넣으므로 화이트리스트 검증을 거칩니다."""
    if not _SAFE_SCHEMA.match(schema):
        raise ValueError(f"허용되지 않는 스키마 이름: {schema!r}")
    return schema


@contextmanager
def control_session() -> Iterator[Session]:
    """공용(컨트롤 플레인) 세션 — 매장 목록, 계정, 권한, 단말."""
    with SessionLocal() as s:
        s.execute(text(f"SET LOCAL search_path TO {_assert_safe(CONTROL_SCHEMA)}"))
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise


@contextmanager
def store_session(slug: str) -> Iterator[Session]:
    """매장 세션 — 이 트랜잭션에서는 그 매장 스키마만 보입니다.

    search_path 에 public 을 넣지 않는 것이 핵심입니다. 다른 매장 테이블은
    이름으로도 닿지 않습니다.
    """
    schema = _assert_safe(schema_for(slug))
    with SessionLocal() as s:
        s.execute(text(f"SET LOCAL search_path TO {schema}"))
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise


def schema_exists(schema: str) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"
                ),
                {"s": schema},
            ).scalar()
        )
