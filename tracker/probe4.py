#!/usr/bin/env python3
"""KATI 수입조회(HS코드 지정) 결과 구조 정찰 v4 (임시)."""
import re
import sys
import urllib.request
from urllib.parse import urlencode

import track_price as tp

BASE = "https://www.kati.net"
PAGE = "/statistics/monthlyPerformanceByProduct.do"
HS = ["0301920000", "0303260000", "1604170000"]


def post(path: str, data: dict) -> str:
    req = urllib.request.Request(
        BASE + path, data=urlencode(data, doseq=True).encode(),
        headers={"User-Agent": tp.UA, "Accept": "*/*",
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "Referer": BASE + PAGE})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


BASE_DATA = {
    "ieStandard": "I", "basisYear": "2026", "basisMonth": "06",
    "basisYearMonth": "202606", "period": "2", "unit": "Kg",
    "nations": "CN", "nationSearchType": "4", "productType": "P",
    "classification": "0", "productRowEnabled": "true",
    "showComparisonBasisMonth": "true", "showComparisonMonthTotal": "true",
}

for code_type in ("HS", "2", "1", "AG", ""):
    data = dict(BASE_DATA)
    data["codeType"] = code_type
    data["products"] = HS
    try:
        body = post(PAGE, data)
    except Exception as e:
        print(f"codeType={code_type!r} FAIL {e}")
        continue
    text = tp.strip_tags(body)
    basis = re.search(r"수출입기준\s*:\s*(\S+)", text)
    sel = re.search(r"선택 품목\s*:\s*(.{0,80}?)\s*\*", text)
    eel = text.count("장어")
    print(f"codeType={code_type!r} -> {len(body)}b 기준={basis.group(1) if basis else '?'} "
          f"품목=[{sel.group(1) if sel else '?'}] 장어 {eel}회")
    if eel:
        for p in [m.start() for m in re.finditer("장어", text)][:6]:
            print("   ctx:", re.sub(r"\s+", " ", text[max(0, p - 150):p + 350]))
        print("\n--- 테이블 헤더 부근 ---")
        p = text.find("품목명")
        if p < 0:
            p = text.find("합계")
        print(re.sub(r"\s+", " ", text[max(0, p - 100):p + 600]))
        break
sys.exit(0)
