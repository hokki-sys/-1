"""근무표 사진을 조각내기 (설계 D7).

DeepSeek 비전은 이미지 한 장을 최대 384 토큰으로 만들고, 큰 이미지는 총 픽셀이
대략 800x800 수준이 되도록 축소한 뒤 토큰화합니다. A4 근무표를 통째로 보내면
40px 이던 글자가 13px 로 줄어 손글씨 이름이 뭉갭니다.

해법은 다른 모델이 아니라 조각내기입니다. 조각마다 384 토큰과 축소 예산을
따로 받으므로, 6조각이면 실효 해상도가 6배가 됩니다. 조각 경계에서 글자가
잘리지 않도록 겹침(overlap)을 둡니다.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

#: 이 픽셀 수를 넘으면 서비스 쪽에서 축소되므로, 그 아래로 조각내는 게 목표입니다.
TARGET_PIXELS_PER_TILE = 800 * 800
DEFAULT_OVERLAP = 0.08
MAX_TILES = 12


@dataclass(frozen=True)
class Tile:
    index: int
    label: str          # "1행 2열" — 모델에게 위치를 알려주기 위한 라벨
    data: bytes
    width: int
    height: int


def suggest_grid(width: int, height: int, target: int = TARGET_PIXELS_PER_TILE) -> tuple[int, int]:
    """조각을 몇 개로 나눌지 정합니다. 돌려주는 값은 (열, 행).

    조각 하나가 `target` 픽셀 아래로 떨어지면 서비스 쪽 축소가 거의 일어나지
    않으므로 원본 해상도가 그대로 살아납니다. 화면 비율을 유지해 나누고,
    조각 수는 MAX_TILES 로 묶습니다.
    """
    if width <= 0 or height <= 0:
        return 1, 1
    total = width * height
    if total <= target:
        return 1, 1

    needed = total / target
    aspect = width / height
    cols = max(1, round((needed * aspect) ** 0.5))
    rows = max(1, round((needed / aspect) ** 0.5))

    # 반올림 때문에 목표에 못 미치면 긴 쪽을 한 칸씩 늘립니다.
    while cols * rows < needed and cols * rows < MAX_TILES:
        if cols / rows < aspect:
            cols += 1
        else:
            rows += 1
    while cols * rows > MAX_TILES:
        if cols >= rows:
            cols -= 1
        else:
            rows -= 1
    return max(1, cols), max(1, rows)


def split_image(
    data: bytes,
    cols: int | None = None,
    rows: int | None = None,
    overlap: float = DEFAULT_OVERLAP,
    include_overview: bool = True,
) -> list[Tile]:
    """조각 목록. 첫 조각은 전체 사진(맥락용)이고 나머지가 확대 조각입니다.

    전체 사진을 같이 보내는 이유는, 조각만 보면 모델이 표의 머리글(요일·부서)을
    못 봐서 어느 열인지 모르기 때문입니다.
    """
    if not 0 <= overlap < 0.5:
        raise ValueError(f"겹침은 0 이상 0.5 미만이어야 합니다: {overlap}")
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        w, h = im.size
        if cols is None or rows is None:
            cols, rows = suggest_grid(w, h)
        cols, rows = max(1, cols), max(1, rows)

        tiles: list[Tile] = []
        if include_overview and cols * rows > 1:
            tiles.append(Tile(0, "전체", _encode(im), w, h))

        if cols * rows == 1:
            return tiles or [Tile(0, "전체", _encode(im), w, h)]

        tile_w, tile_h = w / cols, h / rows
        pad_x, pad_y = tile_w * overlap, tile_h * overlap
        for r in range(rows):
            for c in range(cols):
                left = max(0, int(c * tile_w - pad_x))
                upper = max(0, int(r * tile_h - pad_y))
                right = min(w, int((c + 1) * tile_w + pad_x))
                lower = min(h, int((r + 1) * tile_h + pad_y))
                crop = im.crop((left, upper, right, lower))
                tiles.append(
                    Tile(
                        index=len(tiles),
                        label=f"{r + 1}행 {c + 1}열",
                        data=_encode(crop),
                        width=crop.width,
                        height=crop.height,
                    )
                )
        return tiles


def _encode(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92, optimize=True)
    return buf.getvalue()
