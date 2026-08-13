#!/usr/bin/env python3
"""일일 가격 리포트 작성기 (다기간 비교판).

세 데이터 층(거래처 상품, 원물 경매, 중국산 수입실적)의 현재 시세를
전일/전주/전월/전분기/전년과 비교해 요약한 본문(tracker/.daily_report.txt)을
만들고, GitHub Actions 출력으로 제목(subject)을 내보낸다. 변동이 있든 없든
매일 발송하는 용도라서 '변동 없음'도 명시적으로 적는다.

데이터가 아직 해당 기간만큼 쌓이지 않았으면 "—"로 표기한다 (일별 수집은
2026-08-10 시작, 원물 경매는 2022-11부터, 수입실적은 2025-02부터 존재).
"""
from __future__ import annotations

import os
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import market_price as mp
import track_price as tp

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / ".daily_report.txt"
REPO_URL = "https://github.com/hokki-sys/-1/tree/claude/cloud-work-save-check-l1icvs/tracker"

DAY_PERIODS = [("전일", 1), ("전주", 7), ("전월", 30), ("전분기", 91), ("전년", 365)]
MONTH_PERIODS = [("전월", 1), ("전분기", 3), ("전년", 12)]

IMPORT_ITEMS = {
    "1604179000": "조제 장어(양념구이류)",
    "0301929090": "활뱀장어(성만)",
    "0304690000": "필렛(민물어류 혼합)",
    "0301921000": "실장어(치어)",
}


def d(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def arrow(p: float) -> str:
    return "▲" if p > 0.05 else ("▼" if p < -0.05 else "─")


def cmp_line(cur: float, prev: float | None, unit: str, when: str = "") -> str:
    if prev is None or prev <= 0:
        return "—"
    p = (cur - prev) / prev * 100
    when_s = f", {when}" if when else ""
    if unit == "$":
        return f"{arrow(p)} {p:+.1f}% (${prev:,.2f}{when_s})"
    if unit == "¥":
        return f"{arrow(p)} {p:+.1f}% (¥{prev:,.0f}{when_s})"
    return f"{arrow(p)} {p:+.1f}% ({int(prev):,}원{when_s})"


def series_value_at(series: list[tuple[date, float]], target: date):
    """target일 이전(포함) 가장 최근 값. 없으면 None."""
    best = None
    for dt, v in series:
        if dt <= target:
            best = (dt, v)
        else:
            break
    return best


def multi_period_lines(series: list[tuple[date, float]], unit: str) -> list[str]:
    cur_date, cur = series[-1]
    lines = []
    for name, days in DAY_PERIODS:
        target = cur_date - timedelta(days=days)
        found = series_value_at(series[:-1], target)
        if found is None:
            lines.append(f"   {name}대비: — (데이터 없음)")
        else:
            fdt, fv = found
            lines.append(f"   {name}대비: {cmp_line(cur, fv, unit, fdt.strftime('%y/%m/%d'))}")
    return lines


def product_section() -> tuple[list[str], bool, list[str]]:
    rows = [r for r in mp.load_csv(ROOT / "price_history.csv")
            if r.get("status") == "ok" and r.get("price")]
    lines: list[str] = []
    changed = False
    notes: list[str] = []
    for label in sorted({r["label"] for r in rows}):
        per_date: dict[date, float] = {}
        for r in rows:
            if r["label"] == label:
                per_date[d(r["date"])] = float(r["price"])
        series = sorted(per_date.items())
        cur_date, cur = series[-1]
        lines.append(f"● {label}")
        lines.append(f"   현재가: {int(cur):,}원 ({cur_date.strftime('%m/%d')} 기준)")
        lines.extend(multi_period_lines(series, "원"))
        prev = series_value_at(series[:-1], cur_date - timedelta(days=1))
        if prev and prev[1] != cur:
            changed = True
            pct = (cur - prev[1]) / prev[1] * 100
            notes.append(f"거래처 {arrow(pct)}{abs(pct):.1f}%")
        lines.append("")
    if not lines:
        lines = ["- 데이터 없음", ""]
    return lines, changed, notes


def wonmul_section(today: str) -> tuple[list[str], bool, list[str]]:
    rows = [r for r in mp.load_csv(ROOT / "wonmul_history.csv")
            if r.get("species") == "뱀장어" and r.get("price_per_kg")]
    if not rows:
        return ["- 데이터 없음", ""], False, []
    auctions = sorted((d(r["auction_date"]), float(r["price_per_kg"])) for r in rows)

    def ref_at(target: date) -> float | None:
        pts = [v for dt, v in auctions if dt <= target][-5:]
        return statistics.median(pts) if pts else None

    cur_date = auctions[-1][0]
    cur = ref_at(cur_date)
    latest = [r for r in rows if d(r["auction_date"]) == cur_date][-1]
    new_today = [r for r in rows if r.get("collected") == today]

    lines = ["● 뱀장어 기준시세 (최근 5개 경매 중앙값, kg당)"]
    lines.append(f"   현재: {int(cur):,}원/kg — 최근 경매 {cur_date.strftime('%m/%d')} "
                 f"{latest['market']} {latest['grade']} {int(float(latest['price_per_kg'])):,}원/kg")
    for name, days in DAY_PERIODS:
        prev = ref_at(cur_date - timedelta(days=days))
        label = f"   {name}대비: "
        lines.append(label + (cmp_line(cur, prev, "원") if prev else "—"))
    if new_today:
        lines.append(f"   오늘 신규 경매 {len(new_today)}건 반영")
    lines.append("")
    notes = [f"경매 신규 {len(new_today)}건"] if new_today else []
    return lines, bool(new_today), notes


def month_shift(ym: str, back: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    idx = y * 12 + (m - 1) - back
    return f"{idx // 12}-{idx % 12 + 1:02d}"


def import_section(today: str) -> tuple[list[str], bool, list[str]]:
    rows = [r for r in mp.load_csv(ROOT / "import_history.csv") if r.get("usd_per_kg")]
    if not rows:
        return ["- 데이터 없음", ""], False, []
    latest_month = max(r["month"] for r in rows)
    updated = any(r.get("collected") == today and r["month"] == latest_month for r in rows)

    by_key: dict[tuple[str, str], dict] = {(r["hs"], r["month"]): r for r in rows}
    lines = [f"※ 최신 확정월 {latest_month}" + (" — 오늘 갱신됨" if updated else "")]
    for hs, short in IMPORT_ITEMS.items():
        cur_row = by_key.get((hs, latest_month))
        if not cur_row:
            continue
        cur = float(cur_row["usd_per_kg"])
        lines.append(f"● {short} [{hs[:4]}.{hs[4:6]}]")
        lines.append(f"   {latest_month}: ${cur:.2f}/kg, {int(cur_row['import_kg']):,}kg")
        for name, back in MONTH_PERIODS:
            pm = month_shift(latest_month, back)
            prow = by_key.get((hs, pm))
            if prow:
                pv = float(prow["usd_per_kg"])
                vol = f", {int(prow['import_kg']):,}kg"
                lines.append(f"   {name}대비: {cmp_line(cur, pv, '$', pm)}{vol}")
            else:
                lines.append(f"   {name}대비: — (해당 월 실적/데이터 없음)")
        lines.append("")
    notes = ["수입실적 갱신"] if updated else []
    return lines, updated, notes


def japan_section(today: str) -> tuple[list[str], bool, list[str]]:
    rows = [r for r in mp.load_csv(ROOT / "japan_import_history.csv")
            if r.get("jpy_per_kg")]
    if not rows:
        return ["- 데이터 수집 중 (일본 재무성 무역통계, 수집 시작 후 표시)", ""], False, []
    latest = max(r["month"] for r in rows)
    updated = any(r.get("collected") == today and r["month"] == latest for r in rows)
    by = {(r["item"], r["month"]): r for r in rows}
    lines = [f"※ 최신 확정월 {latest}" + (" — 오늘 갱신됨" if updated else "")]
    for item in ("활 민물장어(뱀장어)", "조제 민물장어(양념구이)", "민물장어 치어(실뱀장어)"):
        cur_row = by.get((item, latest))
        if not cur_row or not cur_row.get("usd_per_kg"):
            continue
        cur = float(cur_row["usd_per_kg"])
        jpy = float(cur_row["jpy_per_kg"])
        lines.append(f"● {item} (중국→일본)")
        lines.append(f"   {latest}: ${cur:.2f}/kg (¥{jpy:,.0f}), {int(cur_row['import_kg']):,}kg")
        for name, back in MONTH_PERIODS:
            prow = by.get((item, month_shift(latest, back)))
            if prow and prow.get("usd_per_kg"):
                lines.append(f"   {name}대비: "
                             f"{cmp_line(cur, float(prow['usd_per_kg']), '$', prow['month'])}"
                             f", {int(prow['import_kg']):,}kg")
            else:
                lines.append(f"   {name}대비: — (해당 월 실적/데이터 없음)")
        lines.append("")
    notes = ["일본 수입 갱신"] if updated else []
    return lines, updated, notes


def main() -> int:
    today = datetime.now(tp.KST).strftime("%Y-%m-%d")
    p_lines, p_changed, p_notes = product_section()
    w_lines, w_new, w_notes = wonmul_section(today)
    i_lines, i_new, i_notes = import_section(today)
    j_lines, j_new, j_notes = japan_section(today)

    notes = p_notes + w_notes + i_notes + j_notes
    if p_changed:
        status = "가격 변동 있음"
    elif w_new or i_new or j_new:
        status = "신규 데이터 있음"
    else:
        status = "변동 없음"
    detail = f" ({', '.join(notes)})" if notes else ""

    sep = "─" * 34
    body = "\n".join([
        f"[장어 가격 일일 리포트] {today} (KST)",
        f"■ 오늘의 요약: {status}{detail}",
        "",
        sep,
        "1) 거래처 상품 (푸드엔)",
        sep,
        *p_lines,
        sep,
        "2) 원물 도매 경매가 (전국 공영도매시장)",
        sep,
        *w_lines,
        sep,
        "3) 중국산 수입실적 — 한국 (월간, USD/kg CIF, 관세청 통관)",
        sep,
        *i_lines,
        sep,
        "4) 중국산 수입실적 — 일본 (월간, USD/kg 환산·CIF, 일본 재무성)",
        sep,
        *j_lines,
        f"전체 이력: {REPO_URL}",
        "※ '—'는 해당 기간의 데이터가 아직 쌓이지 않았다는 뜻입니다.",
        "※ 일본 단가는 ECB 월중 환율로 USD 환산 (원화·엔화 원본은 CSV에 보존).",
        "  (일별 수집 2026-08-10 시작 / 경매 2022-11~ / 수입 2025-02~)",
    ])
    OUT_PATH.write_text(body, encoding="utf-8")
    tp.log(body)
    tp.gh_output("subject", f"[장어 시세] {today} — {status}")
    tp.gh_output("changed", "true" if (p_changed or w_new or i_new or j_new) else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
