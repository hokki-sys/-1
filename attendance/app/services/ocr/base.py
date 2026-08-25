"""OCR 제공자 인터페이스.

**구현체를 직접 부르지 않습니다.** DeepSeek 비전 모델은 이름이 -exp, 즉 실험
단계라 사양이 바뀌거나 내려갈 수 있습니다. 급여에 닿는 시스템이 실험 엔드포인트를
직접 붙들면 안 되므로, 여기 인터페이스를 두고 설정으로 갈아끼웁니다.
같은 테스트 이미지로 엔진을 비교하는 것도 이 덕분에 공짜가 됩니다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

_FENCE = re.compile(r"```(?:json)?\s*([\s\S]+?)\s*```", re.MULTILINE)


@dataclass
class OcrResult:
    provider: str
    model: str
    tiles: int
    raw_response: str            # AI 원본 응답 — 인식 품질 검증의 유일한 근거
    parsed: dict = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.parsed.get("schedule_details"))


class OcrProvider(Protocol):
    name: str
    model: str

    def extract_schedule(self, image: bytes, mime: str = "image/jpeg") -> OcrResult: ...


def parse_json_payload(text: str) -> dict:
    """모델 응답에서 JSON 을 꺼냅니다. 코드블록으로 감싸 오는 경우가 많습니다."""
    if not text or not text.strip():
        raise ValueError("빈 응답입니다")
    m = _FENCE.search(text)
    candidate = m.group(1) if m else text.strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("응답에서 JSON 객체를 찾지 못했습니다")
        data = json.loads(candidate[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("최상위가 JSON 객체가 아닙니다")
    return data


def normalise(data: dict) -> dict:
    """모델이 조금씩 다르게 주는 필드를 하나로 맞춥니다."""
    details = data.get("schedule_details") or data.get("entries") or []
    out = []
    seen = set()
    for item in details:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        day = str(item.get("day") or "").strip()[:1]
        if not name or day not in "월화수목금토일":
            continue
        start = str(item.get("start_time") or "").strip()
        end = str(item.get("end_time") or "").strip()
        key = (day, name, start, end)
        if key in seen:      # 조각이 겹치므로 같은 칸이 두 번 잡힙니다
            continue
        seen.add(key)
        out.append({
            "day": day,
            "department": str(item.get("department") or "").strip(),
            "name": name,
            "start_time": start,
            "end_time": end,
        })
    week = data.get("week_info") or {}
    return {
        "week_info": {
            "week_start_date": str(week.get("week_start_date") or "").strip(),
            "week_end_date": str(week.get("week_end_date") or "").strip(),
        },
        "schedule_details": out,
    }
