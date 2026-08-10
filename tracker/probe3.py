#!/usr/bin/env python3
"""수입가격 무키(無key) 소스 정찰 (임시): 관세청 무역통계·수산정보포털·KATI."""
import re
import sys

import track_price as tp


def probe(url: str, keywords: tuple[str, ...] = ("수출입", "통계", "품목", "무역")) -> None:
    print(f"\n########## PROBE {url}")
    try:
        raw, final, ctype = tp.fetch(url)
    except Exception as e:
        print(f"  FETCH FAIL: {e}")
        return
    html = tp.decode_html(raw, ctype)
    text = tp.strip_tags(html)
    print(f"  final={final} bytes={len(raw)} title={tp.extract_title(html)}")
    print(f"  text[:300]= {text[:300]}")
    seen = set()
    n = 0
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.S | re.I):
        href = m.group(1)
        label = re.sub(r"\s+", " ", tp.strip_tags(m.group(2))).strip()[:35]
        if href in seen or not label:
            continue
        if any(k in label for k in keywords) or any(k in href.lower() for k in ("trade", "stat", "imexport")):
            seen.add(href)
            print(f"  link: {href}  [{label}]")
            n += 1
            if n >= 30:
                break
    ends = sorted(set(re.findall(
        r'["\']((?:https?://[^"\']+|/[^"\']{3,})\.(?:do|json|jsp|php)[^"\']*)', html)))[:20]
    print("  endpoints:", ends)


probe("https://tradedata.go.kr/cts/index.do")
probe("https://www.fips.go.kr/")
probe("https://www.kati.net/statistics/monthlyPerformanceByProduct.do")
probe("https://www.kati.net/")
sys.exit(0)
