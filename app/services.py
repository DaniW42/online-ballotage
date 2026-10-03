"""Lebenszyklus der Abstimmungen: Abschluss, Ergebnis, Ergebnis-Mail."""
import asyncio
import logging
import secrets

from datetime import timedelta

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal, Election, Invitation, Vote, Organization, MagicLink
from .mail import send_result_mail, send_vote_invitation, mail_queue, PRIORITY_BULK
from .tokens import generate_token
from .i18n import fmt_datetime
from .timeutil import utcnow

log = logging.getLogger("kugelung")


def scramble_row_versions(db: Session, election_id: str) -> None:
    """Verwischt Spuren, die Postgres selbst an jeder Zeile hinterlässt.

    Stimme und "hat abgestimmt"-Markierung werden in derselben Transaktion geschrieben.
    Ohne Gegenmaßnahme tragen beide Zeilen dieselbe Transaktions-ID (Systemspalte
    `xmin`) und liegen in derselben Reihenfolge im Speicher (`ctid`) - ein einfacher
    SQL-Join würde Person und Stimme verbinden. Deshalb wird hier JEDE Stimme und JEDE
    Einladung dieser Abstimmung in zufälliger Reihenfolge, einzeln, neu geschrieben:
    Danach tragen alle Zeilen dieselbe Transaktions-ID, und ihre physische Reihenfolge
    und Befehlsnummer (`cmin`) sind zufällig. Muss in der Transaktion der Stimmabgabe
    laufen, bei gesperrter Abstimmungszeile (Stimmen sind dadurch serialisiert)."""
    rows = [("votes", "choice", vid) for (vid,) in
            db.query(Vote.id).filter(Vote.election_id == election_id)]
    rows += [("invitations", "used", iid) for (iid,) in
             db.query(Invitation.id).filter(Invitation.election_id == election_id)]
    secrets.SystemRandom().shuffle(rows)
    for table, column, row_id in rows:
        db.execute(text(f"UPDATE {table} SET {column} = {column} WHERE id = :id"), {"id": row_id})


def period_text(election: Election) -> str:
    return (f"{fmt_datetime(election.language, election.starts_at)} – "
            f"{fmt_datetime(election.language, election.ends_at)}")


def deliver_invitation(invitation_id: str, to: str, title: str, link: str,
                       period: str, kind: str = "invite", lang: str = "de") -> None:
    """Versendet eine Einladung (läuft im Hintergrund, daher eigene DB-Session)
    und hält das Ergebnis am Einladungseintrag fest, damit Fehler sichtbar sind."""
    error = None
    try:
        send_vote_invitation(to, title, link, period, kind=kind, lang=lang)
    except Exception as exc:
        log.exception("Einladungsmail fehlgeschlagen (Einladung %s)", invitation_id)
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


def create_invitations(db: Session, election: Election, emails: list[str],
                       queued: bool = True) -> list[tuple]:
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
        inv = Invitation(election_id=election.id, email=email, token_hash=token_hash,
                         send_queued_at=utcnow() if queued else None)
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
    inv.send_queued_at = utcnow()
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


def invitations_last_24h(db: Session, org_id: str) -> int:
    since = utcnow() - timedelta(hours=24)
    return (
        db.query(func.count(Invitation.id))
        .join(Election, Election.id == Invitation.election_id)
        .filter(Election.org_id == org_id, Invitation.created_at >= since)
        .scalar()
    )


def is_verified(org: Organization) -> bool:
    """Ohne ADMIN_EMAIL ist die Verifizierung ausgeschaltet: alle gelten als verifiziert."""
    return (not settings.admin_email) or org.verified


def purge_organization(db: Session, org: Organization) -> None:
    """Löscht eine Loge samt allen Abstimmungen, Stimmen und Einladungen (Ablehnung)."""
    for (election_id,) in db.query(Election.id).filter(Election.org_id == org.id).all():
        # Sperrreihenfolge wie bei der Stimmabgabe: erst Abstimmung, dann Einladungen
        db.query(Election).filter(Election.id == election_id).with_for_update().first()
        db.query(Vote).filter(Vote.election_id == election_id).delete()
        db.query(Invitation).filter(Invitation.election_id == election_id).delete()
    db.query(Election).filter(Election.org_id == org.id).delete()
    db.query(MagicLink).filter(MagicLink.org_id == org.id).delete()
    db.delete(org)
    db.commit()


def delete_election(db: Session, election_id: str) -> str | None:
    """Löscht eine Abstimmung samt Stimmen und Einladungen. Läuft sie noch, wird sie vorher
    abgebrochen (kein Ergebnis). Gibt einen Fehlerschlüssel zurück, wenn es (noch) nicht geht."""
    finalize_due(db, election_id)
    election = db.query(Election).filter(Election.id == election_id).first()
    if not election:
        return "missing"
    if election.status == "open":
        abort_election(db, election_id)
        db.refresh(election)
    if election.status == "finished" and not result_mail_settled(election):
        return "mail_pending"  # das Ergebnis wurde dem Organisator noch nicht zugestellt
    db.query(Vote).filter(Vote.election_id == election.id).delete()
    db.query(Invitation).filter(Invitation.election_id == election.id).delete()
    db.delete(election)
    db.commit()
    return None


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


RESULT_MAIL_MAX_ATTEMPTS = 12   # 30 s, 1 min, 2 min, ... -> gibt nach gut einem Tag auf


def result_mail_settled(election: Election) -> bool:
    return election.result_mail_sent or election.result_mail_failed


def send_pending_result_mails(db: Session) -> None:
    now = utcnow()
    pending = (
        db.query(Election)
        .filter(Election.status == "finished", Election.result_mail_sent.is_(False),
                Election.result_mail_failed.is_(False))
        .all()
    )
    for election in pending:
        attempts = election.result_mail_attempts or 0
        next_try = election.finished_at + timedelta(seconds=30 * (2 ** attempts - 1))
        if now < next_try:
            continue
        org = db.query(Organization).filter(Organization.id == election.org_id).first()
        try:
            send_result_mail(
                to=org.email,
                title=election.title,
                reason=election.finish_reason,
                period=period_text(election),
                total=election.total_invited,
                voted=election.total_voted,
                results=get_results(db, election),
                min_voters=settings.min_voters,
                link=f"{settings.base_url}/elections/{election.id}",
                lang=election.language,
            )
        except Exception:
            election.result_mail_attempts = attempts + 1
            election.result_mail_failed = election.result_mail_attempts >= RESULT_MAIL_MAX_ATTEMPTS
            db.commit()
            log.exception("Ergebnis-Mail für %s fehlgeschlagen (Versuch %s)", election.id, attempts + 1)
            continue
        election.result_mail_sent = True
        db.commit()


def send_due_reminders(db: Session) -> None:
    """Erinnert Nicht-Abgestimmte kurz vor Fristende. Der alte Link kann nicht
    erneut verschickt werden (nur sein Hash ist gespeichert), daher bekommt
    jede Erinnerung einen frisch ausgestellten Link; der alte wird ungültig."""
    now = utcnow()
    due = (
        db.query(Election)
        .filter(Election.status == "open", Election.reminder_enabled.is_(True),
                Election.reminder_sent.is_(False))
        .all()
    )
    for election in due:
        if not election.invitations_dispatched:
            continue  # noch gar nicht eingeladen
        reminder_at = election.ends_at - timedelta(hours=settings.reminder_hours_before)
        if now < reminder_at or now >= election.ends_at:
            continue
        election.reminder_sent = True  # auch bei sehr kurzen Abstimmungen nur einmal prüfen
        db.commit()
        if reminder_at <= max(election.created_at, election.starts_at) + timedelta(hours=1):
            continue  # Abstimmung zu kurz: Erinnerung direkt nach der Einladung wäre Spam
        pending = [
            i for (i,) in db.query(Invitation.id).filter(
                Invitation.election_id == election.id, Invitation.used.is_(False)
            )
        ]
        for invitation_id in pending:
            result = reissue_invitation(db, election, invitation_id)
            if result:
                inv_id, email, link = result
                mail_queue.submit(PRIORITY_BULK, deliver_invitation, inv_id, email, election.title,
                                  link, period_text(election), "reminder", election.language)


def dispatch_due_invitations(db: Session) -> None:
    """Geplante Abstimmungen: Einladungen erst zum Beginn versenden. Die Tokens werden
    erst jetzt erzeugt (beim Anlegen wurde nur ein Platzhalter gespeichert)."""
    now = utcnow()
    due = (
        db.query(Election)
        .filter(Election.status == "open", Election.invitations_dispatched.is_(False),
                Election.starts_at <= now)
        .all()
    )
    for election in due:
        # Unter Sperre Flag setzen und Empfänger einsammeln: Ein gleichzeitiges Nachladen
        # (sperrt ebenfalls die Abstimmung) landet so entweder hier oder verschickt selbst.
        locked = db.query(Election).filter(Election.id == election.id).with_for_update().first()
        if locked.invitations_dispatched:
            db.rollback()
            continue
        locked.invitations_dispatched = True  # nie doppelt versenden
        pending = [
            i for (i,) in db.query(Invitation.id)
            .filter(Invitation.election_id == election.id, Invitation.used.is_(False))
            .order_by(Invitation.email)
        ]
        db.commit()
        for invitation_id in pending:
            result = reissue_invitation(db, election, invitation_id)
            if result:
                inv_id, email, link = result
                mail_queue.submit(PRIORITY_BULK, deliver_invitation, inv_id, email, election.title,
                                  link, period_text(election), "invite", election.language)


def purge_expired(db: Session) -> None:
    """Datensparsamkeit: Einladungen (Emails) und alte Login-Links löschen.
    Zähler stehen seit dem Abschluss an der Abstimmung; Stimmen bleiben erhalten."""
    now = utcnow()
    cutoff = now - timedelta(days=settings.retention_days)
    old = (
        db.query(Election)
        .filter(Election.status.in_(["finished", "aborted"]), Election.purged.is_(False),
                Election.finished_at < cutoff)
        .all()
    )
    for election in old:
        if election.status == "finished" and not result_mail_settled(election):
            continue  # Ergebnis-Mail zuerst loswerden (oder aufgeben)
        db.query(Invitation).filter(Invitation.election_id == election.id).delete()
        election.purged = True
        db.commit()
    db.query(MagicLink).filter(MagicLink.expires_at < now - timedelta(days=1)).delete()
    db.commit()
    # Nie bestätigte Registrierungen (Login-Link nie benutzt) nach 24 h löschen
    stale = (
        db.query(Organization)
        .filter(Organization.confirmed_at.is_(None),
                Organization.created_at < now - timedelta(hours=24))
        .all()
    )
    for org in stale:
        if not db.query(Election.id).filter(Election.org_id == org.id).first():
            db.query(MagicLink).filter(MagicLink.org_id == org.id).delete()
            db.delete(org)
    db.commit()


STALLED_SEND_MINUTES = 120
STALLED_SEND_ERROR = "Versand unterbrochen (z. B. Neustart) – bitte „Neuer Link“ verwenden"


def mark_stalled_sends(db: Session) -> None:
    """Einladungen, deren Versand angestoßen, aber nie abgeschlossen wurde (Prozess
    neu gestartet), sichtbar als Fehler markieren. Der Link selbst ist verloren
    (nur der Hash ist gespeichert), daher hilft nur ein neuer Link."""
    cutoff = utcnow() - timedelta(minutes=STALLED_SEND_MINUTES)
    stalled = (Invitation.send_queued_at < cutoff, Invitation.sent_at.is_(None),
               Invitation.send_error.is_(None), Invitation.used.is_(False))
    election_ids = {e for (e,) in db.query(Invitation.election_id).filter(*stalled).distinct()}
    for election_id in election_ids:
        # pro Abstimmung, Abstimmung zuerst sperren (gleiche Reihenfolge wie Stimmabgabe)
        db.query(Election).filter(Election.id == election_id).with_for_update().first()
        (
            db.query(Invitation)
            .filter(Invitation.election_id == election_id, *stalled)
            .update({"send_error": STALLED_SEND_ERROR}, synchronize_session=False)
        )
        db.commit()


def _finalize_all_due(db: Session) -> None:
    for (election_id,) in db.query(Election.id).filter(Election.status == "open").all():
        finalize_due(db, election_id)


MAINTENANCE_STEPS = (_finalize_all_due, dispatch_due_invitations, send_pending_result_mails,
                     send_due_reminders, purge_expired, mark_stalled_sends)


def run_maintenance() -> None:
    """Jeder Schritt läuft für sich: Ein Fehler (z. B. Mailserver weg) blockiert nicht
    die übrigen Aufgaben wie Abschluss oder Löschfristen."""
    db = SessionLocal()
    try:
        for step in MAINTENANCE_STEPS:
            try:
                step(db)
            except Exception:
                log.exception("Wartungsschritt %s fehlgeschlagen", step.__name__)
                db.rollback()
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
