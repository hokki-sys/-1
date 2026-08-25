"""전 구간 통합 테스트.

핵심은 세 가지입니다.
  1. 매장 사이에 데이터가 새지 않는가 (D4)
  2. 단말이 보낸 탭이 서버에서 올바른 세션이 되는가 (D1·D2)
  3. 근무표는 발행해야만 지각 판정에 쓰이는가 (D8)
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.store import Employee, PunchEvent, WorkSession
from tests.conftest import OTHER_EMAIL, OTHER_PW, OWNER_EMAIL, STORE_A, STORE_B

KST = timezone(timedelta(hours=9))


def ev() -> str:
    return uuid.uuid4().hex


def tap(client, token, card, when, event_id=None):
    return client.post(
        "/api/punches",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "punches": [{
                "client_event_id": event_id or ev(),
                "card_uid": card,
                "tapped_at": when.isoformat(),
            }],
            "device_time": datetime.now(timezone.utc).isoformat(),
            "queue_depth": 0,
        },
    )


def add_employee(client, slug, name, card, dept="홀"):
    r = client.post(f"/s/{slug}/employees/add",
                    data={"name": name, "card_uid": card, "department": dept,
                          "employee_type": "아르바이트"}, follow_redirects=False)
    assert r.status_code == 303, r.text


# ----------------------------------------------------------------- 인증·격리

def test_anonymous_is_redirected_to_login(client):
    r = client.get(f"/s/{STORE_A}/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_login_rejects_wrong_password(client):
    r = client.post("/login", data={"email": OWNER_EMAIL, "password": "nope", "next": "/"})
    assert r.status_code == 401
    assert "맞지 않습니다" in r.text


def test_outsider_cannot_reach_a_store_they_have_no_role_in(client):
    client.post("/login", data={"email": OTHER_EMAIL, "password": OTHER_PW, "next": "/"},
                follow_redirects=False)
    r = client.get(f"/s/{STORE_A}/", follow_redirects=False)
    # 권한 없음과 존재하지 않음을 구분해 알려주지 않습니다.
    assert r.status_code == 404


def test_owner_sees_both_stores(owner):
    assert owner.get(f"/s/{STORE_A}/").status_code == 200
    assert owner.get(f"/s/{STORE_B}/").status_code == 200


def test_unknown_store_is_404(owner):
    assert owner.get("/s/no_such_store/", follow_redirects=False).status_code == 404


# ------------------------------------------------------------- 단말 인증

def test_punch_requires_a_valid_device_token(client):
    r = tap(client, "not-a-real-token", "1000000001", datetime.now(timezone.utc))
    assert r.status_code == 401


def test_revoked_device_is_rejected(owner, device_token):
    from app.db import control_session
    from app.models.control import Device
    from app.security import hash_device_token

    assert tap(owner, device_token, "1000000001", datetime.now(timezone.utc)).status_code == 200
    with control_session() as s:
        d = s.execute(
            select(Device).where(Device.token_hash == hash_device_token(device_token))
        ).scalar_one()
        d.revoked_at = datetime.now(timezone.utc)
    assert tap(owner, device_token, "1000000001", datetime.now(timezone.utc)).status_code == 401


# --------------------------------------------------- 수집: 멱등 · 미등록 카드

def test_ingest_is_idempotent(owner, device_token, store_db):
    add_employee(owner, STORE_A, "김호진", "2000000001")
    when = datetime(2026, 8, 25, 10, 0, tzinfo=KST)
    eid = ev()

    first = tap(owner, device_token, "2000000001", when, eid).json()["results"][0]
    second = tap(owner, device_token, "2000000001", when, eid).json()["results"][0]

    assert first["duplicate"] is False and first["employee_name"] == "김호진"
    assert second["duplicate"] is True          # 재전송해도 기록이 늘지 않습니다
    with store_db() as s:
        n = s.execute(
            select(PunchEvent).where(PunchEvent.card_uid == "2000000001")
        ).scalars().all()
        assert len(n) == 1


def test_unknown_card_is_kept_and_claimed_on_registration(owner, device_token, store_db, ctx):
    """등록 전 탭도 버리지 않습니다. 카드를 등록하면 재계산으로 살아납니다."""
    card = "2000000099"
    base = datetime(2026, 7, 1, 9, 0, tzinfo=KST)
    r = tap(owner, device_token, card, base).json()["results"][0]
    assert r["accepted"] is True and r["known_card"] is False
    tap(owner, device_token, card, base + timedelta(hours=8))

    with store_db() as s:
        assert s.execute(
            select(WorkSession).join(Employee, Employee.id == WorkSession.employee_id)
            .where(Employee.card_uid == card)
        ).first() is None

    add_employee(owner, STORE_A, "나중등록", card)

    with store_db() as s:
        emp = s.execute(select(Employee).where(Employee.card_uid == card)).scalar_one()
        sessions = s.execute(
            select(WorkSession).where(WorkSession.employee_id == emp.id)
        ).scalars().all()
        assert len(sessions) == 1
        assert sessions[0].status == "complete"
        assert (sessions[0].check_out - sessions[0].check_in) == timedelta(hours=8)


# ------------------------------------------------- 세션 파생 (D1·D2·D3)

def test_night_shift_pairs_across_midnight_end_to_end(owner, device_token, store_db):
    add_employee(owner, STORE_A, "야간이", "2000000002", dept="주방")
    tap(owner, device_token, "2000000002", datetime(2026, 7, 10, 22, 0, tzinfo=KST))
    tap(owner, device_token, "2000000002", datetime(2026, 7, 11, 2, 0, tzinfo=KST))

    with store_db() as s:
        emp = s.execute(select(Employee).where(Employee.card_uid == "2000000002")).scalar_one()
        rows = s.execute(
            select(WorkSession).where(WorkSession.employee_id == emp.id)
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "complete"
    # 영업일은 근무를 시작한 10일
    assert rows[0].business_date == date(2026, 7, 10)
    assert rows[0].check_out - rows[0].check_in == timedelta(hours=4)


def test_double_tap_does_not_create_a_two_second_shift(owner, device_token, store_db):
    add_employee(owner, STORE_A, "실수맨", "2000000003")
    base = datetime(2026, 7, 12, 9, 0, tzinfo=KST)
    tap(owner, device_token, "2000000003", base)
    tap(owner, device_token, "2000000003", base + timedelta(seconds=2))
    tap(owner, device_token, "2000000003", base + timedelta(hours=9))

    with store_db() as s:
        emp = s.execute(select(Employee).where(Employee.card_uid == "2000000003")).scalar_one()
        rows = s.execute(
            select(WorkSession).where(WorkSession.employee_id == emp.id)
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].check_out - rows[0].check_in == timedelta(hours=9)
    assert len(rows[0].ignored_punch_ids) == 1


def test_missing_checkout_is_flagged_not_auto_closed(owner, device_token, store_db):
    add_employee(owner, STORE_A, "퇴근안찍음", "2000000004")
    tap(owner, device_token, "2000000004", datetime(2026, 7, 13, 10, 0, tzinfo=KST))
    tap(owner, device_token, "2000000004", datetime(2026, 7, 15, 10, 0, tzinfo=KST))

    with store_db() as s:
        emp = s.execute(select(Employee).where(Employee.card_uid == "2000000004")).scalar_one()
        rows = s.execute(
            select(WorkSession).where(WorkSession.employee_id == emp.id)
            .order_by(WorkSession.check_in)
        ).scalars().all()
    assert rows[0].status == "missing_checkout"
    assert rows[0].check_out is None          # 임의로 마감하지 않습니다


def test_offline_backlog_keeps_the_tap_time_not_the_arrival_time(owner, device_token, store_db):
    """오프라인 큐에 오래 있다 올라온 탭도 찍힌 시각으로 기록됩니다 (D2)."""
    add_employee(owner, STORE_A, "오프라인", "2000000005")
    long_ago = datetime(2026, 7, 20, 11, 30, tzinfo=KST)
    tap(owner, device_token, "2000000005", long_ago)

    with store_db() as s:
        p = s.execute(
            select(PunchEvent).where(PunchEvent.card_uid == "2000000005")
        ).scalar_one()
        assert p.tapped_at.astimezone(KST) == long_ago
        assert p.received_at > p.tapped_at     # 도착은 나중, 기록은 찍힌 시각


# ------------------------------------------------------ 매장 격리 (D4)

def test_stores_do_not_see_each_other(owner, device_token, store_db):
    add_employee(owner, STORE_A, "알파직원", "3000000001")
    add_employee(owner, STORE_B, "베타직원", "3000000001")   # 같은 카드 번호, 다른 매장

    tap(owner, device_token, "3000000001", datetime(2026, 7, 25, 9, 0, tzinfo=KST))

    with store_db(STORE_A) as s:
        names = list(s.execute(select(Employee.name)).scalars())
        assert "알파직원" in names and "베타직원" not in names
        assert s.execute(select(PunchEvent)).scalars().all()

    with store_db(STORE_B) as s:
        names = list(s.execute(select(Employee.name)).scalars())
        assert "베타직원" in names and "알파직원" not in names
        # A 매장 단말이 보낸 탭은 B 매장에 존재하지 않아야 합니다.
        assert s.execute(select(PunchEvent)).scalars().all() == []


def test_search_path_hides_other_schemas_entirely(store_db):
    """다른 매장 테이블은 이름으로도 닿지 않습니다."""
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError

    with store_db(STORE_A) as s:
        assert s.execute(text("SELECT current_schema()")).scalar() == f"store_{STORE_A}"
        with pytest.raises(ProgrammingError):
            s.execute(text("SELECT * FROM employees_of_another_store"))


def test_recompute_keeps_every_day_not_just_the_last(owner, device_token, store_db, ctx):
    """재계산 창을 안전하게 자르되, 앞 기록을 통째로 버리면 안 됩니다.

    야간 공백(퇴근~다음날 출근)은 최대 근무시간보다 길어서 전부 '안전 경계'로
    보입니다. 경계를 뒤에서부터 찾으면 마지막 하루만 남고 나머지가 사라집니다.
    """
    add_employee(owner, STORE_A, "연속근무", "5000000001")
    days = 8
    for d in range(days):
        day = datetime(2026, 6, 1, tzinfo=KST) + timedelta(days=d)
        tap(owner, device_token, "5000000001", day.replace(hour=17))
        tap(owner, device_token, "5000000001", day.replace(hour=22))

    with store_db() as s:
        emp = s.execute(select(Employee).where(Employee.card_uid == "5000000001")).scalar_one()
        rows = s.execute(
            select(WorkSession).where(WorkSession.employee_id == emp.id)
        ).scalars().all()
    assert len(rows) == days, f"{days}일치가 나와야 하는데 {len(rows)}일치만 계산됐습니다"
    assert all(r.status == "complete" for r in rows)
    assert all(r.check_out - r.check_in == timedelta(hours=5) for r in rows)


def test_recompute_is_idempotent(owner, device_token, store_db, ctx):
    """같은 원본으로 몇 번을 다시 계산해도 결과가 같아야 합니다."""
    from app.services.sessions import recompute_employee

    add_employee(owner, STORE_A, "재계산", "5000000002")
    for d in range(4):
        day = datetime(2026, 6, 20, tzinfo=KST) + timedelta(days=d)
        tap(owner, device_token, "5000000002", day.replace(hour=10))
        tap(owner, device_token, "5000000002", day.replace(hour=19))

    def snapshot():
        with store_db() as s:
            emp = s.execute(
                select(Employee).where(Employee.card_uid == "5000000002")
            ).scalar_one()
            return sorted(
                (r.business_date, r.check_in, r.check_out, r.status)
                for r in s.execute(
                    select(WorkSession).where(WorkSession.employee_id == emp.id)
                ).scalars()
            )

    first = snapshot()
    assert len(first) == 4
    with store_db() as s:
        emp = s.execute(select(Employee).where(Employee.card_uid == "5000000002")).scalar_one()
        recompute_employee(s, ctx, emp.id, lookback_days=365)
        recompute_employee(s, ctx, emp.id, lookback_days=365)
    assert snapshot() == first


def test_duplicate_in_a_batch_does_not_lose_the_others(owner, device_token, store_db):
    """단말이 재전송을 겹쳐 보내면 한 배치 안에 중복이 섞입니다.

    그때 중복 하나 때문에 트랜잭션 전체가 되돌아가면, 같이 온 정상 기록까지
    사라지고 단말은 500 을 받습니다. 멱등성은 바로 이 상황을 위한 것입니다.
    """
    add_employee(owner, STORE_A, "배치맨", "6000000001")
    shared = ev()
    base = datetime(2026, 6, 5, 9, 0, tzinfo=KST)

    first = owner.post("/api/punches", headers={"Authorization": f"Bearer {device_token}"},
                       json={"punches": [{"client_event_id": shared, "card_uid": "6000000001",
                                          "tapped_at": base.isoformat()}]})
    assert first.status_code == 200

    fresh_a, fresh_b = ev(), ev()
    r = owner.post("/api/punches", headers={"Authorization": f"Bearer {device_token}"},
                   json={"punches": [
                       {"client_event_id": shared, "card_uid": "6000000001",
                        "tapped_at": base.isoformat()},                       # 중복
                       {"client_event_id": fresh_a, "card_uid": "6000000001",
                        "tapped_at": (base + timedelta(hours=8)).isoformat()},
                       {"client_event_id": fresh_b, "card_uid": "6000000001",
                        "tapped_at": (base + timedelta(days=1)).isoformat()},
                   ]})
    assert r.status_code == 200, r.text
    results = {x["client_event_id"]: x for x in r.json()["results"]}
    assert results[shared]["duplicate"] is True
    assert results[fresh_a]["duplicate"] is False
    assert results[fresh_b]["duplicate"] is False

    with store_db() as s:
        emp = s.execute(select(Employee).where(Employee.card_uid == "6000000001")).scalar_one()
        punches = s.execute(
            select(PunchEvent).where(PunchEvent.employee_id == emp.id)
        ).scalars().all()
        assert len(punches) == 3          # 중복은 한 번만, 나머지는 전부 살아남음
        sessions = s.execute(
            select(WorkSession).where(WorkSession.employee_id == emp.id)
        ).scalars().all()
        # 두 번째 탭이 첫 근무를 닫고, 다음날 탭 하나가 열린 채로 남습니다.
        # 오래된 열린 세션은 자동 마감하지 않고 '퇴근 누락'으로 표시됩니다.
        assert sorted(x.status for x in sessions) == ["complete", "missing_checkout"]
