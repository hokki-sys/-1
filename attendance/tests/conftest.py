"""통합 테스트는 진짜 PostgreSQL 에 붙습니다.

스키마 분리(D4)가 이 설계의 핵심이라 SQLite 로 흉내 내면 검증이 안 됩니다.
매 실행마다 전용 테스트 매장 두 곳을 새로 만들고 끝나면 지웁니다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:5432/attendance"
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-do-not-use-in-production")
os.environ.setdefault("DEBUG", "true")

STORE_A, STORE_B = "test_alpha", "test_beta"
OWNER_EMAIL, OWNER_PW = "owner@test.local", "test-password-1234"
OTHER_EMAIL, OTHER_PW = "outsider@test.local", "test-password-5678"


def _alembic(*extra: str) -> None:
    rc = subprocess.call([sys.executable, "-m", "alembic", *extra], cwd=ROOT)
    assert rc == 0, f"alembic 실패: {extra}"


@pytest.fixture(scope="session", autouse=True)
def database():
    from sqlalchemy import text

    from app.config import schema_for
    from app.db import engine

    _alembic("-c", "alembic_control.ini", "upgrade", "head")
    for slug in (STORE_A, STORE_B):
        with engine.begin() as c:
            c.execute(text(f'DROP SCHEMA IF EXISTS "{schema_for(slug)}" CASCADE'))
    _seed()
    yield
    with engine.begin() as c:
        for slug in (STORE_A, STORE_B):
            c.execute(text(f'DROP SCHEMA IF EXISTS "{schema_for(slug)}" CASCADE'))


def _seed() -> None:
    from sqlalchemy import delete, select

    from app.db import control_session
    from app.models.control import Store, User, UserStoreRole

    with control_session() as s:
        ids = list(
            s.execute(select(Store.id).where(Store.slug.in_([STORE_A, STORE_B]))).scalars()
        )
        if ids:
            s.execute(delete(UserStoreRole).where(UserStoreRole.store_id.in_(ids)))
            s.execute(delete(Store).where(Store.id.in_(ids)))
        s.execute(delete(User).where(User.email.in_([OWNER_EMAIL, OTHER_EMAIL])))

    prov = ROOT / "tools" / "provision_store.py"
    for slug, name in ((STORE_A, "알파점"), (STORE_B, "베타점")):
        rc = subprocess.call(
            [sys.executable, str(prov), "--slug", slug, "--name", name,
             "--owner-email", OWNER_EMAIL, "--owner-name", "사장",
             "--owner-password", OWNER_PW],
            cwd=ROOT,
        )
        assert rc == 0, f"프로비저닝 실패: {slug}"

    # 두 매장 어디에도 속하지 않은 사용자 — 격리 검증용
    from app.models.control import User as U
    from app.security import hash_password
    with control_session() as s:
        s.add(U(email=OTHER_EMAIL, name="외부인", password_hash=hash_password(OTHER_PW)))


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def owner(client):
    r = client.post(
        "/login",
        data={"email": OWNER_EMAIL, "password": OWNER_PW, "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return client


@pytest.fixture()
def store_db():
    from contextlib import contextmanager

    from app.db import store_session

    @contextmanager
    def _open(slug: str = STORE_A):
        with store_session(slug) as s:
            yield s

    return _open


@pytest.fixture()
def ctx():
    from sqlalchemy import select

    from app.db import control_session
    from app.models.control import User
    from app.tenancy import load_context

    with control_session() as s:
        uid = s.execute(select(User.id).where(User.email == OWNER_EMAIL)).scalar_one()
    return load_context(uid, STORE_A)


@pytest.fixture()
def device_token(owner):
    """매장 A 에 단말 하나를 만들고 평문 토큰을 돌려줍니다."""
    r = owner.post(f"/s/{STORE_A}/devices/add", data={"name": "테스트 리더기"},
                   follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].split("new_token=")[1]
