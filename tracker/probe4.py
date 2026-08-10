#!/usr/bin/env python3
"""KATI 품목/국가 검색 → 실조회 연쇄 정찰 (임시)."""
import json
import re
import sys
import urllib.request
from urllib.parse import urlencode

import track_price as tp

BASE = "https://www.kati.net"


def post(path: str, data: dict) -> str:
    req = urllib.request.Request(
        BASE + path, data=urlencode(data).encode(),
        headers={"User-Agent": tp.UA, "Accept": "*/*",
                 "X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "Referer": BASE + "/statistics/monthlyPerformanceByProduct.do"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def try_post(path: str, data: dict) -> str | None:
    try:
        body = post(path, data)
        print(f"\n== POST {path} {data} -> {len(body)}b")
        print("  head:", re.sub(r"\s+", " ", body[:500]))
        return body
    except Exception as e:
        print(f"\n== POST {path} {data} -> FAIL {e}")
        return None


agcd_candidates = []
print("########## 품목 검색")
for path, data in (
    ("/stat/search/item/search.do", {"ssi_word": "장어"}),
    ("/stat/search/item/search.do", {"searchWord": "장어"}),
    ("/statistics/searchInterestProduct.do", {"ssi_word": "장어"}),
    ("/stat/search/item/tree.do", {"ssi_see_together": "N", "ssi_digits": "6"}),
):
    body = try_post(path, data)
    if not body or "장어" not in body:
        continue
    for p in [m.start() for m in re.finditer("장어", body)][:10]:
        ctx = re.sub(r"\s+", " ", body[max(0, p - 260):p + 80])
        print("   장어-ctx:", ctx)
    for m in re.finditer(r'"?(agcd|ag_cd|agCd)"?\s*[:=]\s*"?([0-9A-Za-z]+)', body):
        agcd_candidates.append(m.group(2))
    if agcd_candidates:
        break
print("agcd 후보:", agcd_candidates[:10])

nation_code = None
print("\n########## 국가 검색")
for path, data in (
    ("/statistics/searchNation.do", {"ssn_word": "중국"}),
    ("/statistics/searchNation.do", {"searchWord": "중국"}),
    ("/statistics/searchNation.do", {"nationNm": "중국"}),
    ("/statistics/searchNation.do", {}),
):
    body = try_post(path, data)
    if not body or "중국" not in body:
        continue
    for p in [m.start() for m in re.finditer("중국", body)][:6]:
        ctx = re.sub(r"\s+", " ", body[max(0, p - 260):p + 60])
        print("   중국-ctx:", ctx)
    m = re.search(r'"?(?:code|cd|nationCd|ssn_code)"?\s*[:=]\s*"?([0-9A-Za-z]+)"?[^{}]{0,120}중국', body)
    if not m:
        m = re.search(r'중국[^{}]{0,120}?"?(?:code|cd|nationCd)"?\s*[:=]\s*"?([0-9A-Za-z]+)', body)
    if m:
        nation_code = m.group(1)
        break
print("nation code:", nation_code)

print("\n########## 본조회 시도")
prods = agcd_candidates[:2] or ["0403"]
nat = nation_code or "CN"
for path in ("/statistics/monthlyPerformanceByProduct.do",):
    data = {"basisYear": "2026", "basisMonth": "06",
            "products": prods[0], "nations": nat}
    try:
        body = post(path, data)
        print(f"POST {path} {data} -> {len(body)}b")
        text = tp.strip_tags(body)
        for kw in ("장어", "중국", "수입", "합계"):
            hits = [m.start() for m in re.finditer(kw, text)][:3]
            for p in hits:
                print(f"   [{kw}]", re.sub(r"\s+", " ", text[max(0, p - 120):p + 160]))
    except Exception as e:
        print(f"POST {path} FAIL {e}")
sys.exit(0)
