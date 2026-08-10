#!/usr/bin/env python3
"""쇼핑몰 상품 가격 추적기.

tracker/products.csv 에 등록된 상품 페이지를 내려받아 가격을 추출하고
tracker/price_history.csv 에 날짜별로 기록한다. 표준 라이브러리만 사용한다.

로그인해야 가격이 보이는 몰은 환경변수 FOODEN_COOKIE 에 세션 쿠키를 넣으면
요청에 함께 전송된다 (GitHub Actions 에서는 시크릿으로 주입).
"""
from __future__ import annotations

import csv
import html as html_lib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

ROOT = Path(__file__).resolve().parent
PRODUCTS_CSV = ROOT / "products.csv"
HISTORY_CSV = ROOT / "price_history.csv"
DEBUG_DIR = ROOT / ".debug"
CHANGE_NOTE = ROOT / ".change_note.md"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
KST = timezone(timedelta(hours=9))
HISTORY_FIELDS = ["date", "label", "title", "price", "status", "source", "url"]

NUM = r"[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{3,9}"

LOGIN_MARKERS = [
    "로그인 후", "로그인후", "로그인 시", "로그인시", "회원전용", "회원 전용",
    "회원공개", "회원 공개", "가격문의", "가격 문의", "회원가입 후",
]


def log(msg: str) -> None:
    print(msg, flush=True)


def slug(s: str) -> str:
    return re.sub(r"[^\w가-힣-]+", "_", s)[:40] or "page"


def to_price(text: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", text.split(".")[0])
    if not digits:
        return None
    value = int(digits)
    return value if 100 <= value <= 100_000_000 else None


def fetch(url: str, cookie: str = "") -> tuple[bytes, str, str]:
    """(본문 바이트, 최종 URL, Content-Type) 을 반환한다."""
    parts = urlsplit(url)
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
        "Referer": f"{parts.scheme}://{parts.netloc}/",
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read(), resp.geturl(), resp.headers.get("Content-Type", "")


def robots_allowed(url: str) -> bool:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        raw, _, ctype = fetch(robots_url)
    except Exception:
        return True
    rp = RobotFileParser()
    rp.parse(decode_html(raw, ctype).splitlines())
    return rp.can_fetch("*", url)


def decode_html(raw: bytes, content_type: str) -> str:
    candidates: list[str] = []
    m = re.search(r"charset=([\w-]+)", content_type, re.I)
    if m:
        candidates.append(m.group(1))
    head = raw[:4096].decode("ascii", "ignore")
    m = re.search(r"charset=[\"']?([\w-]+)", head, re.I)
    if m:
        candidates.append(m.group(1))
    candidates += ["utf-8", "cp949", "euc-kr"]
    for cs in candidates:
        try:
            return raw.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def strip_tags(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text)


def extract_title(html: str) -> str:
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I
    )
    if not m:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if not m:
        return ""
    return html_lib.unescape(re.sub(r"\s+", " ", m.group(1))).strip()[:120]


def extract_candidates(html: str, text: str) -> list[tuple[int, int, str, str]]:
    """(우선순위, 가격, 근거, 문맥) 후보 목록. 우선순위 숫자가 낮을수록 신뢰도가 높다."""
    cands: list[tuple[int, int, str, str]] = []

    def add(prio: int, value: str, source: str, ctx: str) -> None:
        price = to_price(value)
        if price is not None:
            cands.append((prio, price, source, re.sub(r"\s+", " ", ctx)[:160]))

    meta_names = r"(?:og:price:amount|product:price:amount|product:sale_price:amount)"
    for m in re.finditer(
        rf'<meta[^>]+(?:property|name)=["\']{meta_names}["\'][^>]*content=["\']([0-9,\.]+)["\']',
        html, re.I,
    ):
        add(1, m.group(1), "meta", m.group(0))
    for m in re.finditer(
        rf'<meta[^>]+content=["\']([0-9,\.]+)["\'][^>]*(?:property|name)=["\']{meta_names}["\']',
        html, re.I,
    ):
        add(1, m.group(1), "meta", m.group(0))

    for m in re.finditer(
        r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I
    ):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key in ("price", "lowPrice"):
                    if key in node and isinstance(node[key], (str, int, float)):
                        add(1, str(node[key]), "json-ld", key)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    for m in re.finditer(
        r'(?:name|id)=["\'](?:price|sell_price|sellPrice|goods_price|goodsPrice|'
        r'product_price|sale_price|dc_price)["\'][^>]*value=["\']([0-9,\.]+)["\']',
        html, re.I,
    ):
        add(2, m.group(1), "hidden-input", m.group(0))
    for m in re.finditer(
        r'["\']?(?:sell_?price|sale_?price|goods_?price|dc_?price)["\']?\s*[:=]\s*["\']?(' + NUM + r")",
        html, re.I,
    ):
        add(3, m.group(1), "js-var", m.group(0))

    for label, prio in (
        ("판매가격", 2), ("판매가", 2), ("할인가", 3), ("회원가", 3), ("공급가", 3)
    ):
        for m in re.finditer(re.escape(label) + r"[^0-9원]{0,20}(" + NUM + r")\s*원?", text):
            add(prio, m.group(1), f"label:{label}", m.group(0))

    for m in re.finditer(r"(" + NUM + r")\s*원", text):
        add(5, m.group(1), "won-suffix", m.group(0))

    return cands


def choose_price(cands: list[tuple[int, int, str, str]]) -> tuple[int, str] | None:
    if not cands:
        return None
    best_prio = min(c[0] for c in cands)
    best = [c for c in cands if c[0] == best_prio]
    counts: dict[int, int] = {}
    for _, price, _, _ in best:
        counts[price] = counts.get(price, 0) + 1
    price = max(counts, key=lambda p: counts[p])
    source = next(c[2] for c in best if c[1] == price)
    return price, source


def detect_login_wall(text: str, final_url: str) -> bool:
    if re.search(r"login|member", urlsplit(final_url).path, re.I):
        return True
    return any(marker in text for marker in LOGIN_MARKERS)


def dump_debug(text: str, cands: list[tuple[int, int, str, str]]) -> None:
    log("--- 가격 후보 목록 ---")
    if not cands:
        log("  (없음)")
    for prio, price, source, ctx in cands[:40]:
        log(f"  [p{prio}] {price:,} via {source} :: {ctx}")
    positions = [m.start() for m in re.finditer("원", text)][:12]
    if positions:
        log("--- '원' 주변 문맥 ---")
        for pos in positions:
            log("  …" + text[max(0, pos - 90):pos + 30] + "…")
    log("--- 본문 앞부분 ---")
    log(text[:600])


def load_history() -> list[dict]:
    if not HISTORY_CSV.exists():
        return []
    with HISTORY_CSV.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_history(rows: list[dict]) -> None:
    with HISTORY_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def previous_price(rows: list[dict], label: str, today: str) -> tuple[int, str] | None:
    for row in reversed(rows):
        if row.get("label") == label and row.get("date") != today and row.get("price"):
            try:
                return int(row["price"]), row["date"]
            except ValueError:
                continue
    return None


def gh_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def write_step_summary(rows: list[dict], changes: list[tuple]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## 가격 추적 결과", "", "| 상품 | 가격 | 상태 |", "|---|---|---|"]
    for r in rows:
        price = f"{int(r['price']):,}원" if r.get("price") else "-"
        lines.append(f"| {r['label']} | {price} | {r['status']} |")
    if changes:
        lines += ["", "### ⚠️ 가격 변동 감지"]
        for label, old, new, old_date in changes:
            lines.append(f"- {label}: {old:,}원({old_date}) → **{new:,}원**")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    cookie = os.environ.get("FOODEN_COOKIE", "").strip()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    DEBUG_DIR.mkdir(exist_ok=True)

    with PRODUCTS_CSV.open(encoding="utf-8-sig", newline="") as f:
        products = [row for row in csv.DictReader(f) if row.get("url", "").strip()]
    if not products:
        log("products.csv 에 추적할 상품이 없습니다.")
        return 1

    history = load_history()
    today_rows: list[dict] = []
    changes: list[tuple] = []
    failures = 0

    for prod in products:
        url = prod["url"].strip()
        label = (prod.get("label") or "").strip() or url[:40]
        row = {"date": today, "label": label, "title": "", "price": "",
               "status": "", "source": "", "url": url}
        log(f"\n=== {label} ===")
        log(url)

        if not robots_allowed(url):
            row["status"] = "robots_blocked"
            log("robots.txt 가 이 경로의 수집을 금지하고 있어 건너뜁니다.")
            today_rows.append(row)
            failures += 1
            continue

        try:
            raw, final_url, ctype = fetch(url, cookie)
        except urllib.error.HTTPError as e:
            body = e.read() or b""
            (DEBUG_DIR / f"{slug(label)}.html").write_bytes(body)
            row["status"] = f"http_{e.code}"
            log(f"HTTP 오류 {e.code}")
            today_rows.append(row)
            failures += 1
            continue
        except Exception as e:
            row["status"] = "fetch_error"
            log(f"요청 실패: {e}")
            today_rows.append(row)
            failures += 1
            continue

        (DEBUG_DIR / f"{slug(label)}.html").write_bytes(raw)
        html = decode_html(raw, ctype)
        text = strip_tags(html)
        row["title"] = extract_title(html)
        log(f"페이지 제목: {row['title'] or '(없음)'}")
        if final_url != url:
            log(f"리다이렉트됨: {final_url}")

        cands = extract_candidates(html, text)
        picked = choose_price(cands)
        if picked:
            price, source = picked
            row["price"], row["status"], row["source"] = str(price), "ok", source
            log(f"가격: {price:,}원 (근거: {source})")
            if os.environ.get("TRACKER_VERBOSE"):
                dump_debug(text, cands)
            prev = previous_price(history, label, today)
            if prev and prev[0] != price:
                changes.append((label, prev[0], price, prev[1]))
                log(f"가격 변동: {prev[0]:,}원({prev[1]}) → {price:,}원")
        else:
            row["status"] = "login_required" if detect_login_wall(text, final_url) else "no_price"
            failures += 1
            log(f"가격을 찾지 못했습니다 (status={row['status']}).")
            dump_debug(text, cands)

        today_rows.append(row)

    history = [r for r in history if r.get("date") != today] + today_rows
    history.sort(key=lambda r: (r.get("date", ""), r.get("label", "")))
    save_history(history)
    log(f"\nprice_history.csv 갱신 완료 (오늘 {len(today_rows)}건 기록).")

    if changes:
        lines = ["가격 변동이 감지되었습니다.", ""]
        for label, old, new, old_date in changes:
            pct = (new - old) / old * 100
            lines.append(f"- **{label}**: {old:,}원({old_date}) → **{new:,}원** ({pct:+.1f}%)")
        lines += ["", "전체 이력: `tracker/price_history.csv`"]
        CHANGE_NOTE.write_text("\n".join(lines), encoding="utf-8")

    gh_output("changed", "true" if changes else "false")
    write_step_summary(today_rows, changes)
    return 1 if failures == len(products) else 0


if __name__ == "__main__":
    sys.exit(main())
