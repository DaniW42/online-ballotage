"""Admin-Portal (/admin) für die konfigurierte ADMIN_EMAIL.

Zugang nur über den normalen Magic-Link-Login mit der Admin-Adresse; eigene Sitzung
(kurze Laufzeit, serverseitig widerrufbar). Das Portal zeigt Konten, Zähler und
Zustände - nie Abstimmungstitel, Teilnehmerlisten, Stimmen oder Ergebnisse.
Gefährliche Aktionen (Sperren, Löschen) brauchen eine eigene Bestätigungsseite,
Löschen zusätzlich die Eingabe des Namens."""
import hashlib
import hmac
import logging
import os
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from . import i18n
from .auth import read_admin_cookie
from .config import settings
from .db import get_db, Organization, Election, Invitation, MagicLink, AuditLog, AdminState
from . import mail as mail_module
from .mail import mail_queue, send_verification_result, PRIORITY_LOGIN
from .services import (
    LAST_MAINTENANCE, audit, decide_verification, get_admin_state, purge_organization,
)
from .timeutil import utcnow
from .tokens import is_valid_email
from .web import render

log = logging.getLogger("kugelung")
router = APIRouter(prefix="/admin")
PAGE_SIZE = 50
VERSION = "1.0.0"


def _admin(request: Request, db: Session) -> AdminState | None:
    request.state.lang = i18n.DEFAULT_LANG  # Admin-Oberfläche immer in der Standardsprache
    version = read_admin_cookie(request)
    if version is None:
        return None
    state = get_admin_state(db)
    return state if state.session_version == version else None


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def org_status(org: Organization) -> str:
    if org.blocked:
        return "blocked"
    if org.confirmed_at is None:
        return "unconfirmed"
    return "verified" if org.verified else "testmode"


def _get_org(db: Session, org_id: str) -> Organization | None:
    return db.query(Organization).filter(Organization.id == org_id).first() if _is_uuid(org_id) else None


def _flash(request: Request) -> str | None:
    value = request.query_params.get("msg")
    return value if value in FLASH_KEYS else None


FLASH_KEYS = {"verified", "unverified", "blocked", "unblocked", "limit_saved", "limit_invalid", "deleted",
              "mail_paused", "mail_resumed", "mail_cleared", "bad_confirmation", "erased", "erased_partial"}


# ---------- Übersicht ----------

def collect_stats(db: Session) -> dict:
    now = utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    orgs = db.query(Organization).all()
    status_counts = {"testmode": 0, "verified": 0, "unconfirmed": 0, "blocked": 0}
    for org in orgs:
        status_counts[org_status(org)] += 1
    weeks = []
    for i in range(7, -1, -1):
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(weeks=i)
        count = db.query(func.count(Organization.id)).filter(
            Organization.created_at >= start, Organization.created_at < start + timedelta(weeks=1)).scalar()
        weeks.append((start, count))
    phases = {"running": 0, "scheduled": 0, "finished": 0, "aborted": 0}
    for status, starts_at in db.query(Election.status, Election.starts_at):
        if status == "open":
            phases["scheduled" if starts_at > now else "running"] += 1
        else:
            phases[status] += 1
    return {
        "orgs": len(orgs), "status_counts": status_counts, "weeks": weeks, "phases": phases,
        "pending_verifications": db.query(func.count(Organization.id)).filter(
            Organization.verification_token_hash.isnot(None)).scalar(),
        "invites_today": db.query(func.count(Invitation.id)).filter(Invitation.sent_at >= today).scalar(),
        "login_mails_today": db.query(func.count(MagicLink.id)).filter(MagicLink.created_at >= today).scalar(),
        "failed_invitations": db.query(func.count(Invitation.id)).filter(
            Invitation.send_error.isnot(None), Invitation.used.is_(False)).scalar(),
        "failed_result_mails": db.query(func.count(Election.id)).filter(Election.result_mail_failed.is_(True)).scalar(),
        "queue_size": mail_queue.size(), "paused": mail_queue.paused,
    }


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    return render(request, "admin_dashboard.html", {"stats": collect_stats(db), "section": "dashboard"})


# ---------- Verifizierungs-Warteschlange ----------

@router.get("/verifications", response_class=HTMLResponse)
def verifications(request: Request, db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    pending = (db.query(Organization).filter(Organization.verification_token_hash.isnot(None))
               .order_by(Organization.verification_requested_at).all())
    return render(request, "admin_verifications.html", {"pending": pending, "section": "verifications"})


# ---------- Logen ----------

@router.get("/orgs", response_class=HTMLResponse)
def orgs(request: Request, q: str = "", status: str = "", page: int = 1, db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    query = db.query(Organization)
    q = q.strip()[:100]
    if q:
        query = query.filter(Organization.name.icontains(q, autoescape=True)
                             | Organization.email.icontains(q, autoescape=True))
    rows = query.order_by(Organization.created_at.desc()).all()
    if status in ("testmode", "verified", "unconfirmed", "blocked"):
        rows = [o for o in rows if org_status(o) == status]
    total = len(rows)
    page = max(1, page)
    rows = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    counts = dict(db.query(Election.org_id, func.count(Election.id)).group_by(Election.org_id).all())
    return render(request, "admin_orgs.html", {
        "orgs": [(o, org_status(o), counts.get(o.id, 0)) for o in rows], "q": q, "status": status,
        "page": page, "pages": max(1, -(-total // PAGE_SIZE)), "total": total, "section": "orgs",
    })


@router.get("/orgs/{org_id}", response_class=HTMLResponse)
def org_detail(org_id: str, request: Request, db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    org = _get_org(db, org_id)
    if not org:
        return RedirectResponse("/admin/orgs", status_code=303)
    by_status = dict(db.query(Election.status, func.count(Election.id))
                     .filter(Election.org_id == org.id).group_by(Election.status).all())
    invitations = db.query(func.count(Invitation.id)).join(Election, Election.id == Invitation.election_id) \
        .filter(Election.org_id == org.id).scalar()
    return render(request, "admin_org.html", {
        "org": org, "status": org_status(org), "by_status": by_status, "invitations": invitations,
        "limit": org.max_recipients or settings.max_recipients, "flash": _flash(request), "section": "orgs",
    })


def _back(org_id: str, msg: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/orgs/{org_id}?msg={msg}", status_code=303)


@router.post("/orgs/{org_id}/verify")
def org_verify(org_id: str, request: Request, db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    org = _get_org(db, org_id)
    if not org:
        return RedirectResponse("/admin/orgs", status_code=303)
    was_pending = bool(org.verification_token_hash)
    name, email = decide_verification(db, org, True)
    audit(db, "verify", f"Loge „{name}“")
    if was_pending:  # Antragsteller informieren (bei direkter Freischaltung ohne Antrag nicht nötig)
        mail_queue.submit(PRIORITY_LOGIN, send_verification_result, email, name, True, i18n.DEFAULT_LANG)
    return _back(org_id, "verified")


@router.post("/orgs/{org_id}/unverify")
def org_unverify(org_id: str, request: Request, db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    org = _get_org(db, org_id)
    if not org:
        return RedirectResponse("/admin/orgs", status_code=303)
    org.verified = False
    db.commit()
    audit(db, "unverify", f"Loge „{org.name}“")
    return _back(org_id, "unverified")


@router.post("/orgs/{org_id}/limit")
def org_limit(org_id: str, request: Request, max_recipients: str = Form(""), db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    org = _get_org(db, org_id)
    if not org:
        return RedirectResponse("/admin/orgs", status_code=303)
    value = max_recipients.strip()
    if value == "":
        org.max_recipients = None
    elif value.isdigit() and 1 <= int(value) <= 100_000:
        org.max_recipients = int(value)
    else:
        return _back(org_id, "limit_invalid")
    db.commit()
    audit(db, "limit", f"Loge „{org.name}“: {org.max_recipients or 'Standard'}")
    return _back(org_id, "limit_saved")


@router.get("/orgs/{org_id}/block", response_class=HTMLResponse)
def block_confirm(org_id: str, request: Request, db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    org = _get_org(db, org_id)
    if not org:
        return RedirectResponse("/admin/orgs", status_code=303)
    return render(request, "admin_confirm.html", {"org": org, "action": "unblock" if org.blocked else "block",
                                                  "typed": False, "section": "orgs"})


@router.post("/orgs/{org_id}/block")
def block_apply(org_id: str, request: Request, action: str = Form(""), db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    org = _get_org(db, org_id)
    if not org or action not in ("block", "unblock"):
        return RedirectResponse("/admin/orgs", status_code=303)
    # Ausdrücklicher Zielzustand statt Umschalten: doppeltes Absenden hebt die Sperre nicht auf
    org.blocked = action == "block"
    if org.blocked:
        org.session_version += 1  # bestehende Sitzungen sofort beenden
    db.commit()
    audit(db, "block" if org.blocked else "unblock", f"Loge „{org.name}“")
    return _back(org_id, "blocked" if org.blocked else "unblocked")


@router.get("/orgs/{org_id}/delete", response_class=HTMLResponse)
def delete_confirm(org_id: str, request: Request, notify: str = "", db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    org = _get_org(db, org_id)
    if not org:
        return RedirectResponse("/admin/orgs", status_code=303)
    elections = db.query(func.count(Election.id)).filter(Election.org_id == org.id).scalar()
    return render(request, "admin_confirm.html", {"org": org, "action": "delete", "typed": True,
                                                  "notify": notify == "1", "elections": elections,
                                                  "bad": request.query_params.get("msg") == "bad_confirmation",
                                                  "section": "orgs"})


@router.post("/orgs/{org_id}/delete")
def delete_apply(org_id: str, request: Request, confirm_name: str = Form(""), notify: str = Form(""),
                 db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    org = _get_org(db, org_id)
    if not org:
        return RedirectResponse("/admin/orgs", status_code=303)
    if " ".join(confirm_name.split()).lower() != " ".join(org.name.split()).lower():
        query = urlencode({"msg": "bad_confirmation", "notify": "1" if notify == "1" else ""})
        return RedirectResponse(f"/admin/orgs/{org_id}/delete?{query}", status_code=303)
    name, email = org.name, org.email
    purge_organization(db, org)
    audit(db, "delete_org", f"Loge „{name}“")
    if notify == "1":
        mail_queue.submit(PRIORITY_LOGIN, send_verification_result, email, name, False, i18n.DEFAULT_LANG)
    return RedirectResponse("/admin/orgs?msg=deleted", status_code=303)


# ---------- Mail ----------

@router.get("/mail", response_class=HTMLResponse)
def mail_page(request: Request, db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    stats = collect_stats(db)
    failed = (db.query(Invitation, Election.org_id)
              .join(Election, Election.id == Invitation.election_id)
              .filter(Invitation.send_error.isnot(None), Invitation.used.is_(False))
              .order_by(Invitation.created_at.desc()).limit(50).all())
    # Nur Zähler je Loge, keine Adressen oder Titel
    per_org: dict[str, int] = {}
    for _, org_id in failed:
        per_org[org_id] = per_org.get(org_id, 0) + 1
    names = {o.id: o.name for o in db.query(Organization).filter(Organization.id.in_(list(per_org)))} if per_org else {}
    return render(request, "admin_mail.html", {
        "stats": stats, "failed_by_org": [(names.get(k, "?"), k, v) for k, v in per_org.items()],
        "flash": _flash(request), "test_result": None, "section": "mail",
        "smtp": {"host": settings.smtp_host, "port": settings.smtp_port,
                 "mode": "SSL" if settings.smtp_ssl else ("STARTTLS" if settings.smtp_use_tls else "unverschlüsselt")},
    })


def _set_pause(db: Session, paused: bool) -> None:
    state = get_admin_state(db)
    state.mail_paused = paused
    db.commit()
    if paused:
        mail_queue.paused = True
    else:
        mail_queue.resume()


@router.post("/mail/pause")
def mail_pause(request: Request, db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    _set_pause(db, True)
    audit(db, "mail_pause")
    return RedirectResponse("/admin/mail?msg=mail_paused", status_code=303)


@router.post("/mail/resume")
def mail_resume(request: Request, db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    _set_pause(db, False)
    audit(db, "mail_resume")
    return RedirectResponse("/admin/mail?msg=mail_resumed", status_code=303)


@router.post("/mail/clear")
def mail_clear(request: Request, db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    dropped = mail_queue.clear_bulk()
    audit(db, "mail_clear", f"{dropped} Mails verworfen")
    return RedirectResponse("/admin/mail?msg=mail_cleared", status_code=303)


@router.post("/mail/test", response_class=HTMLResponse)
def mail_test(request: Request, db: Session = Depends(get_db)):
    """Testmail an die Admin-Adresse - direkt gesendet, damit Fehler (z. B. Zertifikat) sichtbar sind."""
    if not _admin(request, db):
        return _login_redirect()
    try:
        mail_module.send_mail(settings.admin_email, i18n.t(i18n.DEFAULT_LANG, "admin.mail.test_subject"),
                  i18n.t(i18n.DEFAULT_LANG, "admin.mail.test_body"))
        result = {"ok": True, "error": ""}
    except Exception as exc:  # Ursache anzeigen (Zertifikat, Zugangsdaten, Timeout ...)
        result = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"[:300]}
    audit(db, "smtp_test", "ok" if result["ok"] else "fehlgeschlagen")
    stats = collect_stats(db)
    return render(request, "admin_mail.html", {
        "stats": stats, "failed_by_org": [], "flash": None, "test_result": result, "section": "mail",
        "smtp": {"host": settings.smtp_host, "port": settings.smtp_port,
                 "mode": "SSL" if settings.smtp_ssl else ("STARTTLS" if settings.smtp_use_tls else "unverschlüsselt")},
    })


# ---------- System ----------

def config_checks() -> list[tuple[str, bool]]:
    https = settings.base_url.startswith("https://")
    return [
        ("legal", settings.legal_complete),
        ("https", https),
        ("smtp_tls", settings.smtp_ssl or settings.smtp_use_tls),
        ("proxy_ips", os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1") not in ("", "127.0.0.1") or not https),
        ("secret", len(settings.secret_key) >= 32),
        ("repo_url", bool(settings.repo_url)),
    ]


@router.get("/system", response_class=HTMLResponse)
def system(request: Request, db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    try:
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        db.rollback()
        revision = "?"
    last = LAST_MAINTENANCE["at"]
    return render(request, "admin_system.html", {
        "version": VERSION, "revision": revision, "last_maintenance": last,
        "seconds_ago": int((utcnow() - last).total_seconds()) if last else None,
        "checks": config_checks(), "section": "system",
    })


# ---------- Datenschutz (Auskunft / Löschung) ----------

def _lookup(db: Session, email: str) -> dict:
    accounts = db.query(Organization).filter(Organization.email == email).all()
    contacts = db.query(Organization).filter(Organization.contact_email == email).all()
    invitation_rows = (db.query(Election.status, func.count(Invitation.id))
                       .join(Invitation, Invitation.election_id == Election.id)
                       .filter(Invitation.email == email).group_by(Election.status).all())
    errors = db.query(func.count(Invitation.id)).filter(Invitation.email == email,
                                                        Invitation.send_error.isnot(None)).scalar()
    lists = [o for o in db.query(Organization).all() if email in (o.saved_recipients or [])]
    return {"accounts": accounts, "contacts": contacts, "invitations": dict(invitation_rows),
            "lists": lists, "send_errors": errors,
            "found": bool(accounts or contacts or invitation_rows or lists)}


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request, email: str = "", db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    email = email.strip().lower()[:254]
    result = _lookup(db, email) if email and is_valid_email(email) else None
    return render(request, "admin_privacy.html", {
        "email": email, "result": result, "invalid": bool(email) and result is None,
        "flash": _flash(request), "section": "privacy",
    })


@router.post("/privacy/erase")
def privacy_erase(request: Request, email: str = Form(""), confirm_email: str = Form(""),
                  db: Session = Depends(get_db)):
    """Löschung nach Art. 17: Adresse in Einladungen anonymisieren (Zähler bleiben richtig),
    aus gespeicherten Empfängerlisten und Verifizierungs-Angaben entfernen. Ein Login-Konto
    mit dieser Adresse wird nicht automatisch gelöscht (siehe Konto-Seite)."""
    if not _admin(request, db):
        return _login_redirect()
    email = email.strip().lower()
    if not is_valid_email(email) or confirm_email.strip().lower() != email:
        query = urlencode({"email": email, "msg": "bad_confirmation"})
        return RedirectResponse(f"/admin/privacy?{query}", status_code=303)
    skipped = 0
    for invitation, status in (db.query(Invitation, Election.status)
                               .join(Election, Election.id == Invitation.election_id)
                               .filter(Invitation.email == email)):
        if status == "open":
            # Laufende Abstimmung: Umbenennen würde erneutes Einladen (= zweite Stimme)
            # ermöglichen. Erst nach Abschluss löschen.
            skipped += 1
            continue
        invitation.email = f"geloescht-{uuid.uuid4().hex[:12]}@invalid.invalid"
        invitation.send_error = None
    for org in db.query(Organization).all():
        if email in (org.saved_recipients or []):
            org.saved_recipients = [e for e in org.saved_recipients if e != email]
        if org.contact_email == email:
            org.contact_name = org.contact_email = org.contact_website = org.contact_phone = None
    db.commit()
    # Nicht umkehrbarer Schlüssel (HMAC mit SECRET_KEY) statt der Adresse selbst
    digest = hmac.new(settings.secret_key.encode(), email.encode(), hashlib.sha256).hexdigest()[:12]
    audit(db, "erase_email", f"E-Mail-Adresse (Kennung {digest})" + (f", {skipped} in laufenden Abstimmungen offen" if skipped else ""))
    if skipped:
        return RedirectResponse("/admin/privacy?" + urlencode({"msg": "erased_partial", "email": email}), status_code=303)
    return RedirectResponse("/admin/privacy?msg=erased", status_code=303)


# ---------- Protokoll ----------

@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _login_redirect()
    entries = db.query(AuditLog).order_by(AuditLog.at.desc()).limit(200).all()
    return render(request, "admin_audit.html", {"entries": entries, "section": "audit"})
