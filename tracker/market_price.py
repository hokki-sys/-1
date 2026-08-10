#!/usr/bin/env python3
"""네이버 쇼핑 검색 API 기반 시장시세 수집기.

tracker/market_keywords.csv 의 키워드마다 네이버 쇼핑 검색 API(shop.json)를
호출해 상위 판매글의 가격을 수집하고, 상품명에서 중량을 파싱해 kg당 가격으로
정규화한 뒤 일별 통계(최저/중앙값/평균)를 tracker/market_history.csv 에 기록한다.
당일 상위 판매글 스냅샷은 tracker/market_listings.csv 에 남긴다.

필요 환경변수: NAVER_CLIENT_ID, NAVER_CLIENT_SECRET
(https://developers.naver.com 애플리케이션 등록 후 발급, 검색 API 사용 설정)
키가 없으면 수집을 건너뛰고 정상 종료한다.
"""
from __future__ import annotations

import csv
import html as html_lib
import json
import os
import re
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import track_price as tp

ROOT = Path(__file__).resolve().parent
KEYWORDS_CSV = ROOT / "market_keywords.csv"
HISTORY_CSV = ROOT / "market_history.csv"
LISTINGS_CSV = ROOT / "market_listings.csv"
NOTE_PATH = ROOT / ".market_note.md"

HISTORY_FIELDS = ["date", "label", "listings", "with_weight", "min_per_kg",
                  "median_per_kg", "mean_per_kg", "min_price", "median_price"]
LISTING_FIELDS = ["date", "label", "title", "mall", "price", "weight_g",
                  "price_per_kg", "link"]

# 민물장어 상품이 아닌 것들(다른 어종, 소스/건강식품류)을 제목으로 거른다
EXCLUDE_TERMS = [
    "붕장어", "바다장어", "아나고", "곰장어", "꼼장어", "먹장어", "갯장어",
    "진액", "엑기스", "장어즙", "장어환", "분말", "소스", "타레", "다레",
    "장어뼈", "사료",
]
CHANGE_THRESHOLD = 0.05  # 중앙값 5% 이상 움직이면 알림
MAX_LISTINGS_SAVED = 15


def clean_title(raw: str) -> str:
    return html_lib.unescape(re.sub(r"</?b>", "", raw)).strip()


def naver_search(query: str, cid: str, csec: str) -> dict:
    url = ("https://openapi.naver.com/v1/search/shop.json?query="
           + quote(query) + "&display=100&sort=sim")
    req = urllib.request.Request(url, headers={
        "X-Naver-Client-Id": cid,
        "X-Naver-Client-Secret": csec,
        "User-Agent": tp.UA,
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_weight_g(title: str) -> int | None:
    """상품명에서 총 중량(g)을 추정한다. '1kg', '500g x 2' 등을 인식한다."""
    t = title.lower().replace(",", "").replace(" ", "")

    def with_mult(match: re.Match, value: float) -> float:
        after = t[match.end():match.end() + 4]
        mm = re.match(r"(?:x|×|\*)(\d{1,2})", after)
        return value * int(mm.group(1)) if mm else value

    kg_total = 0.0
    for m in re.finditer(r"(\d+(?:\.\d+)?)(?:kg|킬로)", t):
        kg_total += with_mult(m, float(m.group(1)) * 1000)
    g_total = 0.0
    # 'g' 뒤에 x/×는 허용(500gx2), 그 외 영문자는 다른 단어로 보고 제외
    for m in re.finditer(r"(\d+(?:\.\d+)?)(?:g|그램)(?![a-wyz])", t):
        g_total += with_mult(m, float(m.group(1)))

    # '1kg(500gx2)'처럼 같은 중량을 두 표기로 쓴 경우 이중계산을 피한다
    total = max(kg_total, g_total)
    return int(total) if 100 <= total <= 20000 else None


def keep_listing(title: str, price: int, require: str, exclude_extra: str) -> bool:
    if "장어" not in title:
        return False
    if not 5000 <= price <= 1_000_000:  # 소스·샘플/비정상가 제외
        return False
    for term in EXCLUDE_TERMS:
        if term in title:
            return False
    if re.search(r"양념장(?!어)", title):  # '양념장(소스)'은 제외, '양념장어'는 유지
        return False
    if require and not re.search(require, title, re.I):
        return False
    if exclude_extra and re.search(exclude_extra, title, re.I):
        return False
    return True


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


def previous_median(rows: list[dict], label: str, today: str) -> tuple[int, str] | None:
    for row in reversed(rows):
        if (row.get("label") == label and row.get("date") != today
                and row.get("median_per_kg")):
            try:
                return int(row["median_per_kg"]), row["date"]
            except ValueError:
                continue
    return None


def main() -> int:
    cid = os.environ.get("NAVER_CLIENT_ID", "").strip()
    csec = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        tp.log("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 미등록 — 시장시세 수집을 건너뜁니다.")
        tp.log("등록 방법: tracker/README.md 의 '네이버 API 키 등록' 참고")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("\n> ⚠️ 네이버 API 키 미등록 — 시장시세 수집 건너뜀\n")
        tp.gh_output("changed", "false")
        return 0

    today = datetime.now(tp.KST).strftime("%Y-%m-%d")
    tp.DEBUG_DIR.mkdir(exist_ok=True)

    with KEYWORDS_CSV.open(encoding="utf-8-sig", newline="") as f:
        keywords = [row for row in csv.DictReader(f) if row.get("query", "").strip()]
    if not keywords:
        tp.log("market_keywords.csv 에 키워드가 없습니다.")
        return 1

    history = load_csv(HISTORY_CSV)
    listings_all = load_csv(LISTINGS_CSV)
    today_hist: list[dict] = []
    today_listings: list[dict] = []
    changes: list[tuple] = []
    failures = 0

    for kw in keywords:
        label = kw["label"].strip()
        query = kw["query"].strip()
        require = (kw.get("require") or "").strip()
        exclude_extra = (kw.get("exclude_extra") or "").strip()
        tp.log(f"\n=== 시장시세: {label} (검색어: {query}) ===")

        try:
            data = naver_search(query, cid, csec)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            tp.log(f"API 오류 {e.code}: {body}")
            failures += 1
            continue
        except Exception as e:
            tp.log(f"API 요청 실패: {e}")
            failures += 1
            continue

        (tp.DEBUG_DIR / f"market_{tp.slug(label)}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

        kept = []
        for item in data.get("items", []):
            title = clean_title(item.get("title", ""))
            try:
                price = int(item.get("lprice") or 0)
            except ValueError:
                continue
            if not keep_listing(title, price, require, exclude_extra):
                continue
            weight = parse_weight_g(title)
            kept.append({
                "title": title,
                "mall": item.get("mallName", ""),
                "price": price,
                "weight_g": weight,
                "price_per_kg": round(price / weight * 1000) if weight else None,
                "link": item.get("link", ""),
            })

        per_kg = [k["price_per_kg"] for k in kept if k["price_per_kg"]]
        prices = [k["price"] for k in kept]
        tp.log(f"판매글 {len(data.get('items', []))}건 중 유효 {len(kept)}건, "
               f"중량 인식 {len(per_kg)}건")
        if not kept:
            tp.log("유효한 판매글이 없습니다 — 필터가 과한지 확인 필요")
            failures += 1
            continue

        row = {
            "date": today, "label": label,
            "listings": str(len(kept)), "with_weight": str(len(per_kg)),
            "min_per_kg": str(min(per_kg)) if per_kg else "",
            "median_per_kg": str(round(statistics.median(per_kg))) if per_kg else "",
            "mean_per_kg": str(round(statistics.mean(per_kg))) if per_kg else "",
            "min_price": str(min(prices)),
            "median_price": str(round(statistics.median(prices))),
        }
        today_hist.append(row)
        if per_kg:
            tp.log(f"kg당 가격: 최저 {min(per_kg):,} / 중앙 "
                   f"{round(statistics.median(per_kg)):,} / 평균 {round(statistics.mean(per_kg)):,}원")

        for k in kept[:MAX_LISTINGS_SAVED]:
            today_listings.append({
                "date": today, "label": label, "title": k["title"],
                "mall": k["mall"], "price": str(k["price"]),
                "weight_g": str(k["weight_g"] or ""),
                "price_per_kg": str(k["price_per_kg"] or ""),
                "link": k["link"],
            })

        prev = previous_median(history, label, today)
        if prev and row["median_per_kg"]:
            new_med = int(row["median_per_kg"])
            if prev[0] and abs(new_med - prev[0]) / prev[0] >= CHANGE_THRESHOLD:
                changes.append((label, prev[0], new_med, prev[1]))
                tp.log(f"시세 변동: 중앙값 {prev[0]:,} → {new_med:,}원/kg")

    history = [r for r in history if r.get("date") != today] + today_hist
    history.sort(key=lambda r: (r.get("date", ""), r.get("label", "")))
    save_csv(HISTORY_CSV, history, HISTORY_FIELDS)

    listings_all = [r for r in listings_all if r.get("date") != today] + today_listings
    listings_all.sort(key=lambda r: (r.get("date", ""), r.get("label", "")))
    save_csv(LISTINGS_CSV, listings_all, LISTING_FIELDS)
    tp.log(f"\n시장시세 기록 완료: 집계 {len(today_hist)}건, 판매글 {len(today_listings)}건")

    if changes:
        lines = ["네이버쇼핑 시장시세(중앙값, kg당) 변동이 감지되었습니다.", ""]
        for label, old, new, old_date in changes:
            pct = (new - old) / old * 100
            lines.append(f"- **{label}**: {old:,}원/kg({old_date}) → **{new:,}원/kg** ({pct:+.1f}%)")
        lines += ["", "상세: `tracker/market_history.csv`, `tracker/market_listings.csv`"]
        NOTE_PATH.write_text("\n".join(lines), encoding="utf-8")
    tp.gh_output("changed", "true" if changes else "false")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and today_hist:
        lines = ["", "## 시장시세 (네이버쇼핑)", "",
                 "| 품목 | 유효 판매글 | 최저/kg | 중앙/kg |", "|---|---|---|---|"]
        for r in today_hist:
            mn = f"{int(r['min_per_kg']):,}원" if r["min_per_kg"] else "-"
            md = f"{int(r['median_per_kg']):,}원" if r["median_per_kg"] else "-"
            lines.append(f"| {r['label']} | {r['listings']} | {mn} | {md} |")
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    return 1 if failures == len(keywords) else 0


if __name__ == "__main__":
    sys.exit(main())
