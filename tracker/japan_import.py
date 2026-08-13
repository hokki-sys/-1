#!/usr/bin/env python3
"""중국→일본 장어 수입실적 수집기 (e-Stat, 일본 재무성 무역통계 기반).

e-Stat의 농림수산성 '농림수산물 수출입통계 > 무역통계(수입) > 수산물 >
うなぎ(치어·활·조제품)' 월별 파일을 내려받아 중국(中華人民共和国) 행에서
활장어·조제장어의 수입 중량(kg)과 금액(천엔)을 읽어 JPY/kg 단가를 계산,
tracker/japan_import_history.csv 에 월별로 누적한다. 키 불필요.

파일은 월마다 별도의 statInfId 로 발행되므로 e-Stat 검색 목록에서 ID들을
수집한 뒤, 각 파일 머리글(년차/월차)로 해당 월을 식별한다. 이미 수집한
월은 건너뛰고, 실행당 새 파일 다운로드는 소수로 제한한다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import market_price as mp
import track_price as tp

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "japan_import_history.csv"
BASE = "https://www.e-stat.go.jp"
LIST_QUERY = "うなぎ（稚魚、活、調製品）"
LIST_PAGES = 4
MAX_DOWNLOADS = 12  # 실행당 신규 파일 다운로드 상한 (예의)

FIELDS = ["month", "item", "import_kg", "jpy_thousand", "jpy_per_kg",
          "usd_per_kg", "fx_jpy_usd", "collected"]
# 일본 통계의 うなぎ는 뱀장어속(Anguilla) = 민물장어 전용 분류 (바다장어 별도)
ITEMS = [("민물장어 치어(실뱀장어)", 1), ("활 민물장어(뱀장어)", 6),
         ("조제 민물장어(양념구이)", 11)]  # (이름, 블록 시작 컬럼)
RENAME = {"치어": "민물장어 치어(실뱀장어)", "활장어": "활 민물장어(뱀장어)",
          "조제장어": "조제 민물장어(양념구이)"}

_fx_cache: dict[str, float | None] = {}


def fx_rate(month: str) -> float | None:
    """해당 월의 USD→JPY 환율 (ECB 기준, 월중 15일)."""
    if month in _fx_cache:
        return _fx_cache[month]
    date = f"{month}-15"
    if date >= datetime.now(tp.KST).strftime("%Y-%m-%d"):
        date = "latest"
    try:
        raw, _f, _c = tp.fetch(f"https://api.frankfurter.app/{date}?from=USD&to=JPY")
        rate = float(json.loads(raw)["rates"]["JPY"])
    except Exception as e:
        tp.log(f"  환율 조회 실패({month}): {e}")
        rate = None
    _fx_cache[month] = rate
    return rate


def discover_ids() -> list[str]:
    ids: list[str] = []
    for page in range(1, LIST_PAGES + 1):
        url = (f"{BASE}/stat-search/files?page={page}&layout=datalist"
               f"&query={quote(LIST_QUERY)}")
        try:
            raw, _f, ctype = tp.fetch(url)
        except Exception as e:
            tp.log(f"목록 페이지 {page} 실패: {e}")
            continue
        html = tp.decode_html(raw, ctype)
        found = re.findall(r"statInfId=(\d{12})", html)
        for sid in found:
            if sid not in ids:
                ids.append(sid)
        tp.log(f"목록 p{page}: 누적 {len(ids)}개 ID")
        if not found:
            break
        time.sleep(1)
    return ids


def parse_file(sid: str) -> dict | None:
    """파일 하나를 내려받아 {'month':..., '활장어': (kg, 천엔), ...} 반환."""
    import xlrd
    url = f"{BASE}/stat-search/file-download?statInfId={sid}&fileKind=0"
    raw, _f, _c = tp.fetch(url)
    if raw[:4] != b"\xd0\xcf\x11\xe0":
        tp.log(f"  {sid}: xls 아님 (magic={raw[:4]!r}) — 건너뜀")
        return None
    path = f"/tmp/unagi_{sid}.xls"
    open(path, "wb").write(raw)
    ws = xlrd.open_workbook(path).sheet_by_index(0)

    import unicodedata
    header = unicodedata.normalize(
        "NFKC", " ".join(str(ws.cell_value(r, 0)) for r in range(min(6, ws.nrows))))
    ym = re.search(r"[（(](\d{4})[）)]", header)
    mm = re.search(r"月次[：:]\s*(\d+)\s*月", header)
    if not ym or not mm:
        tp.log(f"  {sid}: 년월 식별 실패 — {header[:80]}")
        return None
    if "うなぎ" not in header and "うなぎ" not in str(ws.cell_value(3, 0)):
        return None
    result = {"month": f"{ym.group(1)}-{int(mm.group(1)):02d}"}

    china_row = None
    for r in range(ws.nrows):
        name = str(ws.cell_value(r, 0))
        if "中華人民共和国" in name or name.strip() == "中国":
            china_row = r
            break
    if china_row is None:
        tp.log(f"  {sid} ({result['month']}): 중국 행 없음")
        return result

    def num(r: int, c: int) -> float:
        if c >= ws.ncols:
            return 0.0
        v = ws.cell_value(r, c)
        try:
            return float(str(v).replace(",", "") or 0)
        except ValueError:
            return 0.0

    for item, col in ITEMS:
        kg = num(china_row, col + 3)        # 第二数量 (KG)
        kyen = num(china_row, col + 4)      # 金額(千円)
        if kg > 0 and kyen > 0:
            result[item] = (kg, kyen)
    return result


def main() -> int:
    today = datetime.now(tp.KST).strftime("%Y-%m-%d")
    existing = mp.load_csv(CSV_PATH)
    have_months = {r.get("month") for r in existing}

    ids = discover_ids()
    if not ids:
        tp.log("e-Stat 목록에서 파일 ID를 찾지 못했습니다 — 신규 수집 없이 진행")

    fresh: list[dict] = []
    downloads = 0
    for sid in ids:
        if downloads >= MAX_DOWNLOADS:
            break
        try:
            info = parse_file(sid)
        except Exception as e:
            tp.log(f"  {sid}: 파싱 실패 {e}")
            continue
        downloads += 1
        time.sleep(1)
        if not info:
            continue
        month = info["month"]
        if month in have_months:
            tp.log(f"  {sid} ({month}): 이미 수집됨")
            continue
        got = False
        for item, _col in ITEMS:
            if item in info:
                kg, kyen = info[item]
                per = kyen * 1000 / kg
                rate = fx_rate(month)
                usd = f"{per / rate:.2f}" if rate else ""
                fresh.append({
                    "month": month, "item": item,
                    "import_kg": str(int(kg)), "jpy_thousand": str(int(kyen)),
                    "jpy_per_kg": f"{per:.0f}", "usd_per_kg": usd,
                    "fx_jpy_usd": f"{rate:.2f}" if rate else "",
                    "collected": today,
                })
                tp.log(f"  {month} {item}: {int(kg):,}kg → ¥{per:,.0f}/kg"
                       + (f" (${usd}/kg)" if usd else ""))
                got = True
        if not got:
            tp.log(f"  {sid} ({month}): 중국 실적 없음")
        have_months.add(month)

    # 기존 행 마이그레이션: 옛 품목명 교체 + USD 단가 보충
    migrated = False
    for r in existing:
        if r.get("item") in RENAME:
            r["item"] = RENAME[r["item"]]
            migrated = True
        if not r.get("usd_per_kg") and r.get("jpy_per_kg"):
            rate = fx_rate(r.get("month", ""))
            if rate:
                r["usd_per_kg"] = f"{float(r['jpy_per_kg']) / rate:.2f}"
                r["fx_jpy_usd"] = f"{rate:.2f}"
                migrated = True

    if fresh or migrated:
        keys = {(r["month"], r["item"]) for r in fresh}
        kept = [r for r in existing
                if (r.get("month"), r.get("item")) not in keys]
        merged = kept + fresh
        merged.sort(key=lambda r: (r.get("month", ""), r.get("item", "")))
        mp.save_csv(CSV_PATH, merged, FIELDS)
        tp.log(f"\njapan_import_history.csv 갱신: 신규 {len(fresh)}건"
               + (", 기존 행 마이그레이션" if migrated else ""))
    else:
        tp.log("\n신규 월 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
