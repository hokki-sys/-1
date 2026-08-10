#!/usr/bin/env python3
"""중국산 장어 수입가격 수집기 (관세청 수출입실적 공공 API).

data.go.kr 의 관세청 '품목별 국가별 수출입실적' API 로 HS코드별·국가별
월간 수입중량(kg)과 수입금액(USD)을 받아 kg당 수입단가(USD)를 계산해
tracker/import_history.csv 에 누적한다. 같은 달 데이터는 갱신(upsert)한다.

필요 환경변수: DATA_GO_KR_KEY (data.go.kr 인증키)
키가 없으면 수집을 건너뛰고 정상 종료한다.
"""
from __future__ import annotations

import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

import market_price as mp
import track_price as tp

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "import_history.csv"

API_URL = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
COUNTRY = "CN"  # 중국
HS_CODES = [
    ("0301920000", "활뱀장어"),
    ("0303260000", "냉동뱀장어"),
    ("1604170000", "조제장어(양념구이류)"),
]
MONTHS_BACK = 36  # 최초 실행 시 소급 수집 범위

FIELDS = ["month", "hs", "label", "country", "import_usd", "import_kg",
          "usd_per_kg", "collected"]


def month_range(back: int) -> tuple[str, str]:
    now = datetime.now(tp.KST)
    end = now.year * 12 + (now.month - 1)
    start = end - back
    return (f"{start // 12}{start % 12 + 1:02d}", f"{end // 12}{end % 12 + 1:02d}")


def api_fetch(key: str, hs: str, start: str, end: str) -> bytes:
    # data.go.kr 키는 이미 인코딩된 형태로 발급되는 경우가 많아 그대로 붙인다
    if "%" not in key:
        key = quote(key, safe="")
    url = (f"{API_URL}?serviceKey={key}&strtYymm={start}&endYymm={end}"
           f"&hsSgn={hs}&cntyCd={COUNTRY}")
    req = urllib.request.Request(url, headers={"User-Agent": tp.UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.read()


def parse_rows(xml_bytes: bytes, hs: str, label: str, today: str) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    result_code = (root.findtext(".//resultCode") or "").strip()
    if result_code not in ("", "00", "0000"):
        msg = (root.findtext(".//resultMsg") or "").strip()
        raise RuntimeError(f"API 오류 resultCode={result_code} msg={msg}")
    rows = []
    for item in root.iter("item"):
        ym_raw = (item.findtext("year") or "").strip()  # 예: "2026.06" 또는 "총계"
        m = re.search(r"(\d{4})\D?(\d{2})", ym_raw)
        if not m:
            continue
        month = f"{m.group(1)}-{m.group(2)}"
        try:
            imp_usd = int(float(item.findtext("impDlr") or 0))
            imp_kg = int(float(item.findtext("impWgt") or 0))
        except ValueError:
            continue
        if imp_kg <= 0:
            continue
        rows.append({
            "month": month, "hs": hs, "label": label, "country": COUNTRY,
            "import_usd": str(imp_usd), "import_kg": str(imp_kg),
            "usd_per_kg": f"{imp_usd / imp_kg:.2f}",
            "collected": today,
        })
    return rows


def main() -> int:
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        tp.log("DATA_GO_KR_KEY 미등록 — 수입가격 수집을 건너뜁니다.")
        tp.log("등록 방법: tracker/README.md 의 '관세청 API 키 등록' 참고")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("\n> ⚠️ 관세청 API 키 미등록 — 수입가격 수집 건너뜀\n")
        return 0

    today = datetime.now(tp.KST).strftime("%Y-%m-%d")
    existing = mp.load_csv(CSV_PATH)
    start, end = month_range(MONTHS_BACK)

    fresh: list[dict] = []
    failures = 0
    for hs, label in HS_CODES:
        tp.log(f"\n=== 수입실적: {label} (HS {hs}, {COUNTRY}, {start}~{end}) ===")
        try:
            xml_bytes = api_fetch(key, hs, start, end)
            rows = parse_rows(xml_bytes, hs, label, today)
        except ET.ParseError as e:
            tp.log(f"응답 파싱 실패: {e}")
            tp.DEBUG_DIR.mkdir(exist_ok=True)
            (tp.DEBUG_DIR / f"import_{hs}.xml").write_bytes(xml_bytes)
            failures += 1
            continue
        except Exception as e:
            tp.log(f"API 요청 실패: {e}")
            failures += 1
            continue
        tp.log(f"월별 실적 {len(rows)}건")
        for r in rows[-3:]:
            tp.log(f"  {r['month']}: {int(r['import_kg']):,}kg, "
                   f"${int(r['import_usd']):,} → ${r['usd_per_kg']}/kg")
        fresh.extend(rows)

    if fresh:
        fresh_keys = {(r["month"], r["hs"], r["country"]) for r in fresh}
        kept = [r for r in existing
                if (r.get("month"), r.get("hs"), r.get("country")) not in fresh_keys]
        merged = kept + fresh
        merged.sort(key=lambda r: (r.get("month", ""), r.get("hs", "")))
        mp.save_csv(CSV_PATH, merged, FIELDS)
        tp.log(f"\nimport_history.csv 갱신: 수신 {len(fresh)}건 (upsert)")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and fresh:
        lines = ["", "## 중국산 수입단가 (관세청, 최근월)", "",
                 "| 품목 | 월 | 수입량 | USD/kg |", "|---|---|---|---|"]
        for hs, label in HS_CODES:
            mine = [r for r in fresh if r["hs"] == hs]
            if mine:
                r = max(mine, key=lambda r: r["month"])
                lines.append(f"| {label} | {r['month']} | {int(r['import_kg']):,}kg "
                             f"| ${r['usd_per_kg']} |")
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    return 1 if failures == len(HS_CODES) else 0


if __name__ == "__main__":
    sys.exit(main())
