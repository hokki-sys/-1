#!/usr/bin/env python3
"""원물시세 소스 확장 정찰 (임시): 품종별 링크·검색 파라미터·데이터 엔드포인트 파악."""
import re
import sys

import track_price as tp


def probe(url: str, pats: list[str]) -> None:
    print(f"\n########## PROBE {url}")
    try:
        raw, final, ctype = tp.fetch(url)
    except Exception as e:
        print(f"  FETCH FAIL: {e}")
        return
    html = tp.decode_html(raw, ctype)
    print(f"  final={final} bytes={len(raw)} title={tp.extract_title(html)}")

    print("  --- 관련 링크 (장어/패턴 매칭) ---")
    seen = set()
    count = 0
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.S | re.I):
        href = m.group(1)
        txt = re.sub(r"\s+", " ", tp.strip_tags(m.group(2))).strip()[:40]
        if href in seen:
            continue
        if any(p in href for p in pats) or "장어" in txt or "가격" in txt or "시세" in txt or "경락" in txt:
            seen.add(href)
            print(f"    {href}  [{txt}]")
            count += 1
            if count >= 40:
                break

    print("  --- select 요소 ---")
    for m in list(re.finditer(r'<select[^>]*name=["\']([^"\']+)["\'](.*?)</select>', html, re.S | re.I))[:12]:
        opts = re.findall(r'<option[^>]*value=["\']([^"\']*)["\'][^>]*>\s*([^<]*)', m.group(2))
        eel = [o for o in opts if "장어" in o[1]]
        print(f"    name={m.group(1)} options={opts[:10]}{' EEL:' + str(eel) if eel else ''}")

    print("  --- hidden input ---")
    for m in list(re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', html, re.I))[:20]:
        print("   ", m.group(0)[:150])

    print("  --- 엔드포인트 주변 JS ---")
    for pat in pats:
        hits = list(re.finditer(re.escape(pat), html))[:2]
        for h in hits:
            snippet = re.sub(r"\s+", " ", html[max(0, h.start() - 350):h.start() + 550])
            print(f"    [{pat}] …{snippet}…")

    print("  --- page/date 파라미터 흔적 ---")
    for m in list(re.finditer(r'["\']([^"\']*(?:page|date|sdate|edate|startDate|endDate)[^"\']*)["\']', html, re.I))[:15]:
        print("   ", m.group(1)[:120])


probe("https://www.sisepan.com/nong-dome-price?type=3&mode=ca2&id=1916",
      ["nong-dome-price", "nong-price"])
probe("https://at.agromarket.kr/", ["price", "Price", "domeinfo", "trade"])
probe("https://fishdata.kmi.re.kr/front/kmisys/seafoodPriceTrends.do",
      ["CttmltDataList"])
sys.exit(0)
