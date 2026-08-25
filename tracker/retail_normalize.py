#!/usr/bin/env python3
"""상품명 정규화 — 설계서 5장(매칭 L1)·6장(가격 정규화).

판매글 제목에서 {형태, 원산지, 보관, 총중량, 미수}를 뽑아 품목 키를 만들고,
배송비 포함 가격을 원/kg 으로 환산한다. 순수 함수만 두어 네트워크 없이
검증할 수 있게 한다. 자체 검증은 `python3 tracker/test_retail.py`.

중량 파싱은 (구) market_price.py 의 parse_weight_g 를 이식하면서
'2kg(1kg x 2)' 류를 4kg 으로 이중계산하던 문제를 괄호 인식으로 고쳤다.
"""
from __future__ import annotations

import html as html_lib
import re

# ── 민물장어가 아닌 것 (구 market_price.py EXCLUDE_TERMS 이식) ──────────
EXCLUDE_TERMS = [
    "붕장어", "바다장어", "아나고", "곰장어", "꼼장어", "먹장어", "갯장어",
    "진액", "엑기스", "장어즙", "장어환", "분말", "소스", "타레", "다레",
    "장어뼈", "사료", "낚시", "미끼",
]

# ── 형태 (우선순위 순: 위에서 먼저 맞는 것을 채택) ─────────────────────
FORM_RULES = [
    ("장어탕", r"장어탕|장어국|탕용|추어탕"),
    ("양념구이", r"양념|데리야끼|데리야키|데리야|가바야끼|카바야키|간장구이|"
                 r"소금구이|초벌구이|초벌|양념구이|불맛"),
    ("필렛",   r"필렛|필레|필렡|fillet|반마리|반장"),
    ("통장어", r"통장어|통마리|한마리|통장|손질장어|장어\s*손질"),
]
FORM_FALLBACK = "통장어"

# '중국산' 안에 '국산'이 들어 있어 국내산 규칙에 함께 걸리던 문제를 lookbehind 로 막는다.
# '자연산'은 원산지가 아니라 양식 여부이므로 제외했고, '일본'은 '일본식 양념' 같은
# 조리 스타일 표기와 구분되지 않아 '일본산'만 인정한다.
ORIGIN_RULES = [
    ("국내산", r"국내산|한국산|(?<![중한])국산"),
    ("중국산", r"중국산|중국"),
    ("일본산", r"일본산|일본\s*직수입"),
]
STORAGE_RULES = [
    ("냉동", r"냉동|급속냉동|동결"),
    ("냉장", r"냉장|chilled"),
    # '활 민물장어', '활뱀장어'처럼 사이에 어종명이 끼는 표기를 모두 잡는다.
    # '생물'은 생물자원 등과 구분되지 않아 넣지 않았다 — 냉장 규칙으로 잡는다.
    ("활",   r"활\s*(?:민물)?(?:뱀)?장어|활어|살아있는"),
]
# 가공품은 냉동 유통이 사실상 표준이라, 미검출 시 이 형태에 한해 냉동으로 추정한다.
STORAGE_INFERRABLE_FORMS = ("필렛", "양념구이", "장어탕")


def clean_title(raw: str) -> str:
    """HTML 엔티티·태그를 제거하고 공백을 정리한다."""
    return html_lib.unescape(re.sub(r"<[^>]+>", "", raw or "")).strip()


def _norm(title: str) -> str:
    return clean_title(title).lower().replace(",", "")


def is_eel_product(title: str) -> tuple[bool, str]:
    """민물장어 가공품인지 판정한다. (통과 여부, 사유)"""
    t = clean_title(title)
    if "장어" not in t:
        return False, "장어 아님"
    # '장어 뼈', '바다 장어'처럼 띄어 쓴 표기도 잡히도록 공백을 지우고 대조한다
    packed = re.sub(r"\s+", "", t)
    for term in EXCLUDE_TERMS:
        if term in packed:
            return False, f"제외어:{term}"
    # '양념장(소스)'은 제외하되 '양념장어'는 살린다
    if re.search(r"양념장(?!어)", packed):
        return False, "제외어:양념장"
    return True, ""


# ── 중량 ──────────────────────────────────────────────────────────────
_KG = r"(\d+(?:\.\d+)?)\s*(?:kg|킬로|킬로그램)"
_G = r"(\d+(?:\.\d+)?)\s*(?:g|그램|그람)(?![a-wyz])"
_PACK_AFTER = r"\s*(?:x|×|\*|/)\s*(\d{1,2})\s*(?:팩|봉|개|입|세트|ea)?"
_PACK_BEFORE = r"(\d{1,2})\s*(?:팩|봉|개|입|세트)\s*$"


def _weights_in(segment: str) -> float:
    """한 구간의 총 g. 'x2' 배수와 앞쪽 '2팩' 배수를 반영한다."""
    s = segment
    kg_total = 0.0
    for m in re.finditer(_KG, s):
        val = float(m.group(1)) * 1000
        mm = re.match(_PACK_AFTER, s[m.end():])
        kg_total += val * int(mm.group(1)) if mm else val
    g_total = 0.0
    for m in re.finditer(_G, s):
        val = float(m.group(1))
        mm = re.match(_PACK_AFTER, s[m.end():])
        g_total += val * int(mm.group(1)) if mm else val
    # '1kg(500g x 2)' 처럼 같은 양을 두 단위로 쓴 경우 큰 쪽만 취해 이중계산을 막는다
    total = max(kg_total, g_total)
    # '2팩 500g' 처럼 배수가 앞에 오는 표기
    pre = re.search(_PACK_BEFORE, s[:s.find(str(int(total))) if total else 0])
    if pre and total:
        total *= int(pre.group(1))
    return total


def parse_weight_g(title: str) -> int | None:
    """상품명에서 총 중량(g)을 추정한다. 못 찾으면 None."""
    t = _norm(title)
    # 괄호 밖이 총량, 괄호 안은 내역인 경우가 많다 ('2kg(1kg x 2)' → 2kg)
    outer = re.sub(r"[\(\[（【][^\)\]）】]*[\)\]）】]", " ", t)
    inner = " ".join(re.findall(r"[\(\[（【]([^\)\]）】]*)[\)\]）】]", t))
    for seg in (outer, inner, t):
        total = _weights_in(seg)
        if 100 <= total <= 20_000:
            return int(round(total))
    return None


def parse_unit_count(title: str) -> int | None:
    """미수(1팩 기준 마리 수). '8미', '8마리'. 못 찾으면 None."""
    t = clean_title(title)
    m = re.search(r"(\d{1,2})\s*미(?!터|만|나|리|음|성)", t)
    if not m:
        m = re.search(r"(\d{1,2})\s*마리", t)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 30 else None


def _match_rule(rules, text: str):
    for name, pattern in rules:
        if re.search(pattern, text):
            return name
    return None


def parse_form(title: str) -> str | None:
    """명시적으로 드러난 형태만 반환한다. 추정은 parse_title 이 맡는다."""
    return _match_rule(FORM_RULES, clean_title(title))


def parse_origin(title: str) -> str | None:
    """원산지. 둘 이상 검출되면 판정하지 않는다(사람 검토로 넘김)."""
    t = clean_title(title)
    hits = [name for name, pat in ORIGIN_RULES if re.search(pat, t)]
    return hits[0] if len(hits) == 1 else None


def parse_storage(title: str, form: str | None = None) -> tuple[str | None, bool]:
    """(보관, 추정여부). 가공품은 미검출 시 냉동으로 추정한다."""
    found = _match_rule(STORAGE_RULES, clean_title(title))
    if found:
        return found, False
    if form in STORAGE_INFERRABLE_FORMS:
        return "냉동", True
    return None, False


def parse_title(title: str) -> dict:
    """제목 → 속성 일괄 추출. 매칭 L1 의 입력."""
    t = clean_title(title)
    ok, reason = is_eel_product(t)
    form = parse_form(t)
    form_inferred = False
    if form is None and ok:
        # 장어 제품인 건 확실한데 형태 단서가 없으면 통마리로 추정한다
        form, form_inferred = FORM_FALLBACK, True
    storage, inferred = parse_storage(t, form)
    return {
        "title": t,
        "is_eel": ok,
        "reject_reason": reason,
        "form": form,
        "form_inferred": form_inferred,
        "origin": parse_origin(t),
        "storage": storage,
        "storage_inferred": inferred,
        "total_g": parse_weight_g(t),
        "unit_count": parse_unit_count(t),
    }


def item_key(form: str | None, origin: str | None, storage: str | None) -> str | None:
    """품목 정규화 키. 하나라도 비면 자동 배정하지 않는다(pending)."""
    if not (form and origin and storage):
        return None
    return f"{form}|{origin}|{storage}"


# ── 가격 ──────────────────────────────────────────────────────────────
def total_price(sale_price: int | None, shipping_fee: int | None) -> int | None:
    """비교 기준값. 배송비가 '미확인'(None)이면 총액도 확정하지 않는다.

    0(무료배송)과 None(확인 못 함)은 절대 같게 다루지 않는다 — 설계 6-1.
    """
    if sale_price is None or shipping_fee is None:
        return None
    return sale_price + shipping_fee


def won_per_kg(total: int | None, total_g: int | None) -> float | None:
    if not total or not total_g:
        return None
    return round(total / (total_g / 1000.0), 1)


def won_per_ea(total: int | None, unit_count: int | None) -> float | None:
    if not total or not unit_count:
        return None
    return round(total / unit_count, 1)


def band_flags(price: int | None, total_g: int | None) -> str:
    """절대 밴드 이상치 사유. 정상이면 빈 문자열 — 설계 6-3."""
    import retail_config as cfg
    flags = []
    if price is not None and not (cfg.MIN_PRICE <= price <= cfg.MAX_PRICE):
        flags.append("price_band")
    if total_g is not None and not (cfg.MIN_TOTAL_G <= total_g <= cfg.MAX_TOTAL_G):
        flags.append("weight_band")
    return "|".join(flags)


def detect_portal(url: str) -> str | None:
    import retail_config as cfg
    from urllib.parse import urlsplit
    host = urlsplit(url).netloc.lower()
    for portal, hosts in cfg.PORTAL_HOSTS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return portal
    return None
