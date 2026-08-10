#!/usr/bin/env python3
"""원물(도매시장 경매가) 시세 수집기.

시세판(sisepan.com)의 장어 도매가 페이지에서 전국 도매시장 경매 결과
(뱀장어, 장어(수입))를 읽어 tracker/wonmul_history.csv 에 누적한다.
경매 건 단위로 기록하며 (경매일, 시장, 품종, 규격, 등급, 평균가) 기준으로
중복을 걸러 append-only 로 쌓는다.
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import market_price as mp
import track_price as tp

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "wonmul_history.csv"
# 장어 경매가 노출되는 시세판 페이지들: 품목(장어) + 관련 분류 페이지
# (분류 페이지에는 수입 냉동장어 등 품목 페이지에 없는 행이 있을 수 있다)
PAGES = [
    ("장어",          "https://www.sisepan.com/nong-dome-price?type=3&mode=ca2&id=1916"),
    ("활 내수면어류",  "https://www.sisepan.com/nong-dome-price?type=3&mode=ca1&id=60"),
    ("활 내수면기타",  "https://www.sisepan.com/nong-dome-price?type=3&mode=ca1&id=61"),
    ("냉동 내수면어류", "https://www.sisepan.com/nong-dome-price?type=3&mode=ca1&id=90"),
    ("냉동 내수면기타", "https://www.sisepan.com/nong-dome-price?type=3&mode=ca1&id=91"),
    ("신선 내수면어류", "https://www.sisepan.com/nong-dome-price?type=3&mode=ca1&id=76"),
]

FIELDS = ["auction_date", "species", "spec_kg", "grade", "avg_price",
          "min_price", "max_price", "volume_kg", "market", "corp",
          "price_per_kg", "collected"]

# 예: "[ 장어] 뱀장어 1 kg 자연산 상 24,242원 24,242원 30 kg 2026-08-08 서울가락 25,000원 727,260원 가락수산"
ROW_RE = re.compile(
    r"\[\s*[^\]]{1,15}\]\s*(\S+)\s+([\d\.,]+)\s*kg\s+(.+?)\s+"
    r"([\d,]+)원\s+([\d,]+)원\s+([\d,]+)\s*kg\s+(\d{4}-\d{2}-\d{2})\s+"
    r"(\S+)\s+([\d,]+)원\s+([\d,]+)원\s+(\S+)"
)

# 바다장어류(해면)는 제외하고 민물장어 계열만 남긴다
SEA_EEL = ("붕장어", "갯장어", "먹장어", "곰장어", "꼼장어")


def is_target_species(species: str) -> bool:
    if "장어" not in species:
        return False
    return not any(s in species for s in SEA_EEL)


def to_int(s: str) -> int:
    return int(s.replace(",", ""))


def main() -> int:
    today = datetime.now(tp.KST).strftime("%Y-%m-%d")

    if not tp.robots_allowed(PAGES[0][1]):
        tp.log("robots.txt 가 수집을 금지하고 있어 원물시세를 건너뜁니다.")
        return 0

    existing = mp.load_csv(CSV_PATH)
    seen = {(r.get("auction_date"), r.get("market"), r.get("species"),
             r.get("spec_kg"), r.get("grade"), r.get("avg_price"))
            for r in existing}

    new_rows = []
    total_matched = 0
    fetch_fail = 0
    for page_label, url in PAGES:
        try:
            raw, _final, ctype = tp.fetch(url)
        except Exception as e:
            tp.log(f"[{page_label}] 페이지 요청 실패(다음 실행에서 재시도): {e}")
            fetch_fail += 1
            time.sleep(1)
            continue
        text = tp.strip_tags(tp.decode_html(raw, ctype))

        page_matched = 0
        page_new = 0
        for m in ROW_RE.finditer(text):
            species, spec, grade, avg, mn, vol, date, market, mx, _amt, corp = m.groups()
            if not is_target_species(species):
                continue
            try:
                spec_kg = float(spec.replace(",", ""))
                avg_i = to_int(avg)
            except ValueError:
                continue
            if spec_kg <= 0 or avg_i <= 0:
                continue
            page_matched += 1
            row = {
                "auction_date": date, "species": species,
                "spec_kg": f"{spec_kg:g}", "grade": grade.strip(),
                "avg_price": str(avg_i), "min_price": str(to_int(mn)),
                "max_price": str(to_int(mx)), "volume_kg": str(to_int(vol)),
                "market": market, "corp": corp,
                "price_per_kg": str(round(avg_i / spec_kg)),
                "collected": today,
            }
            key = (row["auction_date"], row["market"], row["species"],
                   row["spec_kg"], row["grade"], row["avg_price"])
            if key in seen:
                continue
            seen.add(key)
            new_rows.append(row)
            page_new += 1
            tp.log(f"경매: {date} {market} {species} {spec_kg:g}kg {row['grade']} "
                   f"평균 {avg_i:,}원 (kg당 {int(row['price_per_kg']):,}원)")
        total_matched += page_matched
        tp.log(f"[{page_label}] 장어 경매 {page_matched}건, 신규 {page_new}건")
        time.sleep(1)  # 페이지 간 예의상 간격

    if new_rows:
        merged = existing + new_rows
        merged.sort(key=lambda r: (r.get("auction_date", ""), r.get("market", "")))
        mp.save_csv(CSV_PATH, merged, FIELDS)
    tp.log(f"\n원물시세 합계: 장어 경매 {total_matched}건, 신규 {len(new_rows)}건 기록")
    if not total_matched and not existing and fetch_fail < len(PAGES):
        tp.log("파싱 결과가 없습니다 — 페이지 구조 변경 여부 확인 필요")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and new_rows:
        lines = ["", "## 원물 도매시세 (신규 경매)", "",
                 "| 경매일 | 시장 | 품종 | 등급 | kg당 |", "|---|---|---|---|---|"]
        for r in new_rows[:10]:
            lines.append(f"| {r['auction_date']} | {r['market']} | {r['species']} "
                         f"| {r['grade']} | {int(r['price_per_kg']):,}원 |")
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
