#!/usr/bin/env python3
"""KATI 월별품목별 실적 페이지의 조회 파라미터 정찰 (임시)."""
import re
import sys

import track_price as tp

URL = "https://www.kati.net/statistics/monthlyPerformanceByProduct.do"
print(f"########## PROBE {URL}")
try:
    raw, final, ctype = tp.fetch(URL)
except Exception as e:
    print(f"  FETCH FAIL: {e}")
    sys.exit(0)
html = tp.decode_html(raw, ctype)
print(f"  bytes={len(raw)}")

print("  --- form 블록 ---")
for m in re.finditer(r"<form[^>]*>", html, re.I):
    print("   ", m.group(0)[:200])

print("  --- input/select 이름 ---")
for m in re.finditer(r'<(input|select)[^>]*name=["\']([^"\']+)["\'][^>]*>', html, re.I):
    print(f"    {m.group(1)} name={m.group(2)} :: {m.group(0)[:140]}")

print("  --- select 옵션 (장어 관련) ---")
for m in re.finditer(r'<select[^>]*name=["\']([^"\']+)["\'](.*?)</select>', html, re.S | re.I):
    opts = re.findall(r'<option[^>]*value=["\']([^"\']*)["\'][^>]*>\s*([^<]*)', m.group(2))
    eel = [o for o in opts if "장어" in o[1]]
    print(f"    select={m.group(1)} n={len(opts)} first={opts[:8]}")
    if eel:
        print(f"      EEL: {eel}")

print("  --- ajax/action 흔적 ---")
for pat in ("ajax", "action", "\\.do", "excel", "Excel"):
    for m in list(re.finditer(pat, html))[:6]:
        s = re.sub(r"\s+", " ", html[max(0, m.start() - 150):m.start() + 250])
        if any(k in s for k in (".do", "url", "action")):
            print(f"    [{pat}] …{s}…")
            break

print("  --- 품목 검색/자동완성 흔적 ---")
for m in list(re.finditer(r'["\'](/[^"\']*(?:prod|item|hs|code|search)[^"\']*\.do[^"\']*)["\']', html, re.I))[:20]:
    print("   ", m.group(1))
sys.exit(0)
