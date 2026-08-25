#!/usr/bin/env python3
"""데모 데이터. 화면을 실제 모양으로 보고 싶을 때 씁니다.

    python3 tools/seed_demo.py --slug gangnam

직원 6명, 지난 2주 출퇴근 기록, 이번 주 근무표(발행)를 만듭니다.
"""
from __future__ import annotations

import argparse
import random
import sys
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select  # noqa: E402

from app.db import control_session, store_session  # noqa: E402
from app.domain.business_day import week_start  # noqa: E402
from app.models.control import Store  # noqa: E402
from app.models.store import (  # noqa: E402
    Employee, PunchEvent, ScheduleEntryRow, ScheduleWeek, WorkSession,
)
from app.services.schedules import publish  # noqa: E402
from app.services.sessions import recompute_employee  # noqa: E402
from app.tenancy import load_context  # noqa: E402

PEOPLE = [
    ("김호진", "1000000001", "홀", "정직원", time(10, 0), time(22, 0), [0, 1, 3, 4, 5]),
    ("이서연", "1000000002", "홀", "아르바이트", time(17, 0), time(22, 0), [1, 2, 4]),
    ("최민지", "1000000003", "홀", "아르바이트", time(10, 0), time(15, 0), [0, 1, 2, 3, 4]),
    ("과장", "1000000004", "주방", "정직원", time(10, 0), time(22, 0), [0, 1, 2, 3, 4, 5]),
    ("박지훈", "1000000005", "주방", "아르바이트", time(17, 0), time(22, 0), [2, 3, 4, 5, 6]),
    ("정다은", "1000000006", "주방", "아르바이트", time(12, 0), time(22, 0), [5, 6]),
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--weeks", type=int, default=2, help="며칠치 기록을 만들지")
    a = ap.parse_args()

    with control_session() as s:
        store = s.execute(select(Store).where(Store.slug == a.slug)).scalar_one_or_none()
        if store is None:
            raise SystemExit(f"매장이 없습니다: {a.slug}. 먼저 provision_store.py 로 만드세요.")
        uid = store.roles[0].user_id if store.roles else None
    if uid is None:
        raise SystemExit("이 매장에 연결된 계정이 없습니다")

    ctx = load_context(uid, a.slug)
    tz = ZoneInfo(store.timezone)
    rng = random.Random(20260825)
    monday = week_start(date.today())

    with store_session(a.slug) as s:
        for table in (WorkSession, PunchEvent, ScheduleEntryRow, ScheduleWeek, Employee):
            s.execute(delete(table))
        s.flush()

        emps = {}
        for name, card, dept, kind, start, end, days in PEOPLE:
            e = Employee(name=name, card_uid=card, department=dept, employee_type=kind)
            s.add(e)
            s.flush()
            emps[name] = e

        # 지난 주들의 실제 출퇴근 탭
        made = 0
        for w in range(a.weeks, 0, -1):
            base = monday - timedelta(days=7 * w)
            for name, card, dept, kind, start, end, days in PEOPLE:
                for d in days:
                    day = base + timedelta(days=d)
                    if day >= date.today():
                        continue
                    cin = datetime.combine(day, start, tz) + timedelta(
                        minutes=rng.choice([-6, -3, -1, 0, 1, 2, 4, 12])
                    )
                    cout = datetime.combine(day, end, tz) + timedelta(
                        minutes=rng.choice([-4, 0, 2, 3, 7])
                    )
                    if end <= start:
                        cout += timedelta(days=1)
                    for stamp in (cin, cout):
                        s.add(PunchEvent(client_event_id=uuid.uuid4().hex, card_uid=card,
                                         employee_id=emps[name].id, tapped_at=stamp,
                                         device_name="카운터 리더기"))
                        made += 1
                # 가끔 퇴근을 안 찍고 갑니다 — 확인 큐가 실제로 채워지게
                if rng.random() < 0.12 and days:
                    day = base + timedelta(days=days[0])
                    if day < date.today():
                        s.add(PunchEvent(
                            client_event_id=uuid.uuid4().hex, card_uid=card,
                            employee_id=emps[name].id,
                            tapped_at=datetime.combine(day, start, tz) - timedelta(days=3),
                            device_name="카운터 리더기"))
                        made += 1
        s.flush()
        for e in emps.values():
            recompute_employee(s, ctx, e.id, lookback_days=120)

        # 이번 주 근무표
        week = ScheduleWeek(week_start=monday, status="draft")
        s.add(week)
        s.flush()
        cells = 0
        for name, card, dept, kind, start, end, days in PEOPLE:
            for d in days:
                s.add(ScheduleEntryRow(week_id=week.id, employee_id=emps[name].id,
                                       work_date=monday + timedelta(days=d),
                                       start_time=start, end_time=end, department=dept))
                cells += 1
        s.flush()
        publish(s, monday, "seed@demo")

    print(f"데모 데이터 준비 완료 — 직원 {len(PEOPLE)}명 · 탭 {made}건 · 근무표 {cells}칸")
    print(f"접속: /s/{a.slug}/   근무표: /s/{a.slug}/schedule")


if __name__ == "__main__":
    main()
