"""순수 도메인 로직 테스트.

DB 없이 도는 부분이고, 급여 숫자가 여기서 나옵니다. 데스크톱 버전에서
실제로 터졌던 케이스를 그대로 회귀 테스트로 넣었습니다.
"""
from datetime import date, datetime, time

import pytest

from app.domain.business_day import business_date, month_range, week_range, week_start
from app.domain.schedule import (
    HourThresholds,
    ScheduleEntry,
    Severity,
    build_week,
    parse_range,
    parse_time,
    validate_week,
)
from app.domain.sessions import PairingRules, Punch, SessionStatus, derive_sessions
from app.domain.workhours import (
    RoundingMode,
    RoundingPolicy,
    actual_minutes,
    fmt_hours,
    fmt_minutes,
    is_late,
    round_to_unit,
    settled_minutes,
)


def dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def punches(*specs: tuple[int, str]) -> list[Punch]:
    return [
        Punch(id=i + 1, employee_id=emp, tapped_at=dt(ts))
        for i, (emp, ts) in enumerate(specs)
    ]


# --------------------------------------------------------------- 영업일 (D3)

@pytest.mark.parametrize(
    "stamp,expected",
    [
        ("2026-08-25 10:00:00", date(2026, 8, 25)),  # 낮
        ("2026-08-25 23:59:00", date(2026, 8, 25)),  # 자정 직전
        ("2026-08-26 01:30:00", date(2026, 8, 25)),  # 새벽 -> 전날 영업일
        ("2026-08-26 04:59:59", date(2026, 8, 25)),  # 컷오프 직전
        ("2026-08-26 05:00:00", date(2026, 8, 26)),  # 컷오프 -> 새 영업일
    ],
)
def test_business_date_crosses_midnight(stamp, expected):
    assert business_date(dt(stamp)) == expected


def test_business_date_cutoff_is_configurable():
    assert business_date(dt("2026-08-26 03:00:00"), cutoff_hour=0) == date(2026, 8, 26)
    assert business_date(dt("2026-08-26 03:00:00"), cutoff_hour=5) == date(2026, 8, 25)


def test_business_date_rejects_absurd_cutoff():
    with pytest.raises(ValueError):
        business_date(dt("2026-08-25 10:00:00"), cutoff_hour=23)


def test_week_and_month_helpers():
    assert week_start(date(2026, 8, 26)) == date(2026, 8, 24)      # 수요일 -> 월요일
    assert week_range(date(2026, 8, 24)) == (date(2026, 8, 24), date(2026, 8, 30))
    assert month_range(2026, 2) == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_range(2026, 12) == (date(2026, 12, 1), date(2026, 12, 31))


# --------------------------------------------------- 세션 파생 (D1) — 핵심

def test_simple_in_and_out():
    s = derive_sessions(punches((1, "2026-08-25 10:00:00"), (1, "2026-08-25 22:00:00")))
    assert len(s) == 1
    assert s[0].status is SessionStatus.COMPLETE
    assert s[0].minutes == 720
    assert s[0].business_date == date(2026, 8, 25)


def test_night_shift_pairs_across_midnight():
    """데스크톱 버전 F-05: 22시 출근 -> 새벽 2시 퇴근이 '새 출근'이 되던 버그."""
    s = derive_sessions(punches((1, "2026-08-25 22:00:00"), (1, "2026-08-26 02:00:00")))
    assert len(s) == 1
    assert s[0].status is SessionStatus.COMPLETE
    assert s[0].minutes == 240
    # 영업일은 근무를 시작한 25일로 잡힙니다.
    assert s[0].business_date == date(2026, 8, 25)


def test_double_tap_is_ignored():
    """데스크톱 버전 F-06: 2초 간격 재태그가 '2초 근무'로 마감되던 버그."""
    s = derive_sessions(
        punches(
            (1, "2026-08-25 09:00:00"),
            (1, "2026-08-25 09:00:02"),  # 실수로 한 번 더
            (1, "2026-08-25 09:00:04"),  # 또 한 번
            (1, "2026-08-25 18:00:00"),
        )
    )
    assert len(s) == 1
    assert s[0].status is SessionStatus.COMPLETE
    assert s[0].minutes == 540
    assert s[0].ignored_punch_ids == [2, 3]


def test_debounce_window_is_configurable():
    rules = PairingRules(min_interval_seconds=5)
    s = derive_sessions(
        punches((1, "2026-08-25 09:00:00"), (1, "2026-08-25 09:00:30")), rules
    )
    assert s[0].status is SessionStatus.COMPLETE  # 30초 > 5초 이므로 퇴근으로 인정


def test_missing_checkout_is_not_auto_closed():
    """퇴근을 안 찍고 감 -> 임의 마감하지 않고 사람이 확인하도록 남깁니다."""
    s = derive_sessions(
        punches((1, "2026-08-25 10:00:00"), (1, "2026-08-27 10:00:00"))
    )
    assert len(s) == 2
    assert s[0].status is SessionStatus.MISSING_CHECKOUT
    assert s[0].check_out is None
    assert s[0].minutes == 0
    assert s[1].status is SessionStatus.OPEN  # 두 번째는 아직 근무 중


def test_open_session_becomes_missing_checkout_when_stale():
    p = punches((1, "2026-08-25 10:00:00"))
    assert derive_sessions(p, now=dt("2026-08-25 14:00:00"))[0].status is SessionStatus.OPEN
    assert (
        derive_sessions(p, now=dt("2026-08-27 09:00:00"))[0].status
        is SessionStatus.MISSING_CHECKOUT
    )


def test_multiple_employees_are_independent():
    s = derive_sessions(
        punches(
            (1, "2026-08-25 10:00:00"),
            (2, "2026-08-25 10:00:30"),   # 다른 직원 — 디바운스에 걸리면 안 됩니다
            (1, "2026-08-25 18:00:00"),
            (2, "2026-08-25 20:00:00"),
        )
    )
    assert len(s) == 2
    assert {x.employee_id: x.minutes for x in s} == {1: 480, 2: 599.5}


def test_two_shifts_in_one_day():
    """오전 홀 + 저녁 주방처럼 하루 두 번 근무."""
    s = derive_sessions(
        punches(
            (1, "2026-08-25 10:00:00"),
            (1, "2026-08-25 14:00:00"),
            (1, "2026-08-25 17:00:00"),
            (1, "2026-08-25 22:00:00"),
        )
    )
    assert [x.minutes for x in s] == [240, 300]
    assert all(x.status is SessionStatus.COMPLETE for x in s)


def test_derivation_is_pure_and_repeatable():
    """같은 입력이면 몇 번을 돌려도 같은 결과 — 재계산 가능해야 합니다."""
    p = punches(
        (1, "2026-08-25 10:00:00"),
        (1, "2026-08-25 10:00:01"),
        (1, "2026-08-25 22:03:00"),
        (2, "2026-08-25 17:00:00"),
    )
    first = derive_sessions(p)
    assert derive_sessions(list(reversed(p))) == first  # 입력 순서도 무관


def test_out_of_order_punches_are_sorted():
    s = derive_sessions(punches((1, "2026-08-25 22:00:00"), (1, "2026-08-25 10:00:00")))
    assert s[0].check_in == dt("2026-08-25 10:00:00")
    assert s[0].check_out == dt("2026-08-25 22:00:00")


# ------------------------------------------------------- 근무시간 / 반올림

@pytest.mark.parametrize(
    "stamp,expected",
    [
        ("2026-08-25 22:00:10", "22:00"),
        ("2026-08-25 22:04:59", "22:00"),
        ("2026-08-25 22:05:00", "22:10"),  # 정확히 절반이면 올림
        ("2026-08-25 22:09:59", "22:10"),
        ("2026-08-25 21:59:00", "22:00"),
    ],
)
def test_round_to_unit_10min(stamp, expected):
    assert round_to_unit(dt(stamp), 10).strftime("%H:%M") == expected


def test_rounding_unit_must_divide_an_hour():
    with pytest.raises(ValueError):
        RoundingPolicy(unit_minutes=7)


def test_settled_vs_actual():
    cin, cout = dt("2026-08-25 10:00:00"), dt("2026-08-25 21:56:00")
    assert actual_minutes(cin, cout) == 716
    assert settled_minutes(cin, cout) == 720                      # 퇴근만 올림
    assert settled_minutes(cin, cout, RoundingPolicy(mode=RoundingMode.NONE)) == 716


def test_symmetric_rounding_option():
    cin, cout = dt("2026-08-25 09:56:00"), dt("2026-08-25 21:56:00")
    both = RoundingPolicy(mode=RoundingMode.BOTH)
    assert settled_minutes(cin, cout, both) == 720                # 10:00 ~ 22:00
    assert settled_minutes(cin, cout) == 724                      # 09:56 ~ 22:00


def test_negative_duration_clamps_to_zero():
    assert settled_minutes(dt("2026-08-25 18:00:00"), dt("2026-08-25 09:00:00")) == 0
    assert actual_minutes(dt("2026-08-25 18:00:00"), dt("2026-08-25 09:00:00")) == 0


def test_rounding_policy_label_is_honest():
    assert "10분 단위" in RoundingPolicy().label
    assert "퇴근 시각만" in RoundingPolicy().label


# ------------------------------------------------------------- 지각 판정

def test_is_late_uses_one_threshold_everywhere():
    expected = dt("2026-08-25 10:00:00")
    assert is_late(dt("2026-08-25 10:00:59"), expected) is False
    assert is_late(dt("2026-08-25 10:01:00"), expected) is False   # 정확히 60초는 세이프
    assert is_late(dt("2026-08-25 10:01:01"), expected) is True
    assert is_late(dt("2026-08-25 09:50:00"), expected) is False


def test_no_schedule_means_never_late():
    assert is_late(dt("2026-08-25 23:00:00"), None) is False


def test_formatting():
    assert fmt_minutes(330) == "5시간 30분"
    assert fmt_minutes(0) == "0시간 0분"
    assert fmt_hours(330) == "5.5"
    assert fmt_hours(720) == "12"


# ------------------------------------------------------ 근무표 (D8)

@pytest.mark.parametrize(
    "text,expected",
    [("10", time(10, 0)), ("10:00", time(10, 0)), ("1730", time(17, 30)),
     ("9:30", time(9, 30)), ("930", time(9, 30)), ("24:00", time(0, 0))],
)
def test_parse_time_is_forgiving(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "25:00", "10:70", "1:2:3"])
def test_parse_time_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_time(bad)


@pytest.mark.parametrize("text", ["10-22", "10:00~22:00", "10 – 22", "1000—2200"])
def test_parse_range_accepts_common_separators(text):
    assert parse_range(text) == (time(10, 0), time(22, 0))


def entry(emp, name, day, rng, dept=""):
    start, end = parse_range(rng)
    return ScheduleEntry(emp, name, day, start, end, dept)


MON = date(2026, 8, 24)


def test_weekly_totals():
    rows = build_week(
        [
            entry(1, "김호진", MON, "10-22", "홀"),
            entry(1, "김호진", MON.replace(day=25), "10-22", "홀"),
            entry(2, "이서연", MON.replace(day=25), "17-22", "홀"),
        ],
        MON,
    )
    totals = {r.employee_name: r.total_minutes for r in rows}
    assert totals == {"김호진": 1440, "이서연": 300}
    assert {r.employee_name: r.days_worked for r in rows} == {"김호진": 2, "이서연": 1}


def test_overnight_schedule_entry_counts_correctly():
    e = entry(1, "과장", MON, "22-02")
    assert e.crosses_midnight is True
    assert e.minutes == 240


def test_overlap_warning():
    warns = validate_week(
        [entry(1, "김호진", MON, "10-18"), entry(1, "김호진", MON, "16-22")], MON
    )
    assert any("겹칩니다" in w.message for w in warns)


def test_weekly_hour_thresholds():
    fifteen = [entry(2, "이서연", MON.replace(day=24 + i), "17-22") for i in range(3)]
    warns = validate_week(fifteen, MON)
    assert any(w.severity is Severity.INFO and "경계" in w.message for w in warns)

    heavy = [entry(3, "과장", MON.replace(day=24 + i), "10-22") for i in range(6)]
    warns = validate_week(heavy, MON)
    assert any(w.severity is Severity.WARNING and "넘습니다" in w.message for w in warns)


def test_thresholds_are_configurable_not_hardcoded():
    heavy = [entry(3, "과장", MON.replace(day=24 + i), "10-22") for i in range(6)]
    relaxed = HourThresholds(weekly_standard_hours=80)
    assert not any("넘습니다" in w.message for w in validate_week(heavy, MON, relaxed))


def test_coverage_gap_detection():
    warns = validate_week(
        [entry(1, "김호진", MON, "12-22", "홀")],
        MON,
        open_time=time(10, 0),
        close_time=time(22, 0),
        departments_requiring_cover=("홀",),
    )
    gaps = [w for w in warns if "배정된 사람이 없습니다" in w.message]
    assert len(gaps) == 1
    assert "10:00–12:00" in gaps[0].message


def test_no_gap_warning_when_department_is_closed_that_day():
    warns = validate_week(
        [entry(1, "김호진", MON, "10-22", "홀")],
        MON,
        open_time=time(10, 0),
        close_time=time(22, 0),
        departments_requiring_cover=("홀",),
    )
    assert not [w for w in warns if "배정된 사람이 없습니다" in w.message]


# ---------------------------------------------- 한 칸에 여러 근무 (하루 2탕)

def test_parse_cell_single_and_multiple():
    from app.domain.schedule import parse_cell

    assert parse_cell("10-22") == [(time(10, 0), time(22, 0))]
    assert parse_cell("10-14 17-22") == [
        (time(10, 0), time(14, 0)), (time(17, 0), time(22, 0))
    ]
    assert parse_cell("10:00-14:00, 17:00-22:00") == [
        (time(10, 0), time(14, 0)), (time(17, 0), time(22, 0))
    ]
    assert parse_cell("") == []
    assert parse_cell("   ") == []


def test_parse_cell_rejects_garbage():
    from app.domain.schedule import parse_cell

    with pytest.raises(ValueError):
        parse_cell("열시부터")
