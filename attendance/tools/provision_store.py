#!/usr/bin/env python3
"""매장 하나를 새로 만듭니다 — 공용 등록 + 전용 스키마 + 마이그레이션 + 기본 프리셋.

    python3 tools/provision_store.py --slug gangnam --name "강남점" \
        --owner-email owner@example.com --owner-name "사장님" --owner-password "..."

이미 있는 매장에 사용자를 추가할 때는 --owner-* 만 다시 주면 됩니다.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.config import schema_for  # noqa: E402
from app.db import control_session, store_session  # noqa: E402
from app.models.control import (  # noqa: E402
    DEFAULT_STORE_SETTINGS, ROLE_OWNER, Store, User, UserStoreRole,
)
from app.models.store import ShiftPreset  # noqa: E402
from app.security import hash_password  # noqa: E402

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

DEFAULT_PRESETS = [
    ("오픈", time(10, 0), time(15, 0)),
    ("풀", time(10, 0), time(22, 0)),
    ("마감", time(17, 0), time(22, 0)),
]


def provision(slug: str, name: str, timezone: str) -> Store:
    if not SLUG_RE.match(slug):
        raise SystemExit(
            f"슬러그는 영소문자로 시작하는 2~41자 [a-z0-9_] 여야 합니다: {slug!r}"
        )
    with control_session() as s:
        store = s.execute(select(Store).where(Store.slug == slug)).scalar_one_or_none()
        if store is None:
            store = Store(
                slug=slug, name=name, timezone=timezone,
                settings=dict(DEFAULT_STORE_SETTINGS),
            )
            s.add(store)
            s.flush()
            print(f"매장 등록: {name} ({slug})")
        else:
            print(f"매장이 이미 있습니다: {store.name} ({slug})")
        s.expunge(store)
        return store


def migrate(slug: str) -> None:
    schema = schema_for(slug)
    print(f"스키마 마이그레이션: {schema}")
    rc = subprocess.call(
        [sys.executable, "-m", "alembic", "-c", "alembic_store.ini",
         "-x", f"schema={schema}", "upgrade", "head"],
        cwd=ROOT,
    )
    if rc:
        raise SystemExit(f"마이그레이션 실패: {schema}")


def seed_presets(slug: str) -> None:
    with store_session(slug) as s:
        if s.execute(select(ShiftPreset).limit(1)).first():
            return
        for i, (label, start, end) in enumerate(DEFAULT_PRESETS):
            s.add(ShiftPreset(label=label, start_time=start, end_time=end, sort_order=i))
        print(f"기본 근무 패턴 {len(DEFAULT_PRESETS)}개 등록")


def attach_user(store_id: int, email: str, name: str, password: str, role: str) -> None:
    with control_session() as s:
        user = s.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            user = User(email=email, name=name, password_hash=hash_password(password))
            s.add(user)
            s.flush()
            print(f"계정 생성: {email}")
        else:
            print(f"계정이 이미 있습니다: {email}")
        link = s.execute(
            select(UserStoreRole).where(
                UserStoreRole.user_id == user.id, UserStoreRole.store_id == store_id
            )
        ).scalar_one_or_none()
        if link is None:
            s.add(UserStoreRole(user_id=user.id, store_id=store_id, role=role))
            print(f"권한 부여: {email} -> {role}")
        elif link.role != role:
            link.role = role
            print(f"권한 변경: {email} -> {role}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True, help="스키마 이름이 됩니다 (store_<slug>)")
    ap.add_argument("--name", help="화면에 보일 매장 이름")
    ap.add_argument("--timezone", default="Asia/Seoul")
    ap.add_argument("--owner-email")
    ap.add_argument("--owner-name", default="")
    ap.add_argument("--owner-password")
    ap.add_argument("--role", default=ROLE_OWNER, choices=["owner", "manager", "staff"])
    a = ap.parse_args()

    store = provision(a.slug, a.name or a.slug, a.timezone)
    migrate(a.slug)
    seed_presets(a.slug)
    if a.owner_email:
        if not a.owner_password:
            raise SystemExit("--owner-password 가 필요합니다")
        attach_user(store.id, a.owner_email, a.owner_name or a.owner_email,
                    a.owner_password, a.role)
    print(f"\n완료. 접속: /s/{a.slug}/")


if __name__ == "__main__":
    main()
