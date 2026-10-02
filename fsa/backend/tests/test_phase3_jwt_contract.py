import uuid
import jwt
from app.config import Settings

SECRET = "test-secret-that-is-at-least-32-characters-long"

def test_phase3_jwt_claims_match_fsa_settings():
    settings = Settings(phase3_auth_secret=SECRET)
    token = jwt.encode({"sub": str(uuid.uuid4()), "roles": ["manager"], "type": "access", "iss": settings.phase3_auth_issuer, "aud": settings.phase3_auth_audience}, SECRET, algorithm="HS256")
    payload = jwt.decode(token, settings.phase3_auth_secret, algorithms=["HS256"], issuer=settings.phase3_auth_issuer, audience=settings.phase3_auth_audience)
    assert payload["type"] == "access"
    assert payload["roles"] == ["manager"]
