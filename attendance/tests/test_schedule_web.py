"""근무표 편집기와 리포트 (D8).

가장 중요한 규칙: **발행된 근무표만 지각 판정의 기준이 됩니다.**
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.models.store import Employee, ScheduleWeek, WorkSession
from tests.conftest import STORE_A

KST = timezone(timedelta(hours=9))
MON = date(2026, 9, 7)          # 월요일


def add_employee(client, name, card, dept="홀"):
    r = client.post(f"/s/{STORE_A}/employees/add",
                    data={"name": name, "card_uid": card, "department": dept,
                          "employee_type": "아르바이트"}, follow_redirects=False)
    assert r.status_code == 303, r.text


def emp_id(store_db, card):
    with store_db() as s:
        return s.execute(select(Employee.id).where(Employee.card_uid == card)).scalar_one()


def save_grid(client, week, grid):
    return client.post(f"/s/{STORE_A}/schedule/save",
                       data={"week": week.isoformat(), "grid": json.dumps(grid)},
                       follow_redirects=False)


def tap(client, token, card, when):
    return client.post("/api/punches", headers={"Authorization": f"Bearer {token}"},
                       json={"punches": [{"client_event_id": uuid.uuid4().hex,
                                          "card_uid": card, "tapped_at": when.isoformat()}]})


def cell_values(html: str, employee_id: int) -> dict[str, str]:
    """격자에서 그 직원 행의 칸 값만 뽑습니다.

    프리셋 버튼에도 "10:00-22:00" 같은 문자열이 있어서, 페이지 전체에서
    찾으면 격자가 비어 있어도 통과해 버립니다.
    """
    import re
    out = {}
    for m in re.finditer(
        r'value="([^"]*)"[^>]*\s+data-key="' + str(employee_id) + r'\|([0-9-]+)"', html
    ):
        out[m.group(2)] = m.group(1)
    return out


def log_row(html: str, name: str) -> str:
    """기록 표에서 그 직원의 행만 뽑습니다. 검색창 value 에도 이름이 들어가므로
    <tbody> 이후만 봅니다."""
    body = html.split("<tbody>")[1]
    for chunk in body.split("<tr>"):
        if f">{name}" in chunk:
            return chunk
    return ""


# --------------------------------------------------------------- 편집기

def test_editor_renders_every_active_employee(owner, store_db):
    add_employee(owner, "격자직원", "4000000001")
    r = owner.get(f"/s/{STORE_A}/schedule?week={MON}")
    assert r.status_code == 200
    assert "격자직원" in r.text
    assert 'id="sched-grid"' in r.text


def test_save_then_reload_keeps_the_cells(owner, store_db):
    add_employee(owner, "저장맨", "4000000002")
    eid = emp_id(store_db, "4000000002")
    assert save_grid(owner, MON, {
        f"{eid}|{MON}": "10-22",
        f"{eid}|{MON + timedelta(days=1)}": "17:00-22:00",
    }).status_code == 303

    cells = cell_values(owner.get(f"/s/{STORE_A}/schedule?week={MON}").text, eid)
    assert cells[MON.isoformat()] == "10:00-22:00"
    assert cells[(MON + timedelta(days=1)).isoformat()] == "17:00-22:00"
    assert cells[(MON + timedelta(days=2)).isoformat()] == ""      # 비운 칸은 휴무


def test_two_shifts_on_one_day_are_both_kept(owner, store_db):
    """데스크톱 버전은 UNIQUE(직원,날짜) 때문에 앞 근무가 조용히 덮어써졌습니다 (F-11)."""
    add_employee(owner, "두탕이", "4000000003")
    eid = emp_id(store_db, "4000000003")
    save_grid(owner, MON, {f"{eid}|{MON}": "10-14 17-22"})

    from app.services.schedules import load_entries
    with store_db() as s:
        entries = [e for e in load_entries(s, MON) if e.employee_id == eid]
    assert len(entries) == 2
    assert sum(e.minutes for e in entries) == 4 * 60 + 5 * 60
    # 편집기로 되돌아왔을 때 한 칸에 두 근무가 그대로 보여야 합니다.
    cells = cell_values(owner.get(f"/s/{STORE_A}/schedule?week={MON}").text, eid)
    assert cells[MON.isoformat()] == "10:00-14:00 17:00-22:00"


def test_unparseable_cell_is_reported_not_silently_dropped(owner, store_db):
    add_employee(owner, "오타맨", "4000000004")
    eid = emp_id(store_db, "4000000004")
    r = save_grid(owner, MON, {f"{eid}|{MON}": "열시부터", f"{eid}|{MON + timedelta(days=1)}": "10-22"})
    assert "%EC%9D%BD%EC%A7%80" in r.headers["location"] or "읽지" in r.headers["location"]


def test_copy_previous_week(owner, store_db):
    add_employee(owner, "복사맨", "4000000005")
    eid = emp_id(store_db, "4000000005")
    prev = MON - timedelta(days=7)
    save_grid(owner, prev, {f"{eid}|{prev}": "10-22",
                            f"{eid}|{prev + timedelta(days=2)}": "10-22"})

    r = owner.post(f"/s/{STORE_A}/schedule/copy-previous",
                   data={"week": MON.isoformat()}, follow_redirects=False)
    assert r.status_code == 303

    from app.services.schedules import load_entries
    with store_db() as s:
        entries = [e for e in load_entries(s, MON) if e.employee_id == eid]
    assert {e.work_date for e in entries} == {MON, MON + timedelta(days=2)}


def test_weekly_total_and_threshold_warning(owner, store_db):
    add_employee(owner, "장시간", "4000000006", dept="주방")
    eid = emp_id(store_db, "4000000006")
    save_grid(owner, MON, {
        f"{eid}|{MON + timedelta(days=i)}": "10-22" for i in range(6)
    })  # 12시간 x 6일 = 72시간
    r = owner.get(f"/s/{STORE_A}/schedule?week={MON}")
    assert "72" in r.text
    assert "40시간을 넘습니다" in r.text


def test_coverage_gap_warning(owner, store_db):
    add_employee(owner, "늦게옴", "4000000007", dept="홀")
    eid = emp_id(store_db, "4000000007")
    save_grid(owner, MON, {f"{eid}|{MON}": "12-22"})   # 매장은 10시 오픈
    r = owner.get(f"/s/{STORE_A}/schedule?week={MON}")
    assert "배정된 사람이 없습니다" in r.text


def test_warnings_do_not_block_saving(owner, store_db):
    """경고는 알리기만 합니다. 막으면 시스템을 우회하게 됩니다."""
    add_employee(owner, "경고무시", "4000000008")
    eid = emp_id(store_db, "4000000008")
    r = save_grid(owner, MON, {f"{eid}|{MON + timedelta(days=i)}": "10-22" for i in range(7)})
    assert r.status_code == 303
    assert "error" not in r.headers["location"]


# ------------------------------------------------ 발행과 지각 판정

def test_draft_schedule_does_not_drive_lateness(owner, device_token, store_db):
    add_employee(owner, "초안지각", "4000000010")
    eid = emp_id(store_db, "4000000010")
    save_grid(owner, MON, {f"{eid}|{MON}": "10-22"})       # 초안 상태로 둡니다
    tap(owner, device_token, "4000000010", datetime(2026, 9, 7, 11, 0, tzinfo=KST))

    row = log_row(owner.get(f"/s/{STORE_A}/logs?start={MON}&end={MON}&q=초안지각").text, "초안지각")
    assert row, "기록이 조회되지 않았습니다"
    assert "지각</span>" not in row


def test_published_schedule_drives_lateness(owner, device_token, store_db):
    add_employee(owner, "발행지각", "4000000011")
    eid = emp_id(store_db, "4000000011")
    save_grid(owner, MON, {f"{eid}|{MON}": "10-22"})
    assert owner.post(f"/s/{STORE_A}/schedule/publish",
                      data={"week": MON.isoformat()}, follow_redirects=False).status_code == 303
    tap(owner, device_token, "4000000011", datetime(2026, 9, 7, 11, 0, tzinfo=KST))

    row = log_row(owner.get(f"/s/{STORE_A}/logs?start={MON}&end={MON}&q=발행지각").text, "발행지각")
    assert "지각</span>" in row


def test_on_time_within_grace_is_not_late(owner, device_token, store_db):
    add_employee(owner, "정시맨", "4000000012")
    eid = emp_id(store_db, "4000000012")
    save_grid(owner, MON, {f"{eid}|{MON}": "10-22"})
    owner.post(f"/s/{STORE_A}/schedule/publish", data={"week": MON.isoformat()},
               follow_redirects=False)
    tap(owner, device_token, "4000000012", datetime(2026, 9, 7, 10, 0, 45, tzinfo=KST))

    row = log_row(owner.get(f"/s/{STORE_A}/logs?start={MON}&end={MON}&q=정시맨").text, "정시맨")
    assert row and "지각</span>" not in row


def test_editing_a_published_week_records_a_revision(owner, store_db):
    add_employee(owner, "수정이력", "4000000013")
    eid = emp_id(store_db, "4000000013")
    save_grid(owner, MON, {f"{eid}|{MON}": "10-22"})
    owner.post(f"/s/{STORE_A}/schedule/publish", data={"week": MON.isoformat()},
               follow_redirects=False)
    save_grid(owner, MON, {f"{eid}|{MON}": "12-22"})

    r = owner.get(f"/s/{STORE_A}/schedule?week={MON}")
    assert "발행 후 변경 이력" in r.text
    assert "1건 변경" in r.text


def test_unpublish_stops_lateness_judgement(owner, store_db):
    add_employee(owner, "되돌림", "4000000014")
    eid = emp_id(store_db, "4000000014")
    save_grid(owner, MON, {f"{eid}|{MON}": "10-22"})
    owner.post(f"/s/{STORE_A}/schedule/publish", data={"week": MON.isoformat()},
               follow_redirects=False)
    owner.post(f"/s/{STORE_A}/schedule/unpublish", data={"week": MON.isoformat()},
               follow_redirects=False)
    with store_db() as s:
        week = s.execute(select(ScheduleWeek).where(ScheduleWeek.week_start == MON)).scalar_one()
    assert week.status == "draft"


def test_print_view_renders(owner, store_db):
    add_employee(owner, "인쇄맨", "4000000015")
    eid = emp_id(store_db, "4000000015")
    save_grid(owner, MON, {f"{eid}|{MON}": "10-22"})
    r = owner.get(f"/s/{STORE_A}/schedule/print?week={MON}")
    assert r.status_code == 200 and "인쇄맨" in r.text


# ------------------------------------------------------------- 리포트

def test_report_and_csv(owner, device_token, store_db):
    add_employee(owner, "리포트맨", "4000000020")
    day = datetime(2026, 9, 8, 10, 0, tzinfo=KST)
    tap(owner, device_token, "4000000020", day)
    tap(owner, device_token, "4000000020", day + timedelta(hours=11, minutes=56))

    r = owner.get(f"/s/{STORE_A}/reports?period=weekly&anchor={MON}")
    assert r.status_code == 200 and "리포트맨" in r.text
    assert "11시간 56분" in r.text        # 실근무
    assert "12시간 0분" in r.text         # 정산 (퇴근 10분 반올림)

    c = owner.get(f"/s/{STORE_A}/reports/export.csv?period=weekly&anchor={MON}")
    assert c.status_code == 200
    assert "리포트맨" in c.text and "정산 기준" in c.text


def test_adjustment_is_recorded_and_survives_recompute(owner, device_token, store_db, ctx):
    from app.services.sessions import recompute_employee

    add_employee(owner, "보정맨", "4000000021")
    tap(owner, device_token, "4000000021", datetime(2026, 9, 9, 10, 0, tzinfo=KST))
    eid = emp_id(store_db, "4000000021")

    with store_db() as s:
        ws = s.execute(select(WorkSession).where(WorkSession.employee_id == eid)).scalar_one()
        sid = ws.id
        assert ws.status in ("open", "missing_checkout")

    r = owner.post(f"/s/{STORE_A}/logs/{sid}/adjust",
                   data={"check_in": "2026-09-09 10:00", "check_out": "2026-09-09 19:00",
                         "reason": "퇴근 태그 누락, 본인 확인"}, follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]

    with store_db() as s:
        ws = s.get(WorkSession, sid)
        assert ws.is_adjusted is True and ws.status == "complete"
        # 재계산이 사람의 보정을 되돌리지 않아야 합니다.
        recompute_employee(s, ctx, eid)
        s.flush()
        ws = s.get(WorkSession, sid)
        assert ws is not None and ws.is_adjusted is True
        assert ws.check_out is not None


def test_adjustment_requires_a_reason(owner, device_token, store_db):
    add_employee(owner, "사유없음", "4000000022")
    tap(owner, device_token, "4000000022", datetime(2026, 9, 10, 10, 0, tzinfo=KST))
    eid = emp_id(store_db, "4000000022")
    with store_db() as s:
        sid = s.execute(select(WorkSession.id).where(WorkSession.employee_id == eid)).scalar_one()

    r = owner.post(f"/s/{STORE_A}/logs/{sid}/adjust",
                   data={"check_in": "2026-09-10 10:00", "check_out": "2026-09-10 19:00",
                         "reason": "  "}, follow_redirects=False)
    assert "error" in r.headers["location"]


def test_deactivated_employee_keeps_their_records(owner, device_token, store_db):
    """퇴사 처리해도 근무 기록은 남습니다 (F-16)."""
    add_employee(owner, "퇴사자", "4000000023")
    tap(owner, device_token, "4000000023", datetime(2026, 9, 11, 10, 0, tzinfo=KST))
    tap(owner, device_token, "4000000023", datetime(2026, 9, 11, 18, 0, tzinfo=KST))
    eid = emp_id(store_db, "4000000023")

    owner.post(f"/s/{STORE_A}/employees/{eid}/deactivate", follow_redirects=False)

    with store_db() as s:
        emp = s.get(Employee, eid)
        assert emp.is_active is False and emp.card_uid is None   # 카드는 회수
        rows = s.execute(select(WorkSession).where(WorkSession.employee_id == eid)).scalars().all()
        assert len(rows) == 1 and rows[0].check_out is not None  # 기록은 그대로


def test_dashboard_counts_people_not_rows(owner, device_token, store_db, ctx):
    """한 명이 두 번 찍어도 '출근 2명'이 되면 안 됩니다 (F-14)."""
    from app.services.reports import dashboard

    add_employee(owner, "두번찍음", "4000000024")
    base = datetime(2026, 9, 12, 9, 0, tzinfo=KST)
    for offset in (0, 4, 5, 9):     # 오전 근무 + 오후 근무
        tap(owner, device_token, "4000000024", base + timedelta(hours=offset))

    with store_db() as s:
        state = dashboard(s, ctx, date(2026, 9, 12))
    assert state.headcount == 1
    assert len(state.done_today) == 2
