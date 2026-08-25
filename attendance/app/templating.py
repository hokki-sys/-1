from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from .domain.workhours import fmt_hours, fmt_minutes

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

WEEKDAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]

STATUS_LABEL = {
    "open": "근무 중",
    "complete": "정상",
    "missing_checkout": "퇴근 누락",
}


def hhmm(value: datetime | None) -> str:
    return value.strftime("%H:%M") if value else "—"


def ymd(value: date | datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else "—"


def md_day(value: date) -> str:
    return f"{value.month}/{value.day} {WEEKDAYS_KO[value.weekday()]}"


templates.env.filters["hhmm"] = hhmm
templates.env.filters["ymd"] = ymd
templates.env.filters["md_day"] = md_day
templates.env.filters["mins"] = fmt_minutes
templates.env.filters["hours"] = fmt_hours
templates.env.globals["WEEKDAYS_KO"] = WEEKDAYS_KO
templates.env.globals["STATUS_LABEL"] = STATUS_LABEL
