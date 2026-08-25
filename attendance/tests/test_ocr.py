"""근무표 인식 (설계 D7).

실제 API 는 부르지 않습니다 — 키가 필요하고, 응답이 바뀔 수 있는 실험 단계
엔드포인트입니다. 대신 우리가 책임지는 부분을 검증합니다:
조각내기 계산, 응답 해석, 겹친 조각의 중복 제거, 그리고 실패 처리.
"""
from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from app.services.ocr import normalise, parse_json_payload, split_image, suggest_grid
from app.services.ocr.deepseek import DeepSeekVisionOcr
from app.services.ocr.tiling import TARGET_PIXELS_PER_TILE


def make_image(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "JPEG")
    return buf.getvalue()


# ------------------------------------------------------------- 조각내기

def test_small_image_is_not_split():
    assert suggest_grid(700, 700) == (1, 1)
    tiles = split_image(make_image(700, 700))
    assert len(tiles) == 1


@pytest.mark.parametrize("w,h", [(1200, 900), (3000, 2000), (2000, 3000)])
def test_tiles_land_under_the_downscale_threshold(w, h):
    """조각 하나가 800x800 예산 아래여야 원본 해상도가 살아납니다."""
    cols, rows = suggest_grid(w, h)
    assert (w / cols) * (h / rows) <= TARGET_PIXELS_PER_TILE


def test_split_includes_a_full_frame_for_context():
    """조각만 보면 모델이 표의 머리글을 못 봅니다. 전체를 같이 보냅니다."""
    tiles = split_image(make_image(3000, 2000))
    assert tiles[0].label == "전체"
    assert (tiles[0].width, tiles[0].height) == (3000, 2000)
    assert len(tiles) > 1


def test_tiles_overlap_so_boundary_text_is_not_cut():
    tiles = split_image(make_image(2000, 1000), cols=2, rows=1, overlap=0.1)
    pieces = [t for t in tiles if t.label != "전체"]
    assert len(pieces) == 2
    # 겹침이 있으므로 조각 폭의 합이 원본보다 넓습니다.
    assert sum(t.width for t in pieces) > 2000


def test_overlap_bounds_are_checked():
    with pytest.raises(ValueError):
        split_image(make_image(1000, 1000), overlap=0.6)


# ------------------------------------------------------------ 응답 해석

def test_parse_json_from_fenced_block():
    text = '설명\n```json\n{"a": 1}\n```\n꼬리말'
    assert parse_json_payload(text) == {"a": 1}


def test_parse_json_without_fence():
    assert parse_json_payload('앞말 {"a": 2} 뒷말') == {"a": 2}


@pytest.mark.parametrize("bad", ["", "   ", "JSON 없음"])
def test_parse_json_rejects_unusable(bad):
    with pytest.raises(ValueError):
        parse_json_payload(bad)


def test_normalise_drops_duplicates_from_overlapping_tiles():
    data = {"schedule_details": [
        {"day": "월", "name": "호진", "department": "홀", "start_time": "10:00", "end_time": "22:00"},
        {"day": "월", "name": "호진", "department": "홀", "start_time": "10:00", "end_time": "22:00"},
        {"day": "화", "name": "호진", "department": "홀", "start_time": "10:00", "end_time": "22:00"},
    ]}
    assert len(normalise(data)["schedule_details"]) == 2


def test_normalise_drops_unusable_rows():
    data = {"schedule_details": [
        {"day": "월", "name": "", "start_time": "10:00"},          # 이름 없음
        {"day": "달", "name": "호진", "start_time": "10:00"},       # 요일 아님
        "문자열",                                                    # 형식 자체가 다름
        {"day": "수", "name": "호진", "start_time": "10:00", "end_time": ""},
    ]}
    out = normalise(data)["schedule_details"]
    assert len(out) == 1 and out[0]["day"] == "수" and out[0]["end_time"] == ""


def test_normalise_keeps_week_info_empty_when_model_omits_it():
    """연도를 추측하지 말라고 지시했으므로, 비어 있는 게 정상입니다."""
    out = normalise({"schedule_details": []})
    assert out["week_info"] == {"week_start_date": "", "week_end_date": ""}


# --------------------------------------------------- 제공자 (가짜 응답)

def _provider(handler) -> DeepSeekVisionOcr:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    return DeepSeekVisionOcr("key", "http://x", "test-model", ("홀", "주방"), client=client)


def test_provider_sends_every_tile_as_an_image():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        content = body["messages"][1]["content"]
        seen["images"] = sum(1 for c in content if c["type"] == "image_url")
        seen["prompt"] = content[0]["text"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"week_info": {}, "schedule_details": [
                {"day": "월", "name": "호진", "department": "홀",
                 "start_time": "10:00", "end_time": "22:00"}]})}}]})

    result = _provider(handler).extract_schedule(make_image(3000, 2000))
    assert result.ok
    assert seen["images"] == result.tiles > 1
    assert "홀, 주방" in seen["prompt"]        # 매장 부서를 알려줍니다
    assert "1행 1열" in seen["prompt"]         # 조각 위치도 알려줍니다


def test_provider_keeps_the_raw_response_even_on_success():
    """AI 원본 응답은 인식 품질을 나중에 검증할 유일한 근거입니다."""
    payload = json.dumps({"week_info": {}, "schedule_details": [
        {"day": "화", "name": "서연", "start_time": "17:00", "end_time": "22:00"}]})

    result = _provider(lambda r: httpx.Response(
        200, json={"choices": [{"message": {"content": payload}}]}
    )).extract_schedule(make_image(900, 700))
    assert result.ok and result.raw_response == payload


def test_provider_reports_http_errors_without_raising():
    result = _provider(lambda r: httpx.Response(429, text="rate limited")) \
        .extract_schedule(make_image(900, 700))
    assert not result.ok
    assert "429" in result.error


def test_provider_reports_unparseable_output():
    result = _provider(lambda r: httpx.Response(
        200, json={"choices": [{"message": {"content": "미안하지만 읽을 수 없습니다"}}]}
    )).extract_schedule(make_image(900, 700))
    assert not result.ok
    assert "JSON" in result.error


def test_provider_reports_empty_result():
    result = _provider(lambda r: httpx.Response(200, json={"choices": [{"message": {
        "content": json.dumps({"week_info": {}, "schedule_details": []})}}]})) \
        .extract_schedule(make_image(900, 700))
    assert not result.ok
    assert "하나도 찾지 못했습니다" in result.error


def test_provider_survives_a_corrupt_image():
    result = _provider(lambda r: httpx.Response(200, json={})) \
        .extract_schedule(b"not an image at all")
    assert not result.ok
    assert "이미지를 열 수 없습니다" in result.error
