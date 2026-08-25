#!/usr/bin/env python3
"""소매가 추적 설정 — 설계서 reports/online-price-tracking-design.md 의 결정 사항.

여기 값만 고치면 수집기·집계기 동작이 바뀐다. 코드를 건드릴 필요는 없다.
"""
from __future__ import annotations

# ── 패널 규모 (설계 Q3) ────────────────────────────────────────────────
# 층화 표집: 품목당 가격 3분위에서 균등 추출하므로 품목당 최소 9개가 필요하다.
MIN_OFFERS_PER_ITEM = 9
TARGET_OFFERS_PER_ITEM = 15
TERCILES = ("lo", "mid", "hi")

# ── 이상치 밴드 (설계 6-3) ────────────────────────────────────────────
MIN_TOTAL_G = 300
MAX_TOTAL_G = 5_000
MIN_PRICE = 5_000
MAX_PRICE = 1_000_000
# 품목별 직전 30일 중앙값 대비 허용 배수. 벗어나면 outlier 로 표시하고 집계에서만 뺀다.
OUTLIER_LO_RATIO = 0.4
OUTLIER_HI_RATIO = 2.5
OUTLIER_WINDOW_DAYS = 30

# ── 집계 (설계 10장) ──────────────────────────────────────────────────
MA_WINDOW_DAYS = 7          # 헤드라인은 중앙값 7일 이동평균
MIN_OFFERS_FOR_STAT = 3     # 관측이 이보다 적으면 통계를 내지 않는다(null)

# ── 알림 임계값 (설계 11장) ───────────────────────────────────────────
TREND_SHIFT_PCT = 0.05      # 7일 이동평균이 전주 대비 ±5%
ORIGIN_GAP_SHIFT_PCT = 0.10 # 원산지 배수가 전월 대비 ±10%
PARSE_FAIL_RATE = 0.20      # 수집 이상 — 최우선 등급
MIN_PER_TERCILE = 2         # 표본 붕괴 — 분위별 관측이 이보다 적으면 경보
PENDING_ALERT = 5           # 검토 대기 누적

# ── 수입 원가 환산 (설계 Q6) ──────────────────────────────────────────
# ★2 "수입단가 대비 소매 배수"를 국내 원가 기준으로 내려면 아래가 모두 필요하다.
# 관세율은 HS 코드·협정(한중FTA 등)·연도에 따라 달라 임의값을 넣으면 지표가
# 통째로 무의미해진다. 그래서 기본값은 '미설정'이고, 미설정인 동안에는
# CIF 기준 배수만 산출하며 리포트에도 CIF 기준임을 명시한다.
#
# 확정되면 LANDED_COST["enabled"] = True 로 바꾸고 값을 채우면 된다.
LANDED_COST = {
    "enabled": False,
    "tariff_rate": None,            # 관세율 (예: 0.20)
    "vat_rate": 0.10,               # 부가세 10%
    "clearance_krw_per_kg": None,   # 통관·검역 비용
    "inland_krw_per_kg": None,      # 국내 내륙물류비
}

# 환율은 관세율과 달리 객관적인 값이라 따로 둔다. retail_fx.py 가 매일 받아
# fx_history.csv 에 쌓고, 집계는 그 최근값을 쓴다. 여기에 숫자를 넣으면
# 수집값 대신 이 값이 쓰인다(관세청 고시환율을 직접 고정하고 싶을 때).
USDKRW_OVERRIDE = None

# ── 수집 대상 포털 ────────────────────────────────────────────────────
PORTALS = ("naver", "coupang")
PORTAL_HOSTS = {
    "naver": ("smartstore.naver.com", "shopping.naver.com", "brand.naver.com",
              "search.shopping.naver.com", "m.smartstore.naver.com"),
    "coupang": ("www.coupang.com", "coupang.com", "m.coupang.com"),
}

REQUEST_DELAY_SEC = 2.0     # 오퍼 간 지연
REQUEST_JITTER_SEC = 1.5    # 지터 상한
MAX_RETRIES = 2


# ── 교차 분석 매핑 (설계 10-4) ────────────────────────────────────────
# ★2 수입단가 대비 배수: 품목을 관세청 HS 코드에 대응시킨다.
# import_history.csv 의 label 로 확인한 대응이다.
#   0304690000 뱀장어 필레(냉동)        → 필렛
#   1604171000/1604179000 뱀장어 조제품 → 양념구이
#   0303290000 냉동 뱀장어(통)          → 통장어
# 국내산 품목(FIL-KR-FZ)은 수입 대응이 없으므로 비워 둔다.
IMPORT_HS_BY_ITEM = {
    "FIL-CN-FZ": ["0304690000"],
    "SEA-ANY-FZ": ["1604179000", "1604171000"],
    "WHL-ANY-FZ": ["0303290000"],
}
IMPORT_COUNTRY = "CN"

# ★3 소매÷도매: 도매는 활 뱀장어 경매가(원물)다.
# 경매가 서는 시장이 적어 일별 값이 드물므로 이동창 중앙값을 쓴다.
WHOLESALE_SPECIES = "뱀장어"
WHOLESALE_WINDOW_DAYS = 30

# 활장어 → 필렛 수율. 원물 1kg 과 제품 1kg 은 같은 양이 아니므로 엄밀한
# 마진 비교에는 수율 보정이 필요하다. 확정 전까지는 None(미보정)으로 두고
# 리포트에 '원물 기준'임을 명시한다.
FILLET_YIELD = None
