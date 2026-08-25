#!/usr/bin/env python3
"""소매가 추적 로직 검증 — 네트워크 없이 순수 함수만 확인한다.

실행: python3 tracker/test_retail.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import retail_normalize as n  # noqa: E402
import retail_aggregate as agg  # noqa: E402
import retail_config as cfg  # noqa: E402
import retail_panel as panel  # noqa: E402

FAILS: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILS.append(f"{label}\n    기대: {want!r}\n    실제: {got!r}")


# ── 중량 파싱 ─────────────────────────────────────────────────────────
WEIGHT_CASES = [
    ("민물장어 필렛 1kg", 1000),
    ("민물장어 필렛 500g", 500),
    ("장어 500g x 2", 1000),
    ("장어 500g × 2팩", 1000),
    ("장어 500g*2", 1000),
    ("장어 1kg(500g x 2)", 1000),        # 총량+내역 이중계산 금지
    ("장어 2kg (1kg x 2)", 2000),        # 구 로직이 4000으로 틀리던 케이스
    ("양념장어 [1kg] 소포장 250g x 4", 1000),
    ("장어 2팩 500g", 1000),             # 배수가 앞
    ("민물장어 700그램", 700),
    ("장어 0.5kg", 500),
    ("장어세트 3봉 300g", 900),
    ("민물장어 필렛", None),             # 중량 없음
    ("장어 50g 시식용", None),           # 밴드 밖(100g 미만)
    ("장어 선물세트 30000원", None),     # 숫자는 있으나 중량 아님
]

# ── 미수 ─────────────────────────────────────────────────────────────
UNIT_CASES = [
    ("민물장어 1kg 8미", 8), ("장어 10마리", 10), ("장어 8미 특대", 8),
    ("장어 100미터 낚시줄", None),       # '미터' 오인 금지
    ("장어 3만원 미만", None),           # '미만' 오인 금지
    ("민물장어 필렛 1kg", None),
]

# ── 원산지 ────────────────────────────────────────────────────────────
ORIGIN_CASES = [
    ("국내산 민물장어 필렛", "국내산"),
    ("국산 민물장어", "국내산"),
    ("한국산 장어", "국내산"),
    ("중국산 양념민물장어 1kg", "중국산"),   # '중국산' 안의 '국산' 오인 금지
    ("중국 민물장어 필렛", "중국산"),
    ("일본산 장어 데리야끼", "일본산"),
    ("국내가공 중국산 원료 장어", "중국산"),
    ("국내산 vs 중국산 비교 장어", None),    # 둘 다 나오면 사람에게
    ("민물장어 필렛 1kg", None),
]

# ── 형태 ─────────────────────────────────────────────────────────────
FORM_CASES = [
    ("국내산 민물장어 필렛 1kg", "필렛"),
    ("민물장어 필레 500g", "필렛"),
    ("양념 민물장어 구이 1kg", "양념구이"),
    ("민물장어 데리야끼 500g", "양념구이"),
    ("양념장어 필렛 1kg", "양념구이"),        # 양념이 필렛보다 우선
    ("민물장어탕 500g", "장어탕"),
    ("손질 민물장어 통마리 1kg", "통장어"),
    ("민물장어 1kg", None),                   # 단서 없으면 추정하지 않는다
]

# ── 어종·소스류 필터 ──────────────────────────────────────────────────
FILTER_CASES = [
    ("국내산 민물장어 필렛 1kg", True),
    ("붕장어 아나고 회 500g", False),
    ("장어 진액 30포", False),
    ("장어 양념장 소스 500ml", False),
    ("양념장어 1kg", True),                   # '양념장어'는 살린다
    ("민물장어 뼈 튀김", False),
    ("고등어 필렛 1kg", False),               # '장어' 없음
]

# ── 가격 정규화 ───────────────────────────────────────────────────────
def test_price():
    check("total_price 정상", n.total_price(30000, 3000), 33000)
    check("total_price 무료배송", n.total_price(30000, 0), 30000)
    check("total_price 배송비 미확인", n.total_price(30000, None), None)  # 0과 구분
    check("total_price 가격 없음", n.total_price(None, 0), None)
    check("won_per_kg 1kg", n.won_per_kg(33000, 1000), 33000.0)
    check("won_per_kg 500g", n.won_per_kg(33000, 500), 66000.0)
    check("won_per_kg 중량없음", n.won_per_kg(33000, None), None)
    check("won_per_ea", n.won_per_ea(32000, 8), 4000.0)
    check("band 정상", n.band_flags(30000, 1000), "")
    check("band 저가", n.band_flags(1000, 1000), "price_band")
    check("band 소량", n.band_flags(30000, 100), "weight_band")
    check("band 둘다", n.band_flags(100, 50), "price_band|weight_band")


def test_infer():
    """형태·보관 추정은 값과 함께 '추정했다'는 사실을 남겨야 한다."""
    r = n.parse_title("민물장어 1kg")
    check("추정 형태", (r["form"], r["form_inferred"]), ("통장어", True))
    r = n.parse_title("국내산 민물장어 필렛 1kg")
    check("명시 형태", (r["form"], r["form_inferred"]), ("필렛", False))
    check("추정 보관", (r["storage"], r["storage_inferred"]), ("냉동", True))
    r = n.parse_title("국내산 냉동 민물장어 필렛 1kg")
    check("명시 보관", (r["storage"], r["storage_inferred"]), ("냉동", False))
    r = n.parse_title("활 민물장어 1kg")
    check("활장어", r["storage"], "활")


def test_item_key():
    check("item_key 완전", n.item_key("필렛", "국내산", "냉동"), "필렛|국내산|냉동")
    check("item_key 원산지 결측", n.item_key("필렛", None, "냉동"), None)


def test_portal():
    cases = [
        ("https://smartstore.naver.com/abc/products/123", "naver"),
        ("https://m.smartstore.naver.com/abc/products/123", "naver"),
        ("https://www.coupang.com/vp/products/123", "coupang"),
        ("https://www.fooden.com/shop/detail.php", None),
    ]
    for url, want in cases:
        check(f"detect_portal {url}", n.detect_portal(url), want)


# ── 집계 수학 ─────────────────────────────────────────────────────────
def _snap(day, offer_id, item_id, wpk, status="ok", flag=""):
    return {"date": day, "offer_id": offer_id, "item_id": item_id, "status": status,
            "won_per_kg": str(wpk), "outlier_flag": flag, "stock": "in"}


def test_percentile():
    xs = [40000.0, 45000.0, 50000.0, 55000.0, 60000.0]
    check("p25", agg.percentile(xs, 0.25), 45000.0)
    check("p50", agg.percentile(xs, 0.50), 50000.0)
    check("p75", agg.percentile(xs, 0.75), 55000.0)
    check("보간", agg.percentile([10.0, 20.0], 0.5), 15.0)
    check("단일값", agg.percentile([7.0], 0.75), 7.0)
    check("빈값", agg.percentile([], 0.5), None)


def test_daily():
    offers = [
        {"offer_id": f"N-{i}", "seller_name": f"판매자{i}", "tercile": t,
         "match_status": "confirmed"}
        for i, t in enumerate(["lo", "lo", "mid", "mid", "hi"], 1)
    ]
    snaps = []
    for oid, v in zip("12345", [40000, 45000, 50000, 55000, 60000]):
        snaps.append(_snap("2026-08-01", f"N-{oid}", "FIL-KR-FZ", v))
    # 이튿날 일제히 +10%, 여기에 상대 이상치 1건(직전 중앙값의 4배)을 섞는다
    for oid, v in zip("12345", [44000, 49500, 55000, 60500, 66000]):
        snaps.append(_snap("2026-08-02", f"N-{oid}", "FIL-KR-FZ", v))
    offers.append({"offer_id": "N-9", "seller_name": "이상치몰", "tercile": "hi",
                   "match_status": "confirmed"})
    snaps.append(_snap("2026-08-02", "N-9", "FIL-KR-FZ", 200000))

    rows = agg.build_daily(snaps, offers)
    check("일수", len(rows), 2)
    d1, d2 = rows[0], rows[1]
    check("1일 중앙값", d1["median_won_per_kg"], 50000.0)
    check("1일 p25", d1["p25_won_per_kg"], 45000.0)
    check("1일 p75", d1["p75_won_per_kg"], 55000.0)
    check("1일 분산비", d1["spread_ratio"], round(55000 / 45000, 3))
    check("1일 판매자수", d1["n_sellers"], 5)
    check("1일 분위분포", (d1["n_lo"], d1["n_mid"], d1["n_hi"]), (2, 2, 1))
    check("2일 이상치 제외", d2["n_offers"], 5)          # 200000 은 빠져야 한다
    check("2일 중앙값", d2["median_won_per_kg"], 55000.0)
    check("2일 이동평균", d2["median_ma7"], 52500.0)     # (50000+55000)/2
    check("2일 전일대비", d2["d1_pct"], "0.1000")
    check("2일 30일최저 아님", d2["is_30d_low"], "")


def test_daily_excludes_pending():
    offers = [{"offer_id": "N-1", "seller_name": "A", "tercile": "mid",
               "match_status": "confirmed"},
              {"offer_id": "N-2", "seller_name": "B", "tercile": "mid",
               "match_status": "pending"}]
    snaps = [_snap("2026-08-01", "N-1", "FIL-KR-FZ", 50000),
             _snap("2026-08-01", "N-2", "FIL-KR-FZ", 90000)]
    rows = agg.build_daily(snaps, offers)
    check("pending 제외", rows[0]["n_offers"], 1)


def test_daily_needs_minimum():
    """관측이 최소 개수에 못 미치면 통계를 내지 않고 비워 둔다."""
    offers = [{"offer_id": "N-1", "seller_name": "A", "tercile": "mid",
               "match_status": "confirmed"}]
    snaps = [_snap("2026-08-01", "N-1", "FIL-KR-FZ", 50000)]
    rows = agg.build_daily(snaps, offers)
    check("표본 부족 시 중앙값 없음", rows[0]["median_won_per_kg"], "")
    check("표본 부족해도 최저가는 기록", rows[0]["min_won_per_kg"], 50000.0)


def test_daily_skips_failed():
    offers = [{"offer_id": f"N-{i}", "seller_name": f"S{i}", "tercile": "mid",
               "match_status": "confirmed"} for i in range(1, 5)]
    snaps = [_snap("2026-08-01", "N-1", "FIL-KR-FZ", 50000),
             _snap("2026-08-01", "N-2", "FIL-KR-FZ", 52000),
             _snap("2026-08-01", "N-3", "FIL-KR-FZ", 51000),
             _snap("2026-08-01", "N-4", "FIL-KR-FZ", 0, status="parse_failed"),
             _snap("2026-08-01", "N-4", "FIL-KR-FZ", 99, flag="price_band")]
    rows = agg.build_daily(snaps, offers)
    check("실패·밴드이상치 제외", rows[0]["n_offers"], 3)


def test_cross_import():
    """실제 import_history.csv 에 대응하는 HS 코드가 잡히는지 확인한다."""
    from datetime import date as _date
    usd, month = agg.import_usd_per_kg("FIL-CN-FZ", _date(2026, 8, 25))
    check("필렛 HS 대응됨", usd is not None and usd > 0, True)
    check("월 형식", bool(month and len(month) == 7), True)
    none_usd, _ = agg.import_usd_per_kg("FIL-KR-FZ", _date(2026, 8, 25))
    check("국내산은 수입대응 없음", none_usd, None)


def test_cross_wholesale():
    from datetime import date as _date
    val, n = agg.wholesale_median(_date(2026, 8, 25))
    check("도매 중앙값 산출됨", val is not None and val > 0, True)
    check("경매 건수 > 0", n > 0, True)


def test_match_item():
    items = panel.load_csv(panel.ITEMS_CSV)
    check("국내산 필렛", panel.match_item(items, "필렛", "국내산", "냉동"), "FIL-KR-FZ")
    check("중국산 필렛", panel.match_item(items, "필렛", "중국산", "냉동"), "FIL-CN-FZ")
    check("양념은 원산지 무관", panel.match_item(items, "양념구이", "중국산", "냉동"),
          "SEA-ANY-FZ")
    check("양념 원산지 없어도", panel.match_item(items, "양념구이", None, "냉동"),
          "SEA-ANY-FZ")
    check("냉장 필렛은 미배정", panel.match_item(items, "필렛", "국내산", "냉장"), None)
    check("보관 미상은 미배정", panel.match_item(items, "필렛", "국내산", None), None)


def main() -> int:
    for title, want in WEIGHT_CASES:
        check(f"중량 {title!r}", n.parse_weight_g(title), want)
    for title, want in UNIT_CASES:
        check(f"미수 {title!r}", n.parse_unit_count(title), want)
    for title, want in ORIGIN_CASES:
        check(f"원산지 {title!r}", n.parse_origin(title), want)
    for title, want in FORM_CASES:
        check(f"형태 {title!r}", n.parse_form(title), want)
    for title, want in FILTER_CASES:
        check(f"필터 {title!r}", n.is_eel_product(title)[0], want)
    test_price()
    test_infer()
    test_item_key()
    test_portal()
    test_percentile()
    test_daily()
    test_daily_excludes_pending()
    test_daily_needs_minimum()
    test_daily_skips_failed()
    test_cross_import()
    test_cross_wholesale()
    test_match_item()

    if FAILS:
        print(f"❌ 실패 {len(FAILS)}건\n")
        for f in FAILS:
            print("  " + f)
        return 1
    print("✅ 전체 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
