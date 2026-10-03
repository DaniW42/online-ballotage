import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request, Depends, BackgroundTasks, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func

from .config import settings
from .db import init_db, get_db, Organization, MagicLink, Election, Invitation, Vote
from .tokens import extract_emails, generate_token, hash_token, is_valid_email
from .mail import send_magic_link, send_vote_invitation
from .auth import create_session_cookie, read_session, SESSION_COOKIE, SESSION_MAX_AGE, COOKIE_SECURE
from .ratelimit import limiter
from .timeutil import utcnow, local_input_to_utc, fmt_local, to_local

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Kugelung", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["local"] = fmt_local
templates.env.filters["local_date"] = lambda dt: fmt_local(dt, "%d.%m.%Y")


@app.middleware("http")
async def privacy_headers(request: Request, call_next):
    response = await call_next(request)
    # Tokens stehen in der URL: nie als Referrer weitergeben, nie cachen.
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def current_org(request: Request, db: Session) -> Organization | None:
    org_id = read_session(request)
    if not org_id:
        return None
    return db.query(Organization).filter(Organization.id == org_id).first()


# ---------- Registrierung & Login (passwortlos) ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    org = current_org(request, db)
    if org:
        return RedirectResponse("/dashboard")
    return RedirectResponse("/register")


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


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
        org = Organization(name=name.strip(), email=email)
        db.add(org)
        db.commit()
        db.refresh(org)

    if limiter.allow(f"mail:{email}", settings.rate_limit_per_email):
        _send_magic_link(background_tasks, db, org)
    return templates.TemplateResponse("magic_sent.html", {"request": request, "email": email})


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


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
        _send_magic_link(background_tasks, db, org)
    return templates.TemplateResponse("magic_sent.html", {"request": request, "email": email})


def _send_magic_link(background_tasks: BackgroundTasks, db: Session, org: Organization):
    raw, token_hash = generate_token()
    link = MagicLink(
        org_id=org.id,
        token_hash=token_hash,
        expires_at=utcnow() + timedelta(minutes=settings.magic_link_ttl_minutes),
    )
    db.add(link)
    db.commit()
    url = f"{settings.base_url}/auth/{raw}"
    background_tasks.add_task(send_magic_link, org.email, url)


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
    return templates.TemplateResponse("auth_confirm.html", {"request": request, "token": token})


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
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "message": "Zu viele Anfragen. Bitte später erneut versuchen."},
        status_code=429,
    )


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
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "org": org, "elections": elections}
    )


@app.get("/elections/new", response_class=HTMLResponse)
def new_election_form(request: Request, db: Session = Depends(get_db)):
    org = current_org(request, db)
    if not org:
        return RedirectResponse("/login")
    return templates.TemplateResponse("new_election.html", {"request": request, "org": org})


def _form_error(request: Request, org: Organization, message: str):
    return templates.TemplateResponse(
        "new_election.html", {"request": request, "org": org, "error": message}, status_code=400
    )


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
    db: Session = Depends(get_db),
):
    org = current_org(request, db)
    if not org:
        return RedirectResponse("/login")

    # Server-seitig erneut extrahieren/validieren - Client-Liste nie blind
    # übernehmen, unabhängig davon, was das Frontend vorbereitet hat.
    emails = extract_emails(emails_raw)

    # Optionen: Duplikate (case-insensitiv) entfernen, Reihenfolge behalten.
    options, seen = [], set()
    for o in (" ".join(o.split()) for o in options_raw.split(",")):
        if o and o.lower() not in seen:
            seen.add(o.lower())
            options.append(o)
    options = options or ["Ja", "Nein", "Enthaltung"]
    if len(options) < 2:
        return _form_error(request, org, "Mindestens zwei Abstimmungsoptionen angeben.")

    title = " ".join(title.split())  # Zeilenumbrüche im Titel (Mail-Betreff) vermeiden
    if settings.require_verification and not org.verified:
        return _form_error(request, org, "Ihre Loge wurde noch nicht freigegeben.")

    election = Election(
        org_id=org.id,
        title=title,
        starts_at=local_input_to_utc(starts_at),
        ends_at=local_input_to_utc(ends_at),
        options=options,
        reminder_enabled=reminder_enabled,
    )
    db.add(election)
    db.commit()
    db.refresh(election)

    for email in emails:
        raw_token, token_hash = generate_token()
        invitation = Invitation(election_id=election.id, email=email, token_hash=token_hash)
        db.add(invitation)
        vote_link = f"{settings.base_url}/v/{raw_token}"
        background_tasks.add_task(
            send_vote_invitation,
            email,
            election.title,
            vote_link,
            fmt_local(election.ends_at),
        )
    db.commit()

    return RedirectResponse(f"/elections/{election.id}", status_code=303)


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

    total = db.query(func.count(Invitation.id)).filter(
        Invitation.election_id == election.id
    ).scalar()
    used = db.query(func.count(Invitation.id)).filter(
        Invitation.election_id == election.id, Invitation.used.is_(True)
    ).scalar()

    is_over = utcnow() > election.ends_at
    results = None
    if is_over:
        rows = (
            db.query(Vote.choice, func.count(Vote.id))
            .filter(Vote.election_id == election.id)
            .group_by(Vote.choice)
            .all()
        )
        results = dict(rows)

    return templates.TemplateResponse(
        "election_detail.html",
        {
            "request": request,
            "org": org,
            "election": election,
            "total": total,
            "used": used,
            "is_over": is_over,
            "results": results,
        },
    )


# ---------- Abstimmen (Token-Link, kein Login) ----------

@app.get("/v/{token}", response_class=HTMLResponse)
def vote_form(token: str, request: Request, db: Session = Depends(get_db)):
    token_hash = hash_token(token)
    invitation = db.query(Invitation).filter(Invitation.token_hash == token_hash).first()

    if not invitation:
        return templates.TemplateResponse("vote_invalid.html", {"request": request}, status_code=404)
    if invitation.used:
        return templates.TemplateResponse(
            "vote_invalid.html", {"request": request, "reason": "used"}, status_code=410
        )

    election = db.query(Election).filter(Election.id == invitation.election_id).first()
    now = utcnow()
    if now < election.starts_at or now > election.ends_at:
        return templates.TemplateResponse(
            "vote_invalid.html",
            {"request": request, "reason": "window", "election": election},
            status_code=403,
        )

    return templates.TemplateResponse(
        "vote.html", {"request": request, "election": election, "token": token}
    )


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
        return templates.TemplateResponse(
            "vote_invalid.html", {"request": request, "reason": "used"}, status_code=410
        )

    election = db.query(Election).filter(Election.id == invitation.election_id).first()
    now = utcnow()
    if now < election.starts_at or now > election.ends_at:
        db.rollback()
        return templates.TemplateResponse(
            "vote_invalid.html",
            {"request": request, "reason": "window", "election": election},
            status_code=403,
        )
    if choice not in election.options:
        db.rollback()
        return templates.TemplateResponse(
            "vote_invalid.html", {"request": request, "reason": "bad_choice"}, status_code=400
        )

    invitation.used = True
    # Bewusst KEINE Referenz auf invitation.id - Stimme und Einladung bleiben
    # in der Datenbank vollständig getrennt.
    vote = Vote(election_id=election.id, choice=choice)
    db.add(vote)
    db.commit()

    return templates.TemplateResponse("vote_done.html", {"request": request, "election": election})
