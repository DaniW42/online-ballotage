from fastapi import Request
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from .config import settings

SESSION_COOKIE = "session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 Tage
COOKIE_SECURE = settings.base_url.startswith("https")  # lokal über http sonst nicht setzbar

serializer = URLSafeTimedSerializer(settings.secret_key, salt="session")


def create_session_cookie(org_id: str, version: int = 0) -> str:
    return serializer.dumps({"org_id": org_id, "v": version})


def read_session_data(request: Request) -> tuple[str, int] | None:
    """(org_id, Sitzungsversion) aus dem Cookie, sofern die Signatur gültig ist."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = serializer.loads(raw, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    org_id = data.get("org_id")
    return (org_id, int(data.get("v", 0))) if org_id else None


def read_session(request: Request) -> str | None:
    """Gibt org_id zurück, falls das Cookie gültig signiert ist, sonst None.
    Ob die Sitzung serverseitig noch gilt (nicht abgemeldet), prüft current_org()."""
    data = read_session_data(request)
    return data[0] if data else None
