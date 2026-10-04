"""Gemeinsame Web-Helfer: Templates, Rendering, Kontext (von main.py und admin.py genutzt)."""
import hashlib
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import i18n
from .auth import read_session, read_admin_cookie
from .config import settings
from .db import Election
from .timeutil import utcnow, to_input_value

# Cache-Busting für statische Dateien: Änderung am Inhalt = neue URL
_static_dir = Path(__file__).parent / "static"
STATIC_VERSION = hashlib.sha1(
    b"".join(p.read_bytes() for p in sorted(_static_dir.rglob("*")) if p.is_file())
).hexdigest()[:10]


def election_phase(election: Election) -> str:
    """Anzeigestatus: scheduled | open | finished | aborted ('open' erst ab Beginn)."""
    if election.status == "open" and utcnow() < election.starts_at:
        return "scheduled"
    return election.status


def _template_context(request: Request) -> dict:
    lang = getattr(request.state, "lang", i18n.DEFAULT_LANG)
    return {
        "lang": lang,
        "theme": getattr(request.state, "theme", "auto"),
        "t": lambda key, **kw: i18n.t(lang, key, **kw),
        "tl": lambda key: i18n.tl(lang, key),
        "fmt_dt": lambda dt: i18n.fmt_datetime(lang, dt),
        "fmt_date": lambda dt: i18n.fmt_date(lang, dt),
        "languages": i18n.languages(),
        "logged_in": read_session(request) is not None,
        "is_admin": read_admin_cookie(request) is not None,
        "cfg": settings,
        "now_year": utcnow().year,
        "static_v": STATIC_VERSION,
        "phase": election_phase,
        "input_dt": to_input_value,
        "reminder_hours": settings.reminder_hours_before,
        "base_url": settings.base_url,
    }


templates = Jinja2Templates(directory="app/templates", context_processors=[_template_context])
templates.env.filters["md"] = i18n.render_md


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)
