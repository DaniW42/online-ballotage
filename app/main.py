import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request, Depends, BackgroundTasks, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import i18n
from .auth import create_session_cookie, read_session, SESSION_COOKIE, SESSION_MAX_AGE, COOKIE_SECURE
from .config import settings
from .db import get_db, new_uuid, Organization, MagicLink, Election, Invitation, Vote
from .mail import send_magic_link
from .ratelimit import limiter
from .services import (
    finalize_due, abort_election, get_results, results_available, maintenance_loop,
    deliver_invitation, create_invitations, reissue_invitation,
)
from .timeutil import utcnow, local_input_to_utc
from .tokens import extract_emails, generate_token, hash_token

log = logging.getLogger("kugelung")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.legal_complete:
        log.warning("Impressum unvollständig: LEGAL_NAME/LEGAL_STREET/LEGAL_CITY/LEGAL_EMAIL setzen.")
    task = asyncio.create_task(maintenance_loop())
    yield
    task.cancel()


app = FastAPI(title="ballotage.online", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


# ---------- Templates, Sprache ----------

def _template_context(request: Request) -> dict:
    lang = getattr(request.state, "lang", i18n.DEFAULT_LANG)
    return {
        "lang": lang,
        "t": lambda key, **kw: i18n.t(lang, key, **kw),
        "tl": lambda key: i18n.raw(lang, key),
        "fmt_dt": lambda dt: i18n.fmt_datetime(lang, dt),
        "fmt_date": lambda dt: i18n.fmt_date(lang, dt),
        "languages": i18n.languages(),
        "logged_in": read_session(request) is not None,
        "cfg": settings,
        "now_year": utcnow().year,
        "base_url": settings.base_url,
    }


templates = Jinja2Templates(directory="app/templates", context_processors=[_template_context])
templates.env.filters["md"] = i18n.render_md


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)


def _set_lang(request: Request, lang: str) -> None:
    if lang in i18n.CATALOGS:
        request.state.lang = lang


@app.middleware("http")
async def common_middleware(request: Request, call_next):
    request.state.lang = i18n.pick_language(
        request.cookies.get(i18n.LANG_COOKIE), request.headers.get("accept-language")
    )
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=3600"
    else:
        # Tokens stehen in der URL: nie als Referrer weitergeben, nie cachen.
        response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    if COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return render(request, "error.html", {"message_key": "errors.not_found", "status": 404}, 404)
    return PlainTextResponse(str(exc.detail), status_code=exc.status_code, headers=exc.headers)


@app.get("/lang/{code}")
def switch_language(code: str, next: str = "/"):
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    if code in i18n.CATALOGS:
        response.set_cookie(i18n.LANG_COOKIE, code, max_age=60 * 60 * 24 * 365,
                            samesite="lax", httponly=True, secure=COOKIE_SECURE)
    return response


def current_org(request: Request, db: Session) -> Organization | None:
    org_id = read_session(request)
    if not org_id:
        return None
    return db.query(Organization).filter(Organization.id == org_id).first()


# ---------- Öffentliche Seiten ----------

PUBLIC_PAGES = {
    "/": "home.html",
    "/how-it-works": "how_it_works.html",
    "/security": "security.html",
    "/faq": "faq.html",
    "/self-hosting": "self_hosting.html",
    "/source": "source.html",
    "/impressum": "impressum.html",
    "/datenschutz": "privacy.html",
}


def _register_public(path: str, template: str):
    def view(request: Request):
        return render(request, template)
    app.add_api_route(path, view, methods=["GET"], response_class=HTMLResponse, name=template)


for _path, _template in PUBLIC_PAGES.items():
    _register_public(_path, _template)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz(db: Session = Depends(get_db)):
    db.execute(text("select 1"))
    return "ok"


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return (
        "User-agent: *\n"
        "Disallow: /v/\nDisallow: /auth/\nDisallow: /dashboard\nDisallow: /elections/\n"
        "Disallow: /login\nDisallow: /register\n"
        f"Sitemap: {settings.base_url}/sitemap.xml\n"
    )


@app.get("/sitemap.xml")
def sitemap():
    urls = "".join(f"<url><loc>{settings.base_url}{p}</loc></url>" for p in PUBLIC_PAGES)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(xml, media_type="application/xml")


@app.get("/.well-known/security.txt", response_class=PlainTextResponse)
def security_txt():
    contact = f"mailto:{settings.legal_email}" if settings.legal_email else f"{settings.base_url}/impressum"
    expires = (utcnow() + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"Contact: {contact}\nExpires: {expires}\nPreferred-Languages: de, en\n"
        f"Canonical: {settings.base_url}/.well-known/security.txt\n"
    )


# ---------- Registrierung & Login (passwortlos) ----------

@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return render(request, "register.html")


@app.post("/register")
def register_submit(
    background_tasks: BackgroundTasks,
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    if not limiter.allow(f"ip:{_client_ip(request)}", settings.rate_limit_per_ip):
        return _too_many(request)
    org = db.query(Organization).filter(Organization.email == email).first()
    if not org:
        org = Organization(name=" ".join(name.split()), email=email)
        db.add(org)
        db.commit()
        db.refresh(org)

    if limiter.allow(f"mail:{email}", settings.rate_limit_per_email):
        _send_magic_link(background_tasks, db, org, request.state.lang)
    return render(request, "magic_sent.html", {"email": email})


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str | None = None):
    return render(request, "login.html", {"expired": error == "expired"})


@app.post("/login")
def login_submit(
    background_tasks: BackgroundTasks,
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    if not limiter.allow(f"ip:{_client_ip(request)}", settings.rate_limit_per_ip):
        return _too_many(request)
    org = db.query(Organization).filter(Organization.email == email).first()
    # Bewusst dieselbe Antwort, egal ob die Email existiert oder das
    # Limit pro Adresse greift (keine Enumeration, kein Mail-Bombing).
    if org and limiter.allow(f"mail:{email}", settings.rate_limit_per_email):
        _send_magic_link(background_tasks, db, org, request.state.lang)
    return render(request, "magic_sent.html", {"email": email})


def _send_magic_link(background_tasks: BackgroundTasks, db: Session, org: Organization, lang: str):
    raw, token_hash = generate_token()
    link = MagicLink(
        org_id=org.id,
        token_hash=token_hash,
        expires_at=utcnow() + timedelta(minutes=settings.magic_link_ttl_minutes),
    )
    db.add(link)
    db.commit()
    url = f"{settings.base_url}/auth/{raw}"
    background_tasks.add_task(send_magic_link, org.email, url, lang)


def _valid_magic_link(db: Session, token: str) -> MagicLink | None:
    link = db.query(MagicLink).filter(MagicLink.token_hash == hash_token(token)).first()
    if not link or link.used_at is not None or link.expires_at < utcnow():
        return None
    return link


@app.get("/auth/{token}", response_class=HTMLResponse)
def auth_confirm(token: str, request: Request, db: Session = Depends(get_db)):
    # Verbraucht den Link bewusst NICHT: Mail-Scanner (z. B. Outlook SafeLinks)
    # rufen Links vorab per GET auf. Erst der Klick auf den Button (POST) zählt.
    if not _valid_magic_link(db, token):
        return RedirectResponse("/login?error=expired")
    return render(request, "auth_confirm.html", {"token": token})


@app.post("/auth/{token}")
def auth_via_magic_link(token: str, db: Session = Depends(get_db)):
    link = _valid_magic_link(db, token)
    if not link:
        return RedirectResponse("/login?error=expired", status_code=303)

    link.used_at = utcnow()
    db.commit()

    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        create_session_cookie(link.org_id),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _too_many(request: Request):
    return render(request, "error.html", {"message_key": "errors.too_many", "status": 429}, 429)


# ---------- Dashboard & Abstimmungen verwalten ----------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    org = current_org(request, db)
    if not org:
        return RedirectResponse("/login")

    elections = (
        db.query(Election)
        .filter(Election.org_id == org.id)
        .order_by(Election.created_at.desc())
        .all()
    )
    return render(request, "dashboard.html", {"org": org, "elections": elections})


@app.get("/elections/new", response_class=HTMLResponse)
def new_election_form(request: Request, db: Session = Depends(get_db)):
    org = current_org(request, db)
    if not org:
        return RedirectResponse("/login")
    return render(request, "new_election.html",
                  {"org": org, "saved": org.saved_recipients or [], "save_checked": True})


def _queue_invitations(background_tasks: BackgroundTasks, election: Election, created: list[tuple],
                       kind: str = "invite"):
    ends = i18n.fmt_datetime(election.language, election.ends_at)
    for inv_id, email, link in created:
        background_tasks.add_task(
            deliver_invitation, inv_id, email, election.title, link, ends, kind, election.language
        )


def _form_error(request: Request, org: Organization, message_key: str,
                emails: list[str] | None = None, **params):
    return render(
        request, "new_election.html",
        {"org": org, "error": i18n.t(request.state.lang, message_key, **params),
         "saved": emails if emails is not None else (org.saved_recipients or []),
         "save_checked": True},
        status_code=400,
    )


@app.post("/recipients/clear")
def clear_recipients(request: Request, db: Session = Depends(get_db)):
    org = current_org(request, db)
    if not org:
        return RedirectResponse("/login", status_code=303)
    org.saved_recipients = []
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/elections/new")
def new_election_submit(
    background_tasks: BackgroundTasks,
    request: Request,
    title: str = Form(...),
    starts_at: str = Form(...),
    ends_at: str = Form(...),
    emails_raw: str = Form(""),
    options_raw: str = Form(""),
    reminder_enabled: bool = Form(False),
    receipts_enabled: bool = Form(False),
    save_recipients: bool = Form(False),
    db: Session = Depends(get_db),
):
    org = current_org(request, db)
    if not org:
        return RedirectResponse("/login")

    # Server-seitig erneut extrahieren/validieren - Client-Liste nie blind
    # übernehmen, unabhängig davon, was das Frontend vorbereitet hat.
    emails = extract_emails(emails_raw)
    if len(emails) < settings.min_voters:
        return _form_error(request, org, "election.new.err_min_voters", emails, n=settings.min_voters)

    # Optionen: Duplikate (case-insensitiv) entfernen, Reihenfolge behalten.
    options, seen = [], set()
    for o in (" ".join(o.split()) for o in options_raw.split(",")):
        if o and o.lower() not in seen:
            seen.add(o.lower())
            options.append(o)
    if not options:
        options = [o.strip() for o in i18n.t(request.state.lang, "election.new.options_default").split(",")]
    if len(options) < 2:
        return _form_error(request, org, "election.new.err_options", emails)

    title = " ".join(title.split())  # Zeilenumbrüche im Titel (Mail-Betreff) vermeiden
    if settings.require_verification and not org.verified:
        return _form_error(request, org, "election.new.err_unverified", emails)

    election = Election(
        org_id=org.id,
        title=title,
        starts_at=local_input_to_utc(starts_at),
        ends_at=local_input_to_utc(ends_at),
        options=options,
        reminder_enabled=reminder_enabled,
        receipts_enabled=receipts_enabled,
        language=request.state.lang,
    )
    db.add(election)
    db.commit()
    db.refresh(election)

    created = create_invitations(db, election, emails)
    if save_recipients:
        org.saved_recipients = emails
    db.commit()
    _queue_invitations(background_tasks, election, created)

    return RedirectResponse(f"/elections/{election.id}", status_code=303)


MESSAGES = {"aborted", "extended", "extend_failed", "not_open", "added", "reissued", "reissue_failed"}


@app.get("/elections/{election_id}", response_class=HTMLResponse)
def election_detail(election_id: str, request: Request, db: Session = Depends(get_db)):
    org = current_org(request, db)
    if not org:
        return RedirectResponse("/login")

    election = (
        db.query(Election)
        .filter(Election.id == election_id, Election.org_id == org.id)
        .first()
    )
    if not election:
        return RedirectResponse("/dashboard")

    finalize_due(db, election.id)
    db.refresh(election)

    if election.status == "open":
        total = db.query(func.count(Invitation.id)).filter(
            Invitation.election_id == election.id
        ).scalar()
        used = db.query(func.count(Invitation.id)).filter(
            Invitation.election_id == election.id, Invitation.used.is_(True)
        ).scalar()
    else:
        total, used = election.total_invited, election.total_voted

    results = get_results(db, election)
    message = request.query_params.get("msg")
    return render(request, "election_detail.html", {
        "org": org,
        "election": election,
        "total": total,
        "used": used,
        "results": results,
        # Zufällige UUIDs, sortiert nach ID: Reihenfolge verrät nichts über die Abgabe
        "ballots": (
            db.query(Vote.id, Vote.choice).filter(Vote.election_id == election.id)
            .order_by(Vote.id).all()
            if election.receipts_enabled and results_available(election) else []
        ),
        "min_voters": settings.min_voters,
        "invitations": db.query(Invitation)
        .filter(Invitation.election_id == election.id)
        .order_by(Invitation.email)  # alphabetisch, nicht nach Einfügereihenfolge
        .all(),
        "message": message if message in MESSAGES else None,
        "added": request.query_params.get("n", ""),
    })


def _own_election(db: Session, request: Request, election_id: str) -> Election | None:
    org = current_org(request, db)
    if not org:
        return None
    return (
        db.query(Election)
        .filter(Election.id == election_id, Election.org_id == org.id)
        .first()
    )


@app.post("/elections/{election_id}/voters")
def election_add_voters(
    election_id: str, request: Request, background_tasks: BackgroundTasks,
    emails_raw: str = Form(""), db: Session = Depends(get_db),
):
    if not _own_election(db, request, election_id):
        return RedirectResponse("/login", status_code=303)
    finalize_due(db, election_id)
    election = db.query(Election).filter(Election.id == election_id).with_for_update().first()
    if election.status != "open":
        db.rollback()
        return RedirectResponse(f"/elections/{election_id}?msg=not_open", status_code=303)
    created = create_invitations(db, election, extract_emails(emails_raw))
    db.commit()
    _queue_invitations(background_tasks, election, created)
    return RedirectResponse(f"/elections/{election_id}?msg=added&n={len(created)}", status_code=303)


@app.post("/elections/{election_id}/invitations/{invitation_id}/reissue")
def election_reissue(
    election_id: str, invitation_id: str, request: Request,
    background_tasks: BackgroundTasks, db: Session = Depends(get_db),
):
    if not _own_election(db, request, election_id):
        return RedirectResponse("/login", status_code=303)
    finalize_due(db, election_id)
    election = db.query(Election).filter(Election.id == election_id).first()
    result = reissue_invitation(db, election, invitation_id) if election.status == "open" else None
    if not result:
        return RedirectResponse(f"/elections/{election_id}?msg=reissue_failed", status_code=303)
    _queue_invitations(background_tasks, election, [result], kind="reissue")
    return RedirectResponse(f"/elections/{election_id}?msg=reissued", status_code=303)


@app.get("/elections/{election_id}/abort", response_class=HTMLResponse)
def election_abort_confirm(election_id: str, request: Request, db: Session = Depends(get_db)):
    election = _own_election(db, request, election_id)
    if not election:
        return RedirectResponse("/login")
    if election.status != "open":
        return RedirectResponse(f"/elections/{election_id}?msg=not_open")
    return render(request, "abort_confirm.html", {"election": election})


@app.post("/elections/{election_id}/abort")
def election_abort(
    election_id: str, request: Request, confirm: str = Form(""), db: Session = Depends(get_db)
):
    if not _own_election(db, request, election_id):
        return RedirectResponse("/login", status_code=303)
    if confirm != "yes":  # Abbruch ist endgültig: nur über die Bestätigungsseite
        return RedirectResponse(f"/elections/{election_id}/abort", status_code=303)
    done = abort_election(db, election_id)
    msg = "aborted" if done else "not_open"
    return RedirectResponse(f"/elections/{election_id}?msg={msg}", status_code=303)


@app.post("/elections/{election_id}/extend")
def election_extend(
    election_id: str, request: Request, ends_at: str = Form(...), db: Session = Depends(get_db)
):
    if not _own_election(db, request, election_id):
        return RedirectResponse("/login", status_code=303)
    finalize_due(db, election_id)  # ggf. erst abschließen; danach ist Verlängern gesperrt
    election = (
        db.query(Election).filter(Election.id == election_id).with_for_update().first()
    )
    new_end = local_input_to_utc(ends_at)
    if election.status != "open" or new_end <= election.ends_at:
        db.rollback()
        return RedirectResponse(f"/elections/{election_id}?msg=extend_failed", status_code=303)
    election.ends_at = new_end
    if election.reminder_enabled:
        election.reminder_sent = False  # neue Frist -> Erinnerung wieder möglich
    db.commit()
    return RedirectResponse(f"/elections/{election_id}?msg=extended", status_code=303)


# ---------- Abstimmen (Token-Link, kein Login) ----------

@app.get("/v/{token}", response_class=HTMLResponse)
def vote_form(token: str, request: Request, db: Session = Depends(get_db)):
    token_hash = hash_token(token)
    invitation = db.query(Invitation).filter(Invitation.token_hash == token_hash).first()

    if not invitation:
        return render(request, "vote_invalid.html", {}, 404)
    election = db.query(Election).filter(Election.id == invitation.election_id).first()
    _set_lang(request, election.language)  # Wähler sehen die Sprache der Abstimmung
    if invitation.used:
        return render(request, "vote_invalid.html", {"reason": "used"}, 410)

    now = utcnow()
    if election.status != "open" or now < election.starts_at or now > election.ends_at:
        return render(request, "vote_invalid.html", {"reason": "window", "election": election}, 403)

    return render(request, "vote.html", {"election": election, "token": token})


@app.post("/v/{token}")
def vote_submit(token: str, request: Request, choice: str = Form(...), db: Session = Depends(get_db)):
    token_hash = hash_token(token)

    # Pessimistisches Lock: verhindert, dass derselbe Token durch gleichzeitige
    # Requests (Doppelklick, doppelter Mail-Client-Prefetch) zweimal zählt.
    invitation = (
        db.query(Invitation)
        .filter(Invitation.token_hash == token_hash)
        .with_for_update()
        .first()
    )
    if not invitation or invitation.used:
        return render(request, "vote_invalid.html", {"reason": "used"}, 410)

    # Geteilte Sperre: ein parallel laufender Abschluss (FOR UPDATE) wartet,
    # bis diese Stimme committet ist - oder wir sehen schon den Abschluss.
    election = (
        db.query(Election)
        .filter(Election.id == invitation.election_id)
        .with_for_update(read=True)
        .first()
    )
    _set_lang(request, election.language)
    now = utcnow()
    if election.status != "open" or now < election.starts_at or now > election.ends_at:
        db.rollback()
        return render(request, "vote_invalid.html", {"reason": "window", "election": election}, 403)
    if choice not in election.options:
        db.rollback()
        return render(request, "vote_invalid.html", {"reason": "bad_choice"}, 400)

    invitation.used = True
    # Bewusst KEINE Referenz auf invitation.id - Stimme und Einladung bleiben
    # in der Datenbank vollständig getrennt.
    vote = Vote(id=new_uuid(), election_id=election.id, choice=choice)
    db.add(vote)
    db.commit()

    code = vote.id if election.receipts_enabled else None
    return render(request, "vote_done.html", {"election": election, "code": code})
