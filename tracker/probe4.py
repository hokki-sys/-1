#!/usr/bin/env python3
"""KATI 수입기준 파라미터·장어 AG코드 정찰 v3 (임시)."""
import re
import sys
import urllib.request
from urllib.parse import urlencode

import track_price as tp

BASE = "https://www.kati.net"
PAGE = "/statistics/monthlyPerformanceByProduct.do"


def post(path: str, data: dict, timeout: int = 40) -> str:
    req = urllib.request.Request(
        BASE + path, data=urlencode(data, doseq=True).encode(),
        headers={"User-Agent": tp.UA, "Accept": "*/*",
                 "X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "Referer": BASE + PAGE})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


print("########## 1) 기준 페이지 라디오/체크박스/히든 전수 덤프")
raw, _, ctype = tp.fetch(BASE + PAGE)
html = tp.decode_html(raw, ctype)
count = 0
for m in re.finditer(r'<input[^>]+type=["\'](?:radio|checkbox|hidden)["\'][^>]*>', html, re.I):
    tag = re.sub(r"\s+", " ", m.group(0))
    print("   ", tag[:180])
    count += 1
    if count >= 60:
        break

print("\n########## 2) 품목 트리 (digits=2) 에서 뿌리 확인")
tree2 = None
try:
    tree2 = post("/stat/search/item/tree.do", {"ssi_see_together": "N", "ssi_digits": "2"})
    print(f"  len={len(tree2)} head={re.sub(chr(10), ' ', tree2[:600])}")
except Exception as e:
    print("  FAIL", e)

print("\n########## 3) 품목 검색 param 조합")
for data in (
    {"ssi_word": "장어", "ssi_digits": "6", "ssi_see_together": "N"},
    {"ssi_word": "장어", "ssi_digits": "4", "ssi_see_together": "N"},
    {"ssi_word": "장어", "ssi_tp": "AG", "ssi_see_together": "N"},
    {"ssi_word": "장어", "ssi_digits": "6", "ssi_tp": "1"},
):
    try:
        body = post("/stat/search/item/search.do", data)
        n = body.count("장어")
        print(f"  {data} -> {len(body)}b, 장어 {n}회")
        if '"list":[]' not in body and n > 1:
            for p in [m.start() for m in re.finditer("장어", body)][:10]:
                print("    ctx:", re.sub(r"\s+", " ", body[max(0, p - 300):p + 60]))
            break
    except Exception as e:
        print(f"  {data} FAIL {e}")

print("\n########## 4) 수입 기준 본조회 시험 (분류합계에서 수입 표기 확인)")
for extra in ({"sch_gubun": "2"}, {"gubun": "2"}, {"expImp": "I"}, {"sch_type": "import"},
              {"performanceType": "import"}, {"sch_gubun": "수입"}):
    data = {"basisYear": "2026", "basisMonth": "06", "nations": "CN", **extra}
    try:
        body = post(PAGE, data, timeout=60)
        text = tp.strip_tags(body)
        m = re.search(r"수출입기준\s*:\s*(\S+)", text)
        basis = m.group(1) if m else "?"
        print(f"  {extra} -> {len(body)}b, 수출입기준={basis}")
        if basis.startswith("수입"):
            p = text.find("농수산식품")
            print("    table-ctx:", re.sub(r"\s+", " ", text[p:p + 500]))
            break
    except Exception as e:
        print(f"  {extra} FAIL {e}")
sys.exit(0)
