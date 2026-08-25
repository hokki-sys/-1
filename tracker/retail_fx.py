#!/usr/bin/env python3
"""USD/KRW 환율 수집 — ★2 지표(수입단가 대비 소매 배수)의 환산에 쓴다.

관세율·통관비 가정은 아직 미확정이지만(설계 Q6), 환율은 객관적인 값이므로
이것만이라도 있으면 CIF 기준 '배수'를 무차원으로 낼 수 있다.
환율이 없으면 원/kg ÷ USD/kg 이 되어 사실상 환율을 다시 계산한 값이 나오므로
지표로서 읽히지 않는다.

키가 필요 없는 공개 소스를 순서대로 시도하고, 성공하면 fx_history.csv 에
누적한다. 전부 실패해도 과거 값이 있으면 집계는 그대로 진행된다.
실행: python3 tracker/retail_fx.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import track_price as tp                       # noqa: E402
from retail_panel import append_csv, load_csv, today_kst  # noqa: E402

FX_CSV = ROOT / "fx_history.csv"
FX_FIELDS = ["date", "usdkrw", "source"]

SOURCES = [
    ("frankfurter", "https://api.frankfurter.app/latest?from=USD&to=KRW",
     lambda d: d["rates"]["KRW"]),
    ("er-api", "https://open.er-api.com/v6/latest/USD",
     lambda d: d["rates"]["KRW"]),
]


def fetch_rate() -> tuple[float, str] | None:
    for name, url, pick in SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": tp.UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                rate = float(pick(json.loads(resp.read().decode("utf-8"))))
            if 500 < rate < 3000:              # 상식적인 범위를 벗어나면 버린다
                return rate, name
            tp.log(f"  {name}: 값이 비정상({rate}) — 버림")
        except Exception as e:
            tp.log(f"  {name}: 실패({type(e).__name__})")
    return None


def latest_rate() -> tuple[float | None, str]:
    """저장된 최근 환율. (환율, 기준일). 없으면 (None, '')."""
    rows = [r for r in load_csv(FX_CSV) if r.get("usdkrw")]
    if not rows:
        return None, ""
    last = sorted(rows, key=lambda r: r["date"])[-1]
    try:
        return float(last["usdkrw"]), last["date"]
    except ValueError:
        return None, ""


def main() -> int:
    today = today_kst()
    if any(r["date"] == today for r in load_csv(FX_CSV)):
        tp.log(f"환율 이미 수집됨({today}) — 건너뜀")
        return 0
    got = fetch_rate()
    if got is None:
        prev, when = latest_rate()
        if prev:
            tp.log(f"환율 수집 실패 — 직전 값 {prev:,.2f} ({when}) 을 계속 사용합니다")
        else:
            tp.log("환율 수집 실패, 저장된 값도 없음 — ★2 배수는 산출되지 않습니다")
        return 0
    rate, source = got
    append_csv(FX_CSV, [{"date": today, "usdkrw": round(rate, 2), "source": source}],
               FX_FIELDS)
    tp.log(f"USD/KRW {rate:,.2f} ({source})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
