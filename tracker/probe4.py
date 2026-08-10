#!/usr/bin/env python3
"""KATI 수입 전체테이블 원문 분석 + 품목트리 BFS 정찰 v5 (임시)."""
import json
import re
import sys
import urllib.request
from urllib.parse import urlencode

import track_price as tp

BASE = "https://www.kati.net"
PAGE = "/statistics/monthlyPerformanceByProduct.do"


def post(path: str, data: dict) -> str:
    req = urllib.request.Request(
        BASE + path, data=urlencode(data, doseq=True).encode(),
        headers={"User-Agent": tp.UA, "Accept": "*/*",
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "Referer": BASE + PAGE})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


print("########## 1) 품목트리 BFS로 장어 AG코드 찾기")
eel_nodes = []
try:
    roots = json.loads(post("/stat/search/item/tree.do",
                            {"ssi_see_together": "N", "ssi_digits": "2"})).get("tree", [])
    print("  roots:", [(r.get("name"), r.get("agcd")) for r in roots])
    queue = [r for r in roots if r.get("name") in ("수산물", "농산물")]
    depth = 0
    while queue and depth < 3 and len(eel_nodes) < 12:
        nxt = []
        for node in queue[:40]:
            try:
                body = post("/stat/search/item/sub.do",
                            {"high_agcd": node.get("agcd", ""), "ssi_see_together": "N"})
                children = json.loads(body).get("list") or json.loads(body).get("tree") or []
            except Exception as e:
                print(f"   sub({node.get('name')}) FAIL {e}")
                continue
            for c in children:
                nm = c.get("name") or ""
                if "장어" in nm:
                    eel_nodes.append(c)
                    print(f"   EEL: name={nm} agcd={c.get('agcd')} hscd={c.get('hscd')} "
                          f"high={c.get('high_agcd')}")
                nxt.append(c)
        queue = nxt
        depth += 1
    print(f"  BFS 종료 depth={depth}, eel {len(eel_nodes)}건")
except Exception as e:
    print("  트리 실패:", e)

print("\n########## 2) 수입 전체테이블 원문 분석")
data = {"ieStandard": "I", "basisYear": "2026", "basisMonth": "06",
        "basisYearMonth": "202606", "period": "2", "unit": "Kg",
        "nations": "CN", "nationSearchType": "4", "productType": "P",
        "classification": "0", "productRowEnabled": "true"}
body = post(PAGE, data)
print(f"  raw={len(body)}b strip={len(tp.strip_tags(body))}b tr={body.count('<tr')} 장어(raw)={body.count('장어')}")
for p in [m.start() for m in re.finditer("장어", body)][:4]:
    print("   raw-ctx:", re.sub(r"\s+", " ", body[max(0, p - 350):p + 250]))

print("\n########## 3) 장어 AG코드로 품목 지정 수입조회")
if eel_nodes:
    agcds = [n.get("agcd") for n in eel_nodes if n.get("agcd")][:4]
    data2 = dict(data)
    data2["products"] = agcds
    data2["selectedAgcd"] = ",".join(agcds)
    body2 = post(PAGE, data2)
    text2 = tp.strip_tags(body2)
    sel = re.search(r"선택 품목\s*:\s*(.{0,100}?)\s*\*", text2)
    print(f"  agcds={agcds} -> {len(body2)}b 품목=[{sel.group(1) if sel else '?'}] "
          f"장어(strip)={text2.count('장어')}")
    for p in [m.start() for m in re.finditer("장어", text2)][:8]:
        print("   ctx:", re.sub(r"\s+", " ", text2[max(0, p - 120):p + 400]))
sys.exit(0)
