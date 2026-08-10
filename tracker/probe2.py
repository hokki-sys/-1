#!/usr/bin/env python3
"""원물시세 소스 확장 정찰 2차 (임시): KMI 위판정보·가락·노량진·agromarket."""
import re
import sys
import urllib.request

import track_price as tp


def dump(url: str, html: str) -> None:
    text = tp.strip_tags(html)
    print(f"  title={tp.extract_title(html)}")
    eel_hits = [m.start() for m in re.finditer("뱀장어|민물장어", text)]
    print(f"  '뱀장어/민물장어' 등장 {len(eel_hits)}회")
    for p in eel_hits[:6]:
        print("   eel-ctx:", text[max(0, p - 80):p + 140])
    for m in list(re.finditer(r'<select[^>]*(?:name|id)=["\']([^"\']+)["\'](.*?)</select>', html, re.S | re.I))[:10]:
        opts = re.findall(r'<option[^>]*value=["\']([^"\']*)["\'][^>]*>\s*([^<]*)', m.group(2))
        eel = [o for o in opts if "장어" in o[1]]
        print(f"  select={m.group(1)} n_opts={len(opts)} first={opts[:6]}{' EEL:' + str(eel) if eel else ''}")
    ends = sorted(set(re.findall(r'["\']((?:https?://[^"\']+|/[^"\']{3,})\.(?:do|json|jsp|php)[^"\']*)', html)))[:25]
    print("  endpoints:", ends)
    for m in list(re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]{0,30})</a>', html))[:400]:
        if any(k in m.group(2) for k in ("시세", "가격", "경락", "경매")):
            print(f"  link: {m.group(1)}  [{m.group(2).strip()[:30]}]")


def probe(url: str) -> None:
    print(f"\n########## PROBE {url}")
    try:
        raw, final, ctype = tp.fetch(url)
    except Exception as e:
        print(f"  FETCH FAIL: {e}")
        return
    print(f"  final={final} bytes={len(raw)}")
    dump(url, tp.decode_html(raw, ctype))


probe("https://fishdata.kmi.re.kr/front/kmisys/prdlcCttmltDataList.do")
probe("https://www.garak.co.kr/")
probe("https://www.susansijang.co.kr/nsis/miw/ko/main")

# at.agromarket.kr: 406 응답이라 Accept 헤더 변형으로 1회 재시도
print("\n########## PROBE https://at.agromarket.kr/ (헤더 변형)")
try:
    req = urllib.request.Request("https://at.agromarket.kr/", headers={
        "User-Agent": tp.UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    print(f"  OK bytes={len(raw)}")
    dump("https://at.agromarket.kr/", tp.decode_html(raw, resp.headers.get("Content-Type", "")))
except Exception as e:
    print(f"  FETCH FAIL: {e}")

sys.exit(0)
