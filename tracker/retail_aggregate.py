#!/usr/bin/env python3
"""소매 집계 — 설계서 10장(지표).

offer_snapshots.csv(사실)에서 item_daily.csv 와 retail_cross.csv(해석)를
매번 **전량 재생성**한다. 파생물이므로 매칭이나 이상치 규칙을 고쳐도
과거까지 한 번에 다시 계산된다 — 설계 원칙 1.

실행: python3 tracker/retail_aggregate.py
"""
from __future__ import annotations

import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import retail_config as cfg     # noqa: E402
import track_price as tp        # noqa: E402
from retail_fx import latest_rate                              # noqa: E402
from retail_panel import (SNAPSHOTS_CSV, OFFERS_CSV, ITEMS_CSV,   # noqa: E402
                          load_csv, save_csv, today_kst)

DAILY_CSV = ROOT / "item_daily.csv"
CROSS_CSV = ROOT / "retail_cross.csv"
NOTE_PATH = ROOT / ".retail_note.md"
WONMUL_CSV = ROOT / "wonmul_history.csv"
IMPORT_CSV = ROOT / "import_history.csv"

DAILY_FIELDS = ["date", "item_id", "n_offers", "n_sellers", "n_in_stock",
                "n_lo", "n_mid", "n_hi", "min_won_per_kg", "p25_won_per_kg",
                "median_won_per_kg", "p75_won_per_kg", "median_ma7",
                "spread_ratio", "d1_pct", "d7_pct", "d30_pct", "is_30d_low"]
CROSS_FIELDS = ["date", "metric", "item_id", "value", "basis", "detail"]


# ── 통계 도우미 ───────────────────────────────────────────────────────
def percentile(values: list[float], q: float) -> float | None:
    """선형보간 백분위. n=1 이어도 안전하게 동작한다."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def median(values: list[float]) -> float | None:
    return percentile(values, 0.5)


def d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def pct(cur: float | None, prev: float | None) -> str:
    if cur is None or prev in (None, 0):
        return ""
    return f"{(cur - prev) / prev:.4f}"


def num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── 품목 일별 집계 ────────────────────────────────────────────────────
def build_daily(snapshots: list[dict], offers: list[dict]) -> list[dict]:
    seller_of = {o["offer_id"]: (o.get("seller_name") or o["offer_id"]) for o in offers}
    tercile_of = {o["offer_id"]: o.get("tercile", "") for o in offers}
    confirmed = {o["offer_id"] for o in offers if o.get("match_status") != "pending"}

    # (item, date) 로 묶는다. pending 오퍼는 집계에서 빼되 스냅샷은 그대로 둔다.
    grouped: dict[str, dict[str, list[dict]]] = {}
    for s in snapshots:
        item_id = s.get("item_id", "")
        if not item_id or s["offer_id"] not in confirmed:
            continue
        grouped.setdefault(item_id, {}).setdefault(s["date"], []).append(s)

    rows: list[dict] = []
    for item_id, by_date in grouped.items():
        accepted: list[tuple[date, float]] = []   # 이상치 밴드의 기준이 되는 과거 값
        history: list[tuple[date, float]] = []    # 일별 중앙값 이력

        for day in sorted(by_date):
            today = d(day)
            snaps = by_date[day]

            # 직전 30일 중앙값 대비 상대 이상치 판정 (인과적 — 미래 값을 쓰지 않는다)
            window = [v for dt, v in accepted
                      if 0 <= (today - dt).days <= cfg.OUTLIER_WINDOW_DAYS]
            base = median(window)

            values, kept = [], []
            for s in snaps:
                if s.get("status") != "ok" or s.get("outlier_flag"):
                    continue
                v = num(s.get("won_per_kg"))
                if v is None:
                    continue
                if base and not (base * cfg.OUTLIER_LO_RATIO <= v <= base * cfg.OUTLIER_HI_RATIO):
                    continue
                values.append(v)
                kept.append(s)

            for s in kept:
                accepted.append((today, num(s["won_per_kg"])))

            n_sellers = len({seller_of.get(s["offer_id"], "") for s in kept})
            counts = {t: sum(1 for s in kept if tercile_of.get(s["offer_id"]) == t)
                      for t in cfg.TERCILES}
            med = median(values) if len(values) >= cfg.MIN_OFFERS_FOR_STAT else None
            p25 = percentile(values, 0.25) if med is not None else None
            p75 = percentile(values, 0.75) if med is not None else None
            if med is not None:
                history.append((today, med))

            ma_vals = [v for dt, v in history if 0 <= (today - dt).days < cfg.MA_WINDOW_DAYS]
            ma7 = sum(ma_vals) / len(ma_vals) if ma_vals else None

            def at(days_back: int) -> float | None:
                target = today - timedelta(days=days_back)
                past = [v for dt, v in history if dt <= target]
                return past[-1] if past else None

            prior = [v for dt, v in history[:-1] if 0 <= (today - dt).days <= 30]
            rows.append({
                "date": day, "item_id": item_id,
                "n_offers": len(kept), "n_sellers": n_sellers,
                "n_in_stock": sum(1 for s in snaps if s.get("stock") == "in"),
                "n_lo": counts["lo"], "n_mid": counts["mid"], "n_hi": counts["hi"],
                "min_won_per_kg": round(min(values), 1) if values else "",
                "p25_won_per_kg": round(p25, 1) if p25 is not None else "",
                "median_won_per_kg": round(med, 1) if med is not None else "",
                "p75_won_per_kg": round(p75, 1) if p75 is not None else "",
                "median_ma7": round(ma7, 1) if ma7 is not None else "",
                "spread_ratio": round(p75 / p25, 3) if p25 else "",
                "d1_pct": pct(med, at(1)), "d7_pct": pct(med, at(7)),
                "d30_pct": pct(med, at(30)),
                "is_30d_low": "1" if (med is not None and prior and med < min(prior)) else "",
            })
    rows.sort(key=lambda r: (r["date"], r["item_id"]))
    return rows


# ── 교차 분석 ─────────────────────────────────────────────────────────
def wholesale_median(as_of: date) -> tuple[float | None, int]:
    """활 뱀장어 경매가 이동창 중앙값(원/kg). 경매가 드물어 창을 넓게 잡는다."""
    vals = []
    for r in load_csv(WONMUL_CSV):
        if cfg.WHOLESALE_SPECIES not in (r.get("species") or ""):
            continue
        v = num(r.get("price_per_kg"))
        try:
            dt = d(r["auction_date"])
        except (KeyError, ValueError):
            continue
        if v and 0 <= (as_of - dt).days <= cfg.WHOLESALE_WINDOW_DAYS:
            vals.append(v)
    return median(vals), len(vals)


def import_usd_per_kg(item_id: str, as_of: date) -> tuple[float | None, str]:
    """품목에 대응하는 HS 코드의 최근 월 수입단가(USD/kg)."""
    codes = cfg.IMPORT_HS_BY_ITEM.get(item_id)
    if not codes:
        return None, ""
    best_month, usd, kg = "", 0.0, 0.0
    rows = [r for r in load_csv(IMPORT_CSV)
            if r.get("hs") in codes and r.get("country") == cfg.IMPORT_COUNTRY
            and r.get("month", "") <= as_of.strftime("%Y-%m")]
    for month in sorted({r["month"] for r in rows}, reverse=True):
        same = [r for r in rows if r["month"] == month]
        u = sum(num(r.get("import_usd")) or 0 for r in same)
        k = sum(num(r.get("import_kg")) or 0 for r in same)
        if k > 0:
            best_month, usd, kg = month, u, k
            break
    if not kg:
        return None, ""
    return round(usd / kg, 3), best_month


def build_cross(daily: list[dict]) -> list[dict]:
    fx = cfg.USDKRW_OVERRIDE
    fx_when = "고정값"
    if fx is None:
        fx, fx_when = latest_rate()
    if fx is None:
        tp.log("⚠️ USD/KRW 환율이 없어 ★2 배수를 건너뜁니다 "
               "(python3 tracker/retail_fx.py 를 먼저 실행하거나 "
               "retail_config.USDKRW_OVERRIDE 를 설정하세요)")

    latest: dict[str, dict] = {}
    for r in daily:
        latest.setdefault(r["date"], {})[r["item_id"]] = r
    rows: list[dict] = []

    for day in sorted(latest):
        as_of = d(day)
        by_item = latest[day]

        # ★1 원산지 가격차 — 국내산 필렛 ÷ 중국산 필렛
        kr = num(by_item.get("FIL-KR-FZ", {}).get("median_won_per_kg"))
        cn = num(by_item.get("FIL-CN-FZ", {}).get("median_won_per_kg"))
        if kr and cn:
            rows.append({"date": day, "metric": "origin_gap", "item_id": "필렛",
                         "value": round(kr / cn, 3), "basis": "국내산÷중국산 중앙값",
                         "detail": f"국내산 {kr:,.0f} / 중국산 {cn:,.0f} 원/kg"})

        whole, n_auction = wholesale_median(as_of)
        for item_id, row in sorted(by_item.items()):
            retail = num(row.get("median_won_per_kg"))
            if not retail:
                continue

            # ★2 수입단가 대비 소매 배수
            usd, month = import_usd_per_kg(item_id, as_of)
            if usd:
                # 수입단가 자체도 시계열로 남긴다 — 환율이 없어도 추세는 읽힌다
                rows.append({"date": day, "metric": "import_usd_per_kg",
                             "item_id": item_id, "value": usd,
                             "basis": "관세청 통관 CIF", "detail": f"{month} 기준"})
                lc = cfg.LANDED_COST
                if fx is None:
                    pass       # 환율이 없으면 배수를 내지 않는다(단위가 원/USD 가 되어 무의미)
                elif lc["enabled"] and lc.get("tariff_rate") is not None:
                    krw = usd * fx
                    krw *= 1 + (lc.get("tariff_rate") or 0)
                    krw *= 1 + (lc.get("vat_rate") or 0)
                    krw += (lc.get("clearance_krw_per_kg") or 0)
                    krw += (lc.get("inland_krw_per_kg") or 0)
                    rows.append({"date": day, "metric": "import_multiple",
                                 "item_id": item_id, "value": round(retail / krw, 3),
                                 "basis": "국내 추정원가 기준",
                                 "detail": f"원가 {krw:,.0f}원/kg ({month}, "
                                           f"환율 {fx:,.0f})"})
                else:
                    # 관세·통관 가정이 미확정이면 CIF 원화 환산 기준으로만 낸다 — 설계 Q6
                    krw = usd * fx
                    rows.append({"date": day, "metric": "import_multiple_cif",
                                 "item_id": item_id, "value": round(retail / krw, 3),
                                 "basis": "CIF 원화환산 기준(관세·통관 미반영)",
                                 "detail": f"CIF {krw:,.0f}원/kg "
                                           f"({month}, 환율 {fx:,.0f} {fx_when})"})

            # ★3 소매 ÷ 도매(원물)
            if whole:
                ratio = retail / whole
                note = f"원물 {whole:,.0f}원/kg, 경매 {n_auction}건"
                if cfg.FILLET_YIELD:
                    ratio *= cfg.FILLET_YIELD
                    note += f", 수율 {cfg.FILLET_YIELD:.0%} 보정"
                else:
                    note += ", 수율 미보정"
                rows.append({"date": day, "metric": "retail_wholesale_ratio",
                             "item_id": item_id, "value": round(ratio, 3),
                             "basis": "활 뱀장어 경매가 대비", "detail": note})
    return rows


# ── 경보 판정 (설계 11장) ─────────────────────────────────────────────
def build_alerts(daily: list[dict], cross: list[dict], offers: list[dict],
                 items: list[dict]) -> list[str]:
    alerts: list[str] = []
    if not daily:
        return alerts
    last_day = daily[-1]["date"]
    label_of = {i["item_id"]: i["label"] for i in items}
    today_rows = [r for r in daily if r["date"] == last_day]

    for r in today_rows:
        name = label_of.get(r["item_id"], r["item_id"])
        ma = num(r.get("median_ma7"))
        prev = [x for x in daily if x["item_id"] == r["item_id"]
                and (d(last_day) - d(x["date"])).days == 7]
        prev_ma = num(prev[0].get("median_ma7")) if prev else None
        if ma and prev_ma:
            change = (ma - prev_ma) / prev_ma
            if abs(change) >= cfg.TREND_SHIFT_PCT:
                arrow = "▲" if change > 0 else "▼"
                alerts.append(f"{arrow} **{name}** 7일 이동평균 전주 대비 "
                              f"{change:+.1%} ({prev_ma:,.0f} → {ma:,.0f} 원/kg)")
        for t in cfg.TERCILES:
            if int(r.get(f"n_{t}") or 0) < cfg.MIN_PER_TERCILE:
                alerts.append(f"⚠️ **{name}** {t} 분위 관측 "
                              f"{r.get(f'n_{t}')}건 — 표본 붕괴 위험")

    gaps = [c for c in cross if c["metric"] == "origin_gap"]
    if len(gaps) >= 2:
        cur, base = gaps[-1], None
        for c in reversed(gaps[:-1]):
            if (d(cur["date"]) - d(c["date"])).days >= 28:
                base = c
                break
        if base:
            change = (float(cur["value"]) - float(base["value"])) / float(base["value"])
            if abs(change) >= cfg.ORIGIN_GAP_SHIFT_PCT:
                alerts.append(f"◆ 원산지 가격차 전월 대비 {change:+.1%} "
                              f"({base['value']} → {cur['value']}배)")

    pending = sum(1 for o in offers if o.get("match_status") == "pending"
                  and o.get("status", "active") == "active")
    if pending >= cfg.PENDING_ALERT:
        alerts.append(f"· 검토 대기 오퍼 {pending}건 — panel_offers.csv 확인 필요")
    return alerts


def main() -> int:
    snapshots = load_csv(SNAPSHOTS_CSV)
    offers = load_csv(OFFERS_CSV)
    items = load_csv(ITEMS_CSV)
    if not snapshots:
        tp.log("offer_snapshots.csv 가 비어 있습니다 — 집계 건너뜀.")
        tp.gh_output("retail_changed", "false")
        return 0

    daily = build_daily(snapshots, offers)
    cross = build_cross(daily)
    save_csv(DAILY_CSV, daily, DAILY_FIELDS)
    save_csv(CROSS_CSV, cross, CROSS_FIELDS)
    tp.log(f"집계 완료: item_daily {len(daily)}행 · retail_cross {len(cross)}행")

    alerts = build_alerts(daily, cross, offers, items)
    if alerts:
        NOTE_PATH.write_text("## 소매 시세 알림\n\n" + "\n".join(f"- {a}" for a in alerts)
                             + "\n", encoding="utf-8")
        tp.log("\n".join(alerts))
    tp.gh_output("retail_changed", "true" if alerts else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
