import smtplib
from email.message import EmailMessage

from .config import settings


def send_mail(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    # Zeilenumbrüche im Betreff wären Header-Injection / werfen im Hintergrundtask.
    msg["Subject"] = " ".join(subject.split())
    msg.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        # Lokale Testumgebungen (z. B. Mailpit) laufen ohne Auth -
        # Zugangsdaten nur mitschicken, wenn welche gesetzt sind.
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


def send_magic_link(to: str, link: str) -> None:
    send_mail(
        to=to,
        subject="Ihr Login-Link",
        body=(
            f"Klicken Sie auf den folgenden Link, um sich anzumelden:\n\n{link}\n\n"
            f"Der Link ist {settings.magic_link_ttl_minutes} Minuten gültig und "
            "kann nur einmal verwendet werden."
        ),
    )


def send_vote_invitation(to: str, election_title: str, link: str, ends_at_str: str,
                         kind: str = "invite") -> None:
    if kind == "reissue":
        subject = f"Abstimmung (neuer Link): {election_title}"
        intro = (
            f'Für die Abstimmung "{election_title}" wurde Ihnen ein neuer Link ausgestellt.\n'
            "Ein früher zugesandter Link ist damit ungültig.\n\n"
        )
    elif kind == "reminder":
        subject = f"Erinnerung: {election_title}"
        intro = (
            f'Erinnerung: Sie haben bei der Abstimmung "{election_title}" noch nicht abgestimmt.\n'
            "Dieser Mail liegt ein neuer Link bei; ein früher zugesandter Link ist damit ungültig.\n\n"
        )
    else:
        subject = f"Abstimmung: {election_title}"
        intro = f'Sie sind zur Abstimmung "{election_title}" eingeladen.\n\n'
    send_mail(
        to=to,
        subject=subject,
        body=(
            f"{intro}"
            f"Abstimmen unter folgendem Link (nur einmal gültig):\n{link}\n\n"
            f"Die Abstimmung endet am {ends_at_str}.\n\n"
            "Ihre Stimme wird anonym erfasst. Der Link zeigt kein Ergebnis an."
        ),
    )


def send_result_mail(to: str, title: str, reason: str, period: str, total: int, voted: int,
                     results: dict[str, int] | None, min_voters: int, link: str) -> None:
    reason_text = {
        "deadline": "Die Frist ist abgelaufen.",
        "all_voted": "Alle Eingeladenen haben abgestimmt.",
    }.get(reason, "")
    if results is not None:
        lines = "\n".join(f"  {choice}: {count}" for choice, count in results.items())
        result_block = f"Ergebnis:\n{lines}"
    else:
        result_block = (
            f"Es wurden weniger als {min_voters} Stimmen abgegeben. Aus Gründen der "
            "Anonymität wird kein Ergebnis angezeigt und die Stimmen wurden gelöscht."
        )
    send_mail(
        to=to,
        subject=f"Ergebnis: {title}",
        body=(
            f'Die Abstimmung "{title}" ist beendet. {reason_text}\n\n'
            f"Zeitraum: {period}\n"
            f"Beteiligung: {voted} von {total}\n\n"
            f"{result_block}\n\n"
            "Die Bewertung (angenommen/abgelehnt) obliegt der Loge gemäß ihrem eigenen Ritual.\n"
            f"Diese Mail wird nur einmal versendet. Details: {link}"
        ),
    )
