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


def send_vote_invitation(to: str, election_title: str, link: str, ends_at_str: str) -> None:
    send_mail(
        to=to,
        subject=f"Abstimmung: {election_title}",
        body=(
            f'Sie sind zur Abstimmung "{election_title}" eingeladen.\n\n'
            f"Abstimmen unter folgendem Link (nur einmal gültig):\n{link}\n\n"
            f"Die Abstimmung endet am {ends_at_str}.\n\n"
            "Ihre Stimme wird anonym erfasst. Der Link zeigt kein Ergebnis an."
        ),
    )
