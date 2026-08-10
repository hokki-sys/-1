#!/usr/bin/env python3
"""산지/도매 시세 소스 구조 정찰용 임시 스크립트 (파악 후 제거 예정)."""
import re
import sys

import track_price as tp

URLS = [
    "https://fishdata.kmi.re.kr/front/kmisys/seafoodPriceTrends.do",
    "https://fishdata.kmi.re.kr/",
    "https://www.sisepan.com/nong-dome-price?type=3&mode=ca2&id=1916",
]

for url in URLS:
    print(f"\n########## PROBE {url}")
    try:
        raw, final, ctype = tp.fetch(url)
    except Exception as e:
        print(f"  FETCH FAIL: {e}")
        continue
    html = tp.decode_html(raw, ctype)
    text = tp.strip_tags(html)
    print(f"  final={final}\n  content-type={ctype} bytes={len(raw)}")
    print(f"  title={tp.extract_title(html)}")
    print(f"  text[:800]= {text[:800]}")
    for p in [m.start() for m in re.finditer("뱀장어|민물장어|장어", text)][:6]:
        print("  eel-ctx:", text[max(0, p - 70):p + 130])
    endpoints = sorted(set(re.findall(
        r'["\']((?:https?://[^"\']+|/[^"\']{3,})\.(?:do|json|jsp|php)[^"\']*)', html)))[:40]
    print("  endpoints:")
    for e in endpoints:
        print("   ", e)
    forms = re.findall(r'<form[^>]+action=["\']([^"\']+)', html, re.I)[:10]
    print("  form-actions:", forms)

sys.exit(0)
