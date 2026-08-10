#!/usr/bin/env python3
"""KATI 품목트리·국가코드·조회 POST 정찰 (임시)."""
import json
import re
import sys
import urllib.request
from urllib.parse import urlencode

import track_price as tp

BASE = "https://www.kati.net"


def post(path: str, data: dict) -> bytes:
    req = urllib.request.Request(
        BASE + path, data=urlencode(data).encode(),
        headers={"User-Agent": tp.UA, "Accept": "*/*",
                 "X-Requested-With": "XMLHttpRequest",
                 "Referer": BASE + "/statistics/monthlyPerformanceByProduct.do"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


print("########## 1) 품목 트리에서 장어 찾기")
for digits in ("4", "6", "2", "10"):
    try:
        raw = post("/stat/search/item/tree.do",
                   {"ssi_see_together": "N", "ssi_digits": digits})
    except Exception as e:
        print(f"  digits={digits} FAIL {e}")
        continue
    text = raw.decode("utf-8", "replace")
    print(f"  digits={digits} bytes={len(raw)} head={text[:150]}")
    hits = [m.start() for m in re.finditer("장어", text)]
    print(f"  '장어' {len(hits)}회")
    for p in hits[:8]:
        print("   ctx:", text[max(0, p - 220):p + 120].replace("\n", " "))
    if hits:
        break

print("\n########## 2) 페이지에서 국가 관련 ajax 찾기")
raw, _, ctype = tp.fetch(BASE + "/statistics/monthlyPerformanceByProduct.do")
html = tp.decode_html(raw, ctype)
for m in re.finditer(r"(/stat[^\"']*\.do)", html):
    print("  stat-endpoint:", m.group(1))
seen = set()
for m in re.finditer(r"nation", html, re.I):
    s = re.sub(r"\s+", " ", html[max(0, m.start() - 120):m.start() + 250])
    if ".do" in s and s not in seen:
        seen.add(s)
        print("  nation-ctx:", s[:280])
        if len(seen) >= 6:
            break

print("\n########## 3) 국가 트리 시도")
for path, data in (
    ("/stat/search/nation/tree.do", {}),
    ("/stat/search/nation/tree.do", {"ssn_see_together": "N"}),
    ("/stat/search/nation/list.do", {}),
):
    try:
        raw = post(path, data)
        text = raw.decode("utf-8", "replace")
        print(f"  {path} bytes={len(raw)} head={text[:200]}")
        for p in [m.start() for m in re.finditer("중국", text)][:3]:
            print("   중국-ctx:", text[max(0, p - 200):p + 80].replace("\n", " "))
        if "중국" in text:
            break
    except Exception as e:
        print(f"  {path} FAIL {e}")
sys.exit(0)
