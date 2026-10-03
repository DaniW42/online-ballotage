"""Lebenszyklus der Abstimmungen: Abschluss, Ergebnis, Ergebnis-Mail."""
import asyncio
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal, Election, Invitation, Vote, Organization
from .mail import send_result_mail
from .timeutil import utcnow, fmt_local

log = logging.getLogger("kugelung")


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
