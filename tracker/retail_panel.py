#!/usr/bin/env python3
"""소매 패널 수집기 — 설계서 5장(수집·표집).

panel_offers.csv 의 오퍼를 하루 한 번 순회해 가격을 관측하고
offer_snapshots.csv 에 추가한다(append-only, 수정 금지).

사용법
  python3 tracker/retail_panel.py                 수집 (기본)
  python3 tracker/retail_panel.py --add URL...    오퍼 등록(제목 파싱해 자동 채움)
  python3 tracker/retail_panel.py --assign-terciles   가격 3분위 배정
  python3 tracker/retail_panel.py --status        패널 현황 점검

네트워크 호출은 track_price.py 의 검증된 fetch/robots 경로를 그대로 쓴다.
"""
from __future__ import annotations

import csv
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import retail_config as cfg          # noqa: E402
import retail_normalize as rn        # noqa: E402
import track_price as tp             # noqa: E402

ITEMS_CSV = ROOT / "panel_items.csv"
OFFERS_CSV = ROOT / "panel_offers.csv"
SNAPSHOTS_CSV = ROOT / "offer_snapshots.csv"
CHANGELOG_CSV = ROOT / "panel_changelog.csv"

OFFER_FIELDS = ["offer_id", "item_id", "portal", "seller_name", "url", "option_label",
                "external_id", "total_g", "unit_count", "shipping_fee_krw", "tercile",
                "match_status", "match_method", "added_on", "status", "note"]
SNAPSHOT_FIELDS = ["date", "offer_id", "item_id", "status", "title", "list_price",
                   "sale_price", "coupon_price", "shipping_fee", "total_price",
                   "total_g", "won_per_kg", "won_per_ea", "stock", "is_rocket",
                   "is_free_ship", "outlier_flag", "source", "url"]
CHANGELOG_FIELDS = ["date", "action", "offer_id", "item_id", "portal", "detail",
                    "frame_query", "frame_size"]

SOLD_OUT_MARKERS = ["품절", "일시품절", "재입고", "sold out", "판매중지", "판매종료",
                    "구매불가", "판매하지 않는"]


# ── CSV 입출력 ────────────────────────────────────────────────────────
def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def append_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    """append-only 기록. 기존 행은 절대 건드리지 않는다 — 설계 원칙 1."""
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerows(rows)


def today_kst() -> str:
    return datetime.now(tp.KST).strftime("%Y-%m-%d")


def to_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except ValueError:
        return None


# ── 품목 매칭 ─────────────────────────────────────────────────────────
def match_item(items: list[dict], form, origin, storage) -> str | None:
    """파싱된 속성을 품목에 배정한다. origin '*' 은 원산지 무관을 뜻한다."""
    if not (form and storage):
        return None
    for it in items:
        if it.get("active", "1") != "1":
            continue
        if it["form"] != form or it["storage"] != storage:
            continue
        want = (it.get("origin") or "*").strip()
        if want in ("*", "") or want == origin:
            return it["item_id"]
    return None


# ── 포털별 가격 단서 ──────────────────────────────────────────────────
def portal_candidates(html: str, portal: str) -> list[tuple]:
    """네이버·쿠팡 페이지에 임베드된 상태 JSON에서 가격 단서를 뽑는다.

    두 포털 모두 화면은 JS로 그리지만 초기 상태를 JSON으로 심어두므로
    거기서 읽는 편이 본문 텍스트 파싱보다 훨씬 안정적이다.
    track_price.extract_candidates 와 같은 (우선순위, 가격, 근거, 문맥, 위치)
    튜플을 돌려주어 choose_price 가 함께 판정하게 한다.
    """
    cands: list[tuple] = []

    def add(prio: int, value, source: str) -> None:
        price = tp.to_price(str(value))
        if price is not None:
            cands.append((prio, price, source, f"{portal}:{source}", 0))

    keys = {
        "naver": [("discountedSalePrice", 0), ("salePrice", 1), ("benefitPrice", 1),
                  ("mobileDiscountedSalePrice", 1), ("dispSalePrice", 1)],
        "coupang": [("couponPrice", 0), ("salePrice", 1), ("finalPrice", 1),
                    ("originPrice", 2)],
    }.get(portal, [])
    for key, prio in keys:
        for m in re.finditer(rf'"{key}"\s*:\s*"?(\d{{3,9}})"?', html):
            add(prio, m.group(1), f"json:{key}")
    return cands


def extract_shipping(html: str, portal: str) -> tuple[int | None, bool]:
    """(배송비, 무료배송여부). 확신이 없으면 None 을 돌려준다.

    0(무료)과 None(미확인)은 의미가 전혀 다르므로 절대 섞지 않는다 — 설계 6-1.
    미확인일 때는 panel_offers.csv 의 shipping_fee_krw 를 사용한다.
    """
    if re.search(r'"freeDelivery"\s*:\s*true|"isFreeShipping"\s*:\s*true', html):
        return 0, True
    if re.search(r"무료\s*배송|배송비\s*무료|로켓배송", html):
        return 0, True
    for key in ("deliveryFee", "deliveryCharge", "shippingFee"):
        m = re.search(rf'"{key}"\s*:\s*"?(\d{{1,7}})"?', html)
        if m:
            fee = int(m.group(1))
            return fee, fee == 0
    m = re.search(r"배송비[^0-9]{0,12}([0-9,]{3,9})\s*원", html)
    if m:
        fee = tp.to_price(m.group(1))
        if fee is not None and fee <= 50_000:
            return fee, fee == 0
    return None, False


def detect_sold_out(text: str, html: str) -> bool:
    if re.search(r'"soldOut"\s*:\s*true|"outOfStock"|OutOfStock', html):
        return True
    head = text[:4000].lower()
    return any(mark.lower() in head for mark in SOLD_OUT_MARKERS)


def detect_rocket(html: str) -> bool:
    return bool(re.search(r"로켓배송|로켓프레시|rocket", html, re.I))


# ── 오퍼 1건 관측 ─────────────────────────────────────────────────────
def observe(offer: dict, items: list[dict]) -> dict:
    """오퍼 하나를 관측해 스냅샷 한 행을 만든다. 실패 사유는 status 로 구분한다."""
    url = offer["url"].strip()
    portal = offer.get("portal") or rn.detect_portal(url) or ""
    row = {
        "date": today_kst(), "offer_id": offer["offer_id"],
        "item_id": offer.get("item_id", ""), "status": "parse_failed",
        "title": "", "url": url,
        "total_g": offer.get("total_g", ""), "outlier_flag": "", "source": "",
    }

    if not tp.robots_allowed(url):
        row["status"] = "robots_blocked"
        tp.log("  robots.txt 가 금지 — 건너뜀")
        return row

    try:
        raw, final_url, ctype = tp.fetch(url)
    except Exception as e:
        code = getattr(e, "code", None)
        row["status"] = "not_found" if code == 404 else "blocked"
        tp.log(f"  요청 실패({code or type(e).__name__}) → {row['status']}")
        return row

    html = tp.decode_html(raw, ctype)
    text = tp.strip_tags(html)
    row["title"] = tp.extract_title(html)

    if tp.detect_login_wall(text, final_url):
        row["status"] = "blocked"
        tp.log("  로그인 후에만 가격이 보임 → blocked")
        return row

    if detect_sold_out(text, html):
        row["status"] = "out_of_stock"
        row["stock"] = "out"
        tp.log("  품절")
        return row
    row["stock"] = "in"

    cands = portal_candidates(html, portal) + tp.extract_candidates(html, text)
    chosen = tp.choose_price(cands)
    if chosen is None:
        tp.log("  가격 후보 없음 → parse_failed")
        (tp.DEBUG_DIR / f"retail_{tp.slug(offer['offer_id'])}.html").write_text(
            html[:400_000], encoding="utf-8")
        return row

    sale_price, source = chosen
    row.update(status="ok", sale_price=sale_price, source=source)

    fee, free_ship = extract_shipping(html, portal)
    if fee is None:
        fee = to_int(offer.get("shipping_fee_krw"))   # 패널 등록 시 사람이 적어둔 값
        if fee is not None:
            row["source"] = f"{source}+fee:panel"
            free_ship = fee == 0
    row["shipping_fee"] = "" if fee is None else fee
    row["is_free_ship"] = "1" if free_ship else ""
    row["is_rocket"] = "1" if detect_rocket(html) else ""

    total = rn.total_price(sale_price, fee)
    total_g = to_int(offer.get("total_g")) or rn.parse_weight_g(row["title"])
    unit_count = to_int(offer.get("unit_count")) or rn.parse_unit_count(row["title"])
    row["total_price"] = "" if total is None else total
    row["total_g"] = "" if total_g is None else total_g
    row["won_per_kg"] = rn.won_per_kg(total, total_g) or ""
    row["won_per_ea"] = rn.won_per_ea(total, unit_count) or ""
    row["outlier_flag"] = rn.band_flags(total, total_g)

    if not row["item_id"]:
        parsed = rn.parse_title(row["title"])
        row["item_id"] = match_item(items, parsed["form"], parsed["origin"],
                                    parsed["storage"]) or ""

    fee_txt = "미확인" if fee is None else f"{fee:,}"
    kg_txt = f"{row['won_per_kg']:,.0f}원/kg" if row["won_per_kg"] else "환산불가"
    tp.log(f"  {sale_price:,}원 + 배송 {fee_txt} → {kg_txt}  [{row['source']}]")
    return row


# ── 명령: 수집 ────────────────────────────────────────────────────────
def run_collect() -> int:
    items = load_csv(ITEMS_CSV)
    offers = [o for o in load_csv(OFFERS_CSV) if o.get("status", "active") == "active"]
    if not offers:
        tp.log("panel_offers.csv 에 활성 오퍼가 없습니다 — 수집 건너뜀.")
        tp.log("등록: python3 tracker/retail_panel.py --add <상품URL> ...")
        tp.gh_output("retail_offers", "0")
        return 0

    tp.DEBUG_DIR.mkdir(exist_ok=True)
    rows: list[dict] = []
    for i, offer in enumerate(offers, 1):
        tp.log(f"\n[{i}/{len(offers)}] {offer['offer_id']} — {offer.get('seller_name','')}")
        rows.append(observe(offer, items))
        if i < len(offers):
            time.sleep(cfg.REQUEST_DELAY_SEC + random.random() * cfg.REQUEST_JITTER_SEC)

    append_csv(SNAPSHOTS_CSV, rows, SNAPSHOT_FIELDS)

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    fail_rate = counts.get("parse_failed", 0) / len(rows)
    tp.log("\n=== 수집 요약 ===")
    for status, n in sorted(counts.items()):
        tp.log(f"  {status}: {n}")
    if fail_rate > cfg.PARSE_FAIL_RATE:
        tp.log(f"⚠️ 파싱 실패율 {fail_rate:.0%} — 페이지 구조 변경 의심 (.debug/ 확인)")
    tp.gh_output("retail_offers", str(len(rows)))
    tp.gh_output("retail_parse_fail_rate", f"{fail_rate:.3f}")
    return 0


# ── 명령: 오퍼 등록 ───────────────────────────────────────────────────
def run_add(urls: list[str]) -> int:
    """URL 을 받아 제목을 파싱하고 오퍼 초안을 만든다.

    자동 배정된 item_id 는 match_status=auto 로 남기고, 판정이 애매하면
    pending 으로 둔다. 사람이 확인해 confirmed 로 바꾸면 이후 자동 로직은
    그 오퍼를 건드리지 않는다 — 설계 5장 L2/L3.
    """
    items = load_csv(ITEMS_CSV)
    offers = load_csv(OFFERS_CSV)
    known = {o["url"] for o in offers}
    seq = len(offers)
    added, log_rows = [], []

    for url in urls:
        url = url.strip()
        if not url or url in known:
            tp.log(f"이미 등록됨, 건너뜀: {url}")
            continue
        portal = rn.detect_portal(url)
        if portal is None:
            tp.log(f"⚠️ 네이버·쿠팡 URL 이 아닙니다: {url}")
            continue

        title, status = "", "active"
        try:
            raw, _, ctype = tp.fetch(url)
            title = tp.extract_title(tp.decode_html(raw, ctype))
        except Exception as e:
            tp.log(f"  제목을 못 읽었습니다({type(e).__name__}) — 빈 값으로 등록합니다")

        parsed = rn.parse_title(title) if title else {}
        item_id = match_item(items, parsed.get("form"), parsed.get("origin"),
                             parsed.get("storage")) if parsed else None
        ok = parsed.get("is_eel", False)
        confident = bool(item_id and ok and parsed.get("total_g")
                         and not parsed.get("form_inferred"))

        seq += 1
        offer_id = f"{portal[:2].upper()}-{seq:03d}"
        note = []
        if not title:
            note.append("제목을 못 읽음 — 사이트에서 직접 확인 필요")
        elif parsed and not ok:
            note.append(f"장어제품 아님?({parsed.get('reject_reason','')})")
        if parsed.get("form_inferred"):
            note.append("형태 추정")
        if parsed.get("storage_inferred"):
            note.append("보관 추정")
        if not parsed.get("total_g"):
            note.append("중량 미검출 — 직접 입력 필요")
        if not parsed.get("origin"):
            note.append("원산지 미검출")

        row = {
            "offer_id": offer_id, "item_id": item_id or "", "portal": portal,
            "seller_name": "", "url": url, "option_label": "", "external_id": "",
            "total_g": parsed.get("total_g") or "", "unit_count": parsed.get("unit_count") or "",
            "shipping_fee_krw": "", "tercile": "",
            "match_status": "auto" if confident else "pending",
            "match_method": "L1:title" if item_id else "",
            "added_on": today_kst(), "status": status,
            "note": " / ".join(note),
        }
        offers.append(row)
        added.append(row)
        known.add(url)
        log_rows.append({
            "date": today_kst(), "action": "add", "offer_id": offer_id,
            "item_id": item_id or "", "portal": portal,
            "detail": title[:80], "frame_query": "", "frame_size": "",
        })
        tp.log(f"  + {offer_id}  {row['match_status']:9s} {item_id or '(미배정)':14s} {title[:52]}")

    if added:
        save_csv(OFFERS_CSV, offers, OFFER_FIELDS)
        append_csv(CHANGELOG_CSV, log_rows, CHANGELOG_FIELDS)
        pend = sum(1 for r in added if r["match_status"] == "pending")
        tp.log(f"\n{len(added)}건 등록. 검토 필요 {pend}건 — panel_offers.csv 에서")
        tp.log("total_g / shipping_fee_krw / item_id 를 확인하고 match_status 를 confirmed 로 바꾸세요.")
    return 0


# ── 명령: 분위 배정 ───────────────────────────────────────────────────
def run_assign_terciles() -> int:
    """최근 관측된 원/kg 을 품목별로 3등분해 각 오퍼에 층을 붙인다 — 설계 5장 층화."""
    offers = load_csv(OFFERS_CSV)
    latest: dict[str, float] = {}
    for row in load_csv(SNAPSHOTS_CSV):
        if row.get("status") == "ok" and row.get("won_per_kg"):
            try:
                latest[row["offer_id"]] = float(row["won_per_kg"])
            except ValueError:
                continue
    if not latest:
        tp.log("관측 이력이 없어 분위를 배정할 수 없습니다. 먼저 수집을 1회 실행하세요.")
        return 0

    by_item: dict[str, list[tuple[str, float]]] = {}
    for o in offers:
        if o["offer_id"] in latest and o.get("item_id"):
            by_item.setdefault(o["item_id"], []).append((o["offer_id"], latest[o["offer_id"]]))

    assigned = {}
    for item_id, pairs in by_item.items():
        pairs.sort(key=lambda p: p[1])
        n = len(pairs)
        for idx, (offer_id, _) in enumerate(pairs):
            assigned[offer_id] = cfg.TERCILES[min(idx * 3 // n, 2)] if n >= 3 else "mid"
        tp.log(f"  {item_id}: {n}개 → " + ", ".join(
            f"{t}={sum(1 for o, _ in pairs if assigned[o] == t)}" for t in cfg.TERCILES))

    for o in offers:
        if o["offer_id"] in assigned:
            o["tercile"] = assigned[o["offer_id"]]
    save_csv(OFFERS_CSV, offers, OFFER_FIELDS)
    tp.log(f"\n{len(assigned)}개 오퍼에 분위를 배정했습니다.")
    return 0


# ── 명령: 현황 ────────────────────────────────────────────────────────
def run_status() -> int:
    items = load_csv(ITEMS_CSV)
    offers = load_csv(OFFERS_CSV)
    active = [o for o in offers if o.get("status", "active") == "active"]
    tp.log(f"품목 {len(items)}개 · 오퍼 {len(offers)}개(활성 {len(active)})\n")
    tp.log(f"{'품목':<16}{'오퍼':>5}{'lo':>5}{'mid':>5}{'hi':>5}  상태")
    short = []
    for it in items:
        mine = [o for o in active if o.get("item_id") == it["item_id"]]
        by_t = {t: sum(1 for o in mine if o.get("tercile") == t) for t in cfg.TERCILES}
        flag = "OK" if len(mine) >= cfg.MIN_OFFERS_PER_ITEM else \
               f"부족 (최소 {cfg.MIN_OFFERS_PER_ITEM})"
        if len(mine) < cfg.MIN_OFFERS_PER_ITEM:
            short.append(it["label"])
        tp.log(f"{it['label']:<16}{len(mine):>5}"
               + "".join(f"{by_t[t]:>5}" for t in cfg.TERCILES) + f"  {flag}")

    pending = [o for o in active if o.get("match_status") == "pending"]
    unassigned = [o for o in active if not o.get("item_id")]
    no_weight = [o for o in active if not o.get("total_g")]
    tp.log(f"\n검토 대기 {len(pending)} · 품목 미배정 {len(unassigned)} · 중량 미입력 {len(no_weight)}")
    if short:
        tp.log(f"⚠️ 오퍼가 부족한 품목: {', '.join(short)}")
    return 0


def main(argv: list[str]) -> int:
    if "--add" in argv:
        return run_add(argv[argv.index("--add") + 1:])
    if "--assign-terciles" in argv:
        return run_assign_terciles()
    if "--status" in argv:
        return run_status()
    return run_collect()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
