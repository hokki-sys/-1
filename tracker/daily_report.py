#!/usr/bin/env python3
"""일일 가격 리포트 작성기.

세 데이터 층(거래처 상품, 원물 경매, 중국산 수입실적)의 최신 상태와
변동 여부를 요약한 이메일 본문(tracker/.daily_report.txt)을 만들고,
GitHub Actions 출력으로 제목(subject)을 내보낸다. 변동이 있든 없든
매일 발송하는 용도라서 '변동 없음'도 명시적으로 적는다.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import market_price as mp
import track_price as tp

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / ".daily_report.txt"
REPO_URL = "https://github.com/hokki-sys/-1/tree/claude/cloud-work-save-check-l1icvs/tracker"


def fmt_won(v: str | int) -> str:
    return f"{int(v):,}원"


def product_section(today: str) -> tuple[list[str], bool]:
    rows = mp.load_csv(ROOT / "price_history.csv")
    ok = [r for r in rows if r.get("status") == "ok" and r.get("price")]
    lines: list[str] = []
    changed = False
    labels = sorted({r["label"] for r in ok})
    for label in labels:
        mine = [r for r in ok if r["label"] == label]
        latest = mine[-1]
        prev = next((r for r in reversed(mine) if r["date"] < latest["date"]), None)
        if prev and prev["price"] != latest["price"]:
            pct = (int(latest["price"]) - int(prev["price"])) / int(prev["price"]) * 100
            lines.append(f"- {label}: {fmt_won(latest['price'])} "
                         f"(변동 있음: {fmt_won(prev['price'])} → {fmt_won(latest['price'])}, {pct:+.1f}%)")
            changed = True
        else:
            since = f", {latest['date']} 기준" if latest["date"] != today else ""
            lines.append(f"- {label}: {fmt_won(latest['price'])} (변동 없음{since})")
    failed = [r for r in rows if r.get("date") == today and r.get("status") != "ok"]
    for r in failed:
        lines.append(f"- {r['label']}: 수집 실패 ({r['status']})")
    if not lines:
        lines.append("- 데이터 없음")
    return lines, changed


def wonmul_section(today: str) -> tuple[list[str], bool]:
    rows = mp.load_csv(ROOT / "wonmul_history.csv")
    lines: list[str] = []
    new_today = [r for r in rows if r.get("collected") == today]
    if new_today:
        lines.append(f"- 오늘 신규 경매 {len(new_today)}건:")
        for r in sorted(new_today, key=lambda r: r.get("auction_date", ""))[-5:]:
            lines.append(f"    {r['auction_date']} {r['market']} {r['species']} "
                         f"{r['spec_kg']}kg {r['grade']} — kg당 {fmt_won(r['price_per_kg'])}")
    else:
        lines.append("- 오늘 신규 경매 없음 (변동 없음)")
    recent = sorted(rows, key=lambda r: r.get("auction_date", ""))[-3:]
    if recent:
        lines.append("- 최근 경매 시세:")
        for r in recent:
            lines.append(f"    {r['auction_date']} {r['market']} {r['species']} "
                         f"{r['grade']} — kg당 {fmt_won(r['price_per_kg'])}")
    return lines, bool(new_today)


def import_section(today: str) -> tuple[list[str], bool]:
    rows = [r for r in mp.load_csv(ROOT / "import_history.csv") if r.get("usd_per_kg")]
    lines: list[str] = []
    if not rows:
        return ["- 데이터 없음"], False
    latest_month = max(r["month"] for r in rows)
    changed = any(r.get("collected") == today and r["month"] == latest_month for r in rows)
    lines.append(f"- 최신 확정월: {latest_month}"
                 + (" (오늘 갱신됨)" if changed else " (변동 없음)"))
    for r in sorted(rows, key=lambda r: r["hs"]):
        if r["month"] != latest_month:
            continue
        prev = [p for p in rows if p["hs"] == r["hs"] and p["month"] < latest_month]
        delta = ""
        if prev:
            p = max(prev, key=lambda p: p["month"])
            pct = (float(r["usd_per_kg"]) - float(p["usd_per_kg"])) / float(p["usd_per_kg"]) * 100
            delta = f" (전월 ${p['usd_per_kg']}, {pct:+.1f}%)"
        lines.append(f"    {r['label']}: ${r['usd_per_kg']}/kg, "
                     f"{int(r['import_kg']):,}kg{delta}")
    return lines, changed


def main() -> int:
    today = datetime.now(tp.KST).strftime("%Y-%m-%d")
    p_lines, p_changed = product_section(today)
    w_lines, w_new = wonmul_section(today)
    i_lines, i_new = import_section(today)

    any_change = p_changed or w_new or i_new
    if p_changed:
        status = "가격 변동 있음"
    elif w_new or i_new:
        status = "신규 데이터 있음"
    else:
        status = "변동 없음"

    body = "\n".join([
        f"[장어 가격 일일 리포트] {today} (KST)",
        "",
        f"■ 오늘의 요약: {status}",
        "",
        "1) 거래처 상품 (푸드엔)",
        *p_lines,
        "",
        "2) 원물 도매 경매가 (전국 공영도매시장)",
        *w_lines,
        "",
        "3) 중국산 수입실적 (관세청 통관, USD/kg = CIF)",
        *i_lines,
        "",
        f"전체 이력 데이터: {REPO_URL}",
        "- 이 메일은 매일 자동 발송됩니다.",
    ])
    OUT_PATH.write_text(body, encoding="utf-8")
    tp.log(body)
    subject = f"[장어 시세] {today} — {status}"
    tp.gh_output("subject", subject)
    tp.gh_output("changed", "true" if any_change else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
