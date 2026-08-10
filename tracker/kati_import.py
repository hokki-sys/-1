#!/usr/bin/env python3
"""중국산 장어 수입실적 수집기 (KATI 월별품목별 실적, 키 불필요).

KATI(농수산식품수출지원정보)의 월별품목별 실적 조회를 수입(ieStandard=I),
중국(nations=CN) 기준으로 호출하면 전 품목의 월간 수입 중량(kg)·금액($)이
페이지 내 JS 데이터로 포함된다. 그중 장어 품목(뱀장어·실장어·조제장어 등)을
걸러 tracker/import_history.csv 에 월별로 upsert 한다. 원천은 한국무역통계
진흥원(관세청 통관자료)이며 수입금액은 CIF 기준이다.

첫 실행 시 과거 18개월을 소급하고, 이후에는 최근 3개월만 갱신한다.
"""
from __future__ import annotations

import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import market_price as mp
import track_price as tp

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "import_history.csv"

BASE = "https://www.kati.net"
PAGE = "/statistics/monthlyPerformanceByProduct.do"
COUNTRY = "CN"
BACKFILL_MONTHS = 18
REFRESH_MONTHS = 3

FIELDS = ["month", "hs", "label", "country", "import_usd", "import_kg",
          "usd_per_kg", "cum_usd", "cum_kg", "collected"]

SEA_EEL = ("붕장어", "갯장어", "먹장어", "곰장어", "꼼장어", "아나고")


def is_eel(name: str) -> bool:
    if "장어" not in name:
        return False
    return not any(s in name for s in SEA_EEL)


def month_list(count: int) -> list[str]:
    now = datetime.now(tp.KST)
    idx = now.year * 12 + (now.month - 1)
    months = []
    for back in range(1, count + 1):  # 당월은 집계 미완이라 전월부터
        m = idx - back
        months.append(f"{m // 12}{m % 12 + 1:02d}")
    return months


def fetch_month(yyyymm: str) -> str:
    data = {
        "ieStandard": "I", "basisYear": yyyymm[:4], "basisMonth": yyyymm[4:],
        "basisYearMonth": yyyymm, "period": "2", "unit": "Kg",
        "nations": COUNTRY, "nationSearchType": "4", "productType": "P",
        "classification": "0", "productRowEnabled": "true",
        "showComparisonBasisMonth": "true", "showComparisonMonthTotal": "true",
    }
    req = urllib.request.Request(
        BASE + PAGE, data=urlencode(data).encode(),
        headers={"User-Agent": tp.UA, "Accept": "*/*",
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "Referer": BASE + PAGE})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read().decode("utf-8", "replace")


def num(record: dict, key: str) -> int | None:
    v = (record.get(key) or "").replace(",", "")
    if not v or v == "null":
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def parse_eel_rows(body: str, yyyymm: str, today: str, verbose: bool) -> list[dict]:
    rows = []
    for chunk in body.split("},{"):
        if "agname" not in chunk or "장어" not in chunk:
            continue
        rec = dict(re.findall(r'(\w+)\s*:\s*"([^"]*)"', chunk))
        name = rec.get("agname", "")
        if not is_eel(name):
            continue
        if verbose:
            nums = {k: v for k, v in rec.items() if k.startswith("i_") and v != "null"}
            tp.log(f"  [필드덤프] {name} hs={rec.get('hscd')} {nums}")
        cur_wgt = num(rec, "i_cur_wgt_m")   # 당월 수입중량(kg)
        cur_amt = num(rec, "i_cur_amt_m")   # 당월 수입금액($)
        tot_wgt = num(rec, "i_tot_wgt_m")   # 월누계 중량
        tot_amt = num(rec, "i_tot_amt_m")   # 월누계 금액
        if not cur_wgt and not tot_wgt:
            continue  # 해당 월 실적 없음
        month = f"{yyyymm[:4]}-{yyyymm[4:]}"
        rows.append({
            "month": month, "hs": rec.get("hscd", ""), "label": name,
            "country": COUNTRY,
            "import_usd": str(cur_amt or ""), "import_kg": str(cur_wgt or ""),
            "usd_per_kg": f"{cur_amt / cur_wgt:.2f}" if cur_wgt and cur_amt else "",
            "cum_usd": str(tot_amt or ""), "cum_kg": str(tot_wgt or ""),
            "collected": today,
        })
    return rows


def main() -> int:
    today = datetime.now(tp.KST).strftime("%Y-%m-%d")
    existing = mp.load_csv(CSV_PATH)
    months = month_list(BACKFILL_MONTHS if not existing else REFRESH_MONTHS)

    fresh: list[dict] = []
    failures = 0
    for i, yyyymm in enumerate(months):
        try:
            body = fetch_month(yyyymm)
        except Exception as e:
            tp.log(f"{yyyymm}: 요청 실패 {e}")
            failures += 1
            time.sleep(2)
            continue
        rows = parse_eel_rows(body, yyyymm, today, verbose=(i == 0))
        with_trade = [r for r in rows if r["import_kg"]]
        tp.log(f"{yyyymm}: 장어 수입실적 {len(with_trade)}개 품목")
        for r in with_trade:
            per = f" (${r['usd_per_kg']}/kg)" if r["usd_per_kg"] else ""
            tp.log(f"  {r['label']} [{r['hs']}]: {int(r['import_kg']):,}kg, "
                   f"${int(r['import_usd']):,}{per}")
        fresh.extend(rows)
        time.sleep(1.5)

    if fresh:
        fresh_keys = {(r["month"], r["hs"]) for r in fresh}
        kept = [r for r in existing
                if (r.get("month"), r.get("hs")) not in fresh_keys]
        merged = kept + fresh
        merged.sort(key=lambda r: (r.get("month", ""), r.get("hs", "")))
        mp.save_csv(CSV_PATH, merged, FIELDS)
        tp.log(f"\nimport_history.csv 갱신: {len(fresh)}건 upsert (총 {len(merged)}건)")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and fresh:
        latest = max(r["month"] for r in fresh)
        lines = ["", f"## 중국산 장어 수입실적 ({latest}, KATI/관세청 통관)", "",
                 "| 품목 | 수입량 | USD/kg |", "|---|---|---|"]
        for r in fresh:
            if r["month"] == latest and r["import_kg"]:
                per = f"${r['usd_per_kg']}" if r["usd_per_kg"] else "-"
                lines.append(f"| {r['label']} | {int(r['import_kg']):,}kg | {per} |")
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    return 1 if failures == len(months) else 0


if __name__ == "__main__":
    sys.exit(main())
