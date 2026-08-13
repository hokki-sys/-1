#!/usr/bin/env python3
"""일본 무역통계 접근 경로 정찰 (임시): customs.go.jp 검색 URL 구조, e-Stat CSV."""
import re
import sys

import track_price as tp


def probe(url: str, dump_js_around: tuple[str, ...] = ()) -> str | None:
    print(f"\n########## PROBE {url}")
    try:
        raw, final, ctype = tp.fetch(url)
    except Exception as e:
        print(f"  FETCH FAIL: {e}")
        return None
    html = tp.decode_html(raw, ctype)
    print(f"  final={final} bytes={len(raw)} title={tp.extract_title(html)}")
    for m in list(re.finditer(r'<(?:a|frame|iframe)[^>]+(?:href|src)=["\']([^"\']+)["\'][^>]*>', html, re.I))[:60]:
        u = m.group(1)
        if any(k in u.lower() for k in ("srch", "search", "csv", "download", "futsu", "index")):
            print("  link:", u[:160])
    for pat in dump_js_around:
        for h in list(re.finditer(re.escape(pat), html))[:3]:
            print(f"  [{pat}] …{re.sub(chr(10), ' ', html[max(0, h.start() - 250):h.start() + 450])[:650]}…")
    return html


# 1) 재무성 무역통계 검색 구조
probe("https://www.customs.go.jp/toukei/srch/index.htm", ("P=", "function"))
probe("https://www.customs.go.jp/toukei/search/futsu1.htm", ("P=",))

# 2) 검색 결과 URL 직접 시도 (알려진 M=01 상품별국별 형식 추정 몇 가지)
for p in (
    "https://www.customs.go.jp/toukei/srch/indexe.htm?M=01&P=1,1,,,,,,,,4,1,2026,0,0,0,2,160417000,,,,,,,,,,1,105,,,,,,,,,,,,,,,,,,,,,,,,",
    "https://www.customs.go.jp/toukei/srch/index.htm?M=01&P=1,1,,,,,,,,4,1,2026,0,0,0,2,160417000,,,,,,,,,,1,105,,,,,,,,,,,,,,,,,,,,,,,,",
):
    html = probe(p)
    if html:
        text = tp.strip_tags(html)
        for kw in ("160417", "China", "中国", "うなぎ"):
            pos = text.find(kw)
            if pos >= 0:
                print(f"   [{kw}] …{text[max(0, pos - 120):pos + 260]}…")

# 3) e-Stat 우나기 데이터셋 파일 링크
html = probe("https://www.e-stat.go.jp/stat-search/files?page=1&layout=dataset&query=%E3%81%86%E3%81%AA%E3%81%8E&stat_infid=000032208083")
if html:
    for m in list(re.finditer(r'href=["\']([^"\']*file-download[^"\']*)["\']', html))[:10]:
        print("  download:", m.group(1)[:200])
    text = tp.strip_tags(html)
    pos = text.find("うなぎ")
    print("  ctx:", text[max(0, pos - 100):pos + 400] if pos >= 0 else text[:400])
sys.exit(0)
