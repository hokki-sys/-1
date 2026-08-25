from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

CONTROL_SCHEMA = "control"
STORE_SCHEMA_PREFIX = "store_"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres@127.0.0.1:5432/attendance"
    secret_key: str = "dev-only-change-me"
    debug: bool = False
    default_timezone: str = "Asia/Seoul"
    session_cookie: str = "att_session"
    session_max_age: int = 60 * 60 * 12
    #: 세션 쿠키에 Secure 플래그를 붙일지. 기본은 붙입니다(HTTPS 전제).
    #: HTTP 로만 접근하는 내부망 배포라면 여기만 false 로 두세요 — debug 를 켜면
    #: API 문서까지 열립니다. 이 값이 잘못되면 로그인이 조용히 무한 반복됩니다.
    cookie_secure: bool | None = None

    # OCR 은 선택 기능입니다. 키가 없으면 근무표 가져오기가 꺼지고 편집기만 남습니다.
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_vision_model: str = "deepseek-v4-flash-vision-exp"

    @property
    def secure_cookies(self) -> bool:
        return (not self.debug) if self.cookie_secure is None else self.cookie_secure

    @property
    def ocr_enabled(self) -> bool:
        return bool(self.deepseek_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def schema_for(slug: str) -> str:
    """매장 슬러그 -> 스키마 이름. 슬러그는 provisioning 에서 검증합니다."""
    return f"{STORE_SCHEMA_PREFIX}{slug}"
