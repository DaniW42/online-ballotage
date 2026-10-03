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
from .mail import send_magic_link
from .auth import create_session_cookie, read_session, SESSION_COOKIE, SESSION_MAX_AGE, COOKIE_SECURE
from .services import (
    finalize_due, abort_election, get_results, maintenance_loop,
    deliver_invitation, create_invitations, reissue_invitation,
)
from .ratelimit import limiter
from .timeutil import utcnow, local_input_to_utc, fmt_local, to_local

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(maintenance_loop())
    yield
    task.cancel()


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
    return templates.TemplateResponse(
        "new_election.html",
        {"request": request, "org": org, "saved": org.saved_recipients or [], "save_checked": True},
    )


def _queue_invitations(background_tasks: BackgroundTasks, election: Election, created: list[tuple],
                       reissued: bool = False):
    ends = fmt_local(election.ends_at)
    for inv_id, email, link in created:
        background_tasks.add_task(
            deliver_invitation, inv_id, email, election.title, link, ends, reissued
        )


def _form_error(request: Request, org: Organization, message: str, emails: list[str] | None = None):
    return templates.TemplateResponse(
        "new_election.html",
        {"request": request, "org": org, "error": message,
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
    save_recipients: bool = Form(False),
    db: Session = Depends(get_db),
):
    org = current_org(request, db)
    if not org:
        return RedirectResponse("/login")

    # Server-seitig erneut extrahieren/validieren - Client-Liste nie blind
    # übernehmen, unabhängig davon, was das Frontend vorbereitet hat.
    emails = extract_emails(emails_raw)

    # Optionen: Duplikate (case-insensitiv) entfernen, Reihenfolge behalten.
    if len(emails) < settings.min_voters:
        return _form_error(
            request, org,
            f"Mindestens {settings.min_voters} Email-Adressen nötig – bei weniger wäre die Anonymität nicht gewahrt.",
            emails,
        )

    options, seen = [], set()
    for o in (" ".join(o.split()) for o in options_raw.split(",")):
        if o and o.lower() not in seen:
            seen.add(o.lower())
            options.append(o)
    options = options or ["Ja", "Nein", "Enthaltung"]
    if len(options) < 2:
        return _form_error(request, org, "Mindestens zwei Abstimmungsoptionen angeben.", emails)

    title = " ".join(title.split())  # Zeilenumbrüche im Titel (Mail-Betreff) vermeiden
    if settings.require_verification and not org.verified:
        return _form_error(request, org, "Ihre Loge wurde noch nicht freigegeben.", emails)

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

    created = create_invitations(db, election, emails)
    if save_recipients:
        org.saved_recipients = emails
    db.commit()
    _queue_invitations(background_tasks, election, created)

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

    return templates.TemplateResponse(
        "election_detail.html",
        {
            "request": request,
            "org": org,
            "election": election,
            "total": total,
            "used": used,
            "results": get_results(db, election),
            "min_voters": settings.min_voters,
            "invitations": db.query(Invitation)
            .filter(Invitation.election_id == election.id)
            .order_by(Invitation.email)  # alphabetisch, nicht nach Einfügereihenfolge
            .all(),
            "message": request.query_params.get("msg"),
            "added": request.query_params.get("n"),
        },
    )


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
    _queue_invitations(background_tasks, election, [result], reissued=True)
    return RedirectResponse(f"/elections/{election_id}?msg=reissued", status_code=303)


@app.post("/elections/{election_id}/abort")
def election_abort(election_id: str, request: Request, db: Session = Depends(get_db)):
    if not _own_election(db, request, election_id):
        return RedirectResponse("/login", status_code=303)
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
    db.commit()
    return RedirectResponse(f"/elections/{election_id}?msg=extended", status_code=303)


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
    if election.status != "open" or now < election.starts_at or now > election.ends_at:
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

    # Geteilte Sperre: ein parallel laufender Abschluss (FOR UPDATE) wartet,
    # bis diese Stimme committet ist - oder wir sehen schon den Abschluss.
    election = (
        db.query(Election)
        .filter(Election.id == invitation.election_id)
        .with_for_update(read=True)
        .first()
    )
    now = utcnow()
    if election.status != "open" or now < election.starts_at or now > election.ends_at:
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
