from fastapi import Request, HTTPException
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from .config import settings

SESSION_COOKIE = "session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 Tage

serializer = URLSafeTimedSerializer(settings.secret_key, salt="session")


def create_session_cookie(org_id: str) -> str:
    return serializer.dumps({"org_id": org_id})


def read_session(request: Request) -> str | None:
    """Gibt org_id zurück, falls eine gültige Session vorliegt, sonst None."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = serializer.loads(raw, max_age=SESSION_MAX_AGE)
        return data.get("org_id")
    except (BadSignature, SignatureExpired):
        return None


def require_org(request: Request) -> str:
    org_id = read_session(request)
    if not org_id:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return org_id
