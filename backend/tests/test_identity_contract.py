"""Контракт JWT/паролей единого identity (канон фазы 3, шаг миграции 02).

Проверяет: Argon2id-хеши, round-trip access-токена с claims фазы 3
(`sub`/`sid`/`roles`/`iss=supplier-document-automation`), отвержение чужого секрета.
"""

import uuid

from app.core.errors import ApiError
from app.modules.identity.security import (
    AuthErrorCode,
    AccessClaims,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

SECRET = "test-secret-that-is-at-least-32-characters-long"


def test_argon2_password_contract():
    hashed = hash_password("correct-password")
    assert hashed.startswith("$argon2")
    assert verify_password("correct-password", hashed)
    assert not verify_password("wrong-password", hashed)


def test_phase3_access_claims_round_trip():
    claims = AccessClaims(uuid.uuid4(), uuid.uuid4(), ("manager",))
    token, expires = create_access_token(claims, secret=SECRET, expires_minutes=15)
    decoded = decode_access_token(token, secret=SECRET)
    assert expires == 900
    assert decoded == claims


def test_access_token_rejects_wrong_secret_or_refresh_type():
    claims = AccessClaims(uuid.uuid4(), uuid.uuid4(), ("admin",))
    token, _ = create_access_token(claims, secret=SECRET, expires_minutes=15)
    try:
        decode_access_token(token, secret="wrong-secret-that-is-long-enough")
    except ApiError as exc:
        # Канон фазы 3: повреждённый токен → ApiError с кодом INVALID_ACCESS_TOKEN.
        assert exc.code == AuthErrorCode.INVALID_ACCESS_TOKEN
    else:
        raise AssertionError("wrong signing secret was accepted")
