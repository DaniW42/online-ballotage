"""Lebenszyklus der Abstimmungen: Abschluss, Ergebnis, Ergebnis-Mail."""
import asyncio
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal, Election, Invitation, Vote, Organization
from .mail import send_result_mail, send_vote_invitation
from .tokens import generate_token
from .timeutil import utcnow, fmt_local

log = logging.getLogger("kugelung")


def deliver_invitation(invitation_id: str, to: str, title: str, link: str,
                       ends_at_str: str, reissued: bool = False) -> None:
    """Versendet eine Einladung (läuft im Hintergrund, daher eigene DB-Session)
    und hält das Ergebnis am Einladungseintrag fest, damit Fehler sichtbar sind."""
    error = None
    try:
        send_vote_invitation(to, title, link, ends_at_str, reissued=reissued)
    except Exception as exc:
        log.exception("Einladungsmail an %s fehlgeschlagen", to)
        error = str(exc)[:200] or exc.__class__.__name__
    db = SessionLocal()
    try:
        inv = db.query(Invitation).filter(Invitation.id == invitation_id).first()
        if inv:
            inv.sent_at = None if error else utcnow()
            inv.send_error = error
            db.commit()
    finally:
        db.close()


def create_invitations(db: Session, election: Election, emails: list[str]) -> list[tuple]:
    """Legt Einladungen für noch nicht eingeladene Adressen an. Gibt die Daten für
    den Mailversand zurück (Roh-Token existiert nur hier und in der Mail)."""
    existing = {
        e for (e,) in db.query(Invitation.email).filter(Invitation.election_id == election.id)
    }
    created = []
    for email in emails:
        if email in existing:
            continue
        raw, token_hash = generate_token()
        inv = Invitation(election_id=election.id, email=email, token_hash=token_hash)
        db.add(inv)
        db.flush()
        created.append((inv.id, email, f"{settings.base_url}/v/{raw}"))
    return created


def reissue_invitation(db: Session, election: Election, invitation_id: str) -> tuple | None:
    """Neuer Link für eine noch nicht genutzte Einladung; der alte wird ungültig.
    Gesperrt, damit es nicht mit einer gleichzeitigen Stimmabgabe kollidiert."""
    inv = (
        db.query(Invitation)
        .filter(Invitation.id == invitation_id, Invitation.election_id == election.id)
        .with_for_update()
        .first()
    )
    if not inv or inv.used:
        db.rollback()
        return None
    raw, token_hash = generate_token()
    inv.token_hash = token_hash
    inv.sent_at = None
    inv.send_error = None
    db.commit()
    return inv.id, inv.email, f"{settings.base_url}/v/{raw}"


def finalize_due(db: Session, election_id: str) -> None:
    """Schließt die Abstimmung ab, sobald Frist vorbei ist oder alle Eingeladenen
    abgestimmt haben. Idempotent; die Zeile wird gesperrt, damit parallele
    Aufrufe (Scheduler, Seitenaufruf) nur einmal abschließen."""
    election = (
        db.query(Election).filter(Election.id == election_id).with_for_update().first()
    )
    if not election or election.status != "open":
        db.rollback()
        return

    now = utcnow()
    total = db.query(func.count(Invitation.id)).filter(
        Invitation.election_id == election.id
    ).scalar()
    used = db.query(func.count(Invitation.id)).filter(
        Invitation.election_id == election.id, Invitation.used.is_(True)
    ).scalar()

    deadline = now > election.ends_at
    all_voted = total > 0 and used == total and now >= election.starts_at
    if not (deadline or all_voted):
        db.rollback()
        return

    election.status = "finished"
    election.finish_reason = "all_voted" if all_voted else "deadline"
    election.finished_at = now
    election.total_invited = total
    election.total_voted = used
    if used < settings.min_voters:
        # Zu wenige Stimmen: kein Ergebnis, Stimmen sofort verwerfen.
        db.query(Vote).filter(Vote.election_id == election.id).delete()
    db.commit()


def abort_election(db: Session, election_id: str) -> bool:
    """Manuelles Beenden = Abbruch: es wird nie ein Ergebnis sichtbar,
    die bereits abgegebenen Stimmen werden gelöscht."""
    finalize_due(db, election_id)
    election = (
        db.query(Election).filter(Election.id == election_id).with_for_update().first()
    )
    if not election or election.status != "open":
        db.rollback()
        return False
    election.status = "aborted"
    election.finish_reason = "aborted"
    election.finished_at = utcnow()
    election.total_invited = db.query(func.count(Invitation.id)).filter(
        Invitation.election_id == election.id).scalar()
    election.total_voted = db.query(func.count(Invitation.id)).filter(
        Invitation.election_id == election.id, Invitation.used.is_(True)).scalar()
    db.query(Vote).filter(Vote.election_id == election.id).delete()
    db.commit()
    return True


def results_available(election: Election) -> bool:
    return (
        election.status == "finished"
        and election.total_voted is not None
        and election.total_voted >= settings.min_voters
    )


def get_results(db: Session, election: Election) -> dict[str, int] | None:
    """Zählung inkl. Optionen mit 0 Stimmen; None, solange kein Ergebnis sichtbar sein darf."""
    if not results_available(election):
        return None
    counts = dict(
        db.query(Vote.choice, func.count(Vote.id))
        .filter(Vote.election_id == election.id)
        .group_by(Vote.choice)
        .all()
    )
    return {option: counts.get(option, 0) for option in election.options}


def send_pending_result_mails(db: Session) -> None:
    pending = (
        db.query(Election)
        .filter(Election.status == "finished", Election.result_mail_sent.is_(False))
        .all()
    )
    for election in pending:
        org = db.query(Organization).filter(Organization.id == election.org_id).first()
        try:
            send_result_mail(
                to=org.email,
                title=election.title,
                reason=election.finish_reason,
                period=f"{fmt_local(election.starts_at)} – {fmt_local(election.ends_at)}",
                total=election.total_invited,
                voted=election.total_voted,
                results=get_results(db, election),
                min_voters=settings.min_voters,
                link=f"{settings.base_url}/elections/{election.id}",
            )
        except Exception:
            log.exception("Ergebnis-Mail für %s fehlgeschlagen, nächster Versuch folgt", election.id)
            continue
        election.result_mail_sent = True
        db.commit()


def run_maintenance() -> None:
    db = SessionLocal()
    try:
        for (election_id,) in db.query(Election.id).filter(Election.status == "open").all():
            finalize_due(db, election_id)
        send_pending_result_mails(db)
    finally:
        db.close()


async def maintenance_loop(interval_seconds: int = 30) -> None:
    """Läuft im App-Prozess (ein uvicorn-Worker). Bei mehreren Workern müsste das
    in einen eigenen Prozess; die Zeilensperren verhindern aber Doppelabschlüsse."""
    while True:
        try:
            await asyncio.to_thread(run_maintenance)
        except Exception:
            log.exception("Wartungslauf fehlgeschlagen")
        await asyncio.sleep(interval_seconds)
