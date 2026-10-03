import smtplib
from email.message import EmailMessage

from .config import settings
from .i18n import t, fmt_datetime


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


def send_magic_link(to: str, link: str, lang: str) -> None:
    send_mail(
        to=to,
        subject=t(lang, "mail.magic.subject"),
        body=t(lang, "mail.magic.body", link=link, minutes=settings.magic_link_ttl_minutes),
    )


def send_vote_invitation(to: str, election_title: str, link: str, period: str,
                         kind: str = "invite", lang: str = "de") -> None:
    """kind: invite | reissue | reminder"""
    send_mail(
        to=to,
        subject=t(lang, f"mail.{kind}.subject", title=election_title),
        body=t(lang, f"mail.{kind}.intro", title=election_title)
        + t(lang, "mail.vote_body", link=link, period=period),
    )


def send_result_mail(to: str, title: str, reason: str, period: str, total: int, voted: int,
                     results: dict[str, int] | None, min_voters: int, link: str,
                     lang: str = "de") -> None:
    if results is not None:
        lines = "\n".join(f"  {choice}: {count}" for choice, count in results.items())
        result_block = t(lang, "mail.result.results", lines=lines)
    else:
        result_block = t(lang, "mail.result.suppressed", min=min_voters)
    send_mail(
        to=to,
        subject=t(lang, "mail.result.subject", title=title),
        body=t(
            lang, "mail.result.body",
            title=title,
            reason=t(lang, f"mail.result.reason_{reason}"),
            period=period, voted=voted, total=total, block=result_block, link=link,
        ),
    )
