"""DeepSeek 비전으로 근무표 사진 읽기 (설계 D7).

조각을 **한 요청에 모두** 넣습니다. 조각마다 토큰 예산을 따로 받으면서도,
모델은 전체 사진과 모든 조각을 같이 보므로 머리글(요일·부서)을 잃지 않습니다.

주의: 이미지가 해외로 전송됩니다. 직원 이름이 든 근무표 사진이므로 개인정보
국외이전에 해당할 수 있고, 매장별로 켜고 끌 수 있게 되어 있습니다.
"""
from __future__ import annotations

import base64

import httpx

from ...config import get_settings
from .base import OcrResult, normalise, parse_json_payload
from .tiling import Tile, split_image

TIMEOUT_SECONDS = 120

_SYSTEM = (
    "당신은 주간 근무표 이미지를 JSON 으로 옮기는 도구입니다. "
    "보이는 것만 옮기고, 읽을 수 없는 칸은 만들어내지 말고 그냥 빼세요."
)


def _instructions(tiles: list[Tile], departments: tuple[str, ...]) -> str:
    layout = "\n".join(f"  - 이미지 {t.index + 1}: {t.label}" for t in tiles)
    dept_line = (
        f"이 매장의 부서는 {', '.join(departments)} 입니다. "
        "목록에 없는 부서가 보이면 그대로 적으세요."
        if departments
        else "부서 구분이 있으면 그대로 적고, 없으면 빈 문자열로 두세요."
    )
    return f"""아래 이미지들은 **같은 근무표 한 장**을 나눠 찍은 것입니다.

{layout}

첫 이미지가 전체이고 나머지는 같은 표의 확대 조각입니다. 조각은 서로 겹치므로
같은 칸이 두 번 보일 수 있습니다. 겹친 칸은 한 번만 적으세요.

{dept_line}

다음 JSON 만 출력하세요. 설명이나 다른 텍스트는 넣지 마세요.

{{
  "week_info": {{ "week_start_date": "YYYY-MM-DD", "week_end_date": "YYYY-MM-DD" }},
  "schedule_details": [
    {{ "day": "월", "department": "홀", "name": "직원 이름",
       "start_time": "HH:MM", "end_time": "HH:MM" }}
  ]
}}

규칙:
1. day 는 월·화·수·목·금·토·일 중 한 글자입니다.
2. 시간은 24시간제 "HH:MM". "10-22" 는 start_time "10:00", end_time "22:00" 입니다.
3. 퇴근 시간이 안 적혀 있으면("10~" 같은 경우) end_time 은 빈 문자열 "" 로 둡니다.
4. 빈 칸, "휴무", "OFF" 는 목록에 넣지 않습니다.
5. 이름에 직책이 붙어 있으면("과장" 등) 그대로 이름으로 적습니다.
6. 기간이 "6월 9일 ~ 6월 15일" 처럼 연도 없이 적혀 있으면 week_info 는
   비워 두세요. 연도를 추측하지 마세요 — 사람이 확인해서 채웁니다.
"""


class DeepSeekVisionOcr:
    """OcrProvider 구현체. 인터페이스 뒤에 있으므로 교체가 설정 한 줄입니다."""

    name = "deepseek"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        departments: tuple[str, ...] = (),
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.departments = departments
        self._client = client

    def extract_schedule(self, image: bytes, mime: str = "image/jpeg") -> OcrResult:
        try:
            tiles = split_image(image)
        except Exception as exc:
            return OcrResult(self.name, self.model, 0, "", error=f"이미지를 열 수 없습니다: {exc}")

        content: list[dict] = [{"type": "text", "text": _instructions(tiles, self.departments)}]
        for t in tiles:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(t.data).decode()
                },
            })

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": content},
            ],
            "temperature": 0,
            "stream": False,
        }

        try:
            client = self._client or httpx.Client(timeout=TIMEOUT_SECONDS)
            try:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                resp.raise_for_status()
                body = resp.json()
            finally:
                if self._client is None:
                    client.close()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:400]
            return OcrResult(
                self.name, self.model, len(tiles), detail,
                error=f"API 오류 {exc.response.status_code}: {detail}",
            )
        except Exception as exc:
            return OcrResult(self.name, self.model, len(tiles), "", error=f"요청 실패: {exc}")

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return OcrResult(self.name, self.model, len(tiles), str(body)[:2000],
                             error="응답 형식이 예상과 다릅니다")

        try:
            parsed = normalise(parse_json_payload(text))
        except Exception as exc:
            return OcrResult(self.name, self.model, len(tiles), text, error=f"JSON 해석 실패: {exc}")

        if not parsed["schedule_details"]:
            return OcrResult(self.name, self.model, len(tiles), text, parsed,
                             error="근무 항목을 하나도 찾지 못했습니다")
        return OcrResult(self.name, self.model, len(tiles), text, parsed)


def build_provider(departments: tuple[str, ...] = ()) -> DeepSeekVisionOcr | None:
    """설정이 없으면 None — 그러면 가져오기 기능이 화면에서 사라집니다."""
    s = get_settings()
    if not s.ocr_enabled:
        return None
    return DeepSeekVisionOcr(
        api_key=s.deepseek_api_key,
        base_url=s.deepseek_base_url,
        model=s.deepseek_vision_model,
        departments=departments,
    )
