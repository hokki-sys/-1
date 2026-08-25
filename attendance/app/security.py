"""인증. 사람 계정과 단말 토큰은 완전히 다른 물건입니다.

  * 사람   — 이메일 + 비밀번호(Argon2) -> 서명된 세션 쿠키
  * 단말   — 발급 시 한 번만 보여주는 토큰. 권한은 '탭 전송' 하나뿐이고,
             분실하면 이 토큰만 폐기하면 끝입니다.

**단말에 사람 계정을 넣지 않습니다.** 분실 = 계정 유출이 되기 때문입니다.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import get_settings

_ph = PasswordHasher()
_settings = get_settings()
_serializer = URLSafeTimedSerializer(_settings.secret_key, salt="attendance-session")

DEVICE_TOKEN_BYTES = 32


def hash_password(raw: str) -> str:
    if len(raw) < 8:
        raise ValueError("비밀번호는 8자 이상이어야 합니다")
    return _ph.hash(raw)


def verify_password(raw: str, stored_hash: str) -> bool:
    try:
        return _ph.verify(stored_hash, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


# ------------------------------------------------------------------ 세션 쿠키

def issue_session(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def read_session(token: str, max_age: int | None = None) -> int | None:
    try:
        data = _serializer.loads(token, max_age=max_age or _settings.session_max_age)
    except BadSignature:
        return None
    except Exception:
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return uid if isinstance(uid, int) else None


# ------------------------------------------------------------------ 단말 토큰

def new_device_token() -> str:
    """발급 시 딱 한 번 보여주는 평문 토큰. 저장은 해시만 합니다."""
    return secrets.token_urlsafe(DEVICE_TOKEN_BYTES)


def hash_device_token(token: str) -> str:
    """단말 토큰은 고엔트로피 난수라 느린 해시가 필요 없습니다.

    대신 조회를 위해 결정적이어야 하므로 SHA-256 을 씁니다. 비밀번호에는
    절대 이 방식을 쓰지 않습니다 — 사람이 고른 문자열은 사전 공격에 뚫립니다.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_match(candidate_hash: str, stored_hash: str) -> bool:
    return hmac.compare_digest(candidate_hash, stored_hash)
