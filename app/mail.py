import itertools
import logging
import queue
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage

from .config import settings
from .i18n import t, fmt_datetime

log = logging.getLogger("kugelung")


class MailThrottle:
    """Globale Drosselung: zwischen dem Start zweier Mails liegt mindestens `interval`
    Sekunden - über alle Threads und Mailarten (Login, Einladungen, Ergebnis, ...).
    Wer zu früh kommt, wartet; die Reihenfolge ist dadurch serialisiert."""

    def __init__(self, interval: float, clock=time.monotonic, sleep=time.sleep):
        self.interval = interval
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = self.clock()
            start = max(now, self._next_allowed)
            if start > now:
                self.sleep(start - now)
            self._next_allowed = start + self.interval


throttle = MailThrottle(settings.mail_min_interval_seconds)


def send_mail(to: str, subject: str, body: str) -> None:
    throttle.wait()
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    # Zeilenumbrüche im Betreff wären Header-Injection / werfen im Hintergrundtask.
    msg["Subject"] = " ".join(subject.split())
    msg.set_content(body)

    # Zertifikat und Hostname des Mailservers werden geprüft: Die Mails enthalten
    # Login- und Abstimmungslinks, ein Mitleser könnte sonst in fremdem Namen abstimmen.
    context = ssl.create_default_context()
    timeout = settings.smtp_timeout_seconds
    if settings.smtp_ssl:
        smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout, context=context)
    else:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    with smtp:
        if settings.smtp_use_tls and not settings.smtp_ssl:
            smtp.starttls(context=context)
        # Lokale Testumgebungen (z. B. Mailpit) laufen ohne Auth -
        # Zugangsdaten nur mitschicken, wenn welche gesetzt sind.
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


PRIORITY_LOGIN = 0      # Login-Links und Verifizierung: nie hinter einer großen Einladungsrunde
PRIORITY_BULK = 1       # Einladungen, Erinnerungen


class MailQueue:
    """Ein einziger Hintergrund-Thread verschickt alle Mails (mit Drosselung).
    So blockieren große Einladungsrunden weder Web-Requests noch den Wartungslauf,
    und Login-Links überholen wartende Einladungen."""

    def __init__(self):
        self.queue = queue.PriorityQueue()
        self.synchronous = False  # Tests: sofort ausführen
        self.paused = False       # hält Massenmails (Einladungen/Erinnerungen) an, Login-Mails laufen weiter
        self._held: list = []
        self._seq = itertools.count()
        self._thread = None
        self._lock = threading.Lock()

    def submit(self, priority: int, func, *args) -> None:
        if self.synchronous:
            func(*args)
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="mail-sender", daemon=True)
                self._thread.start()
        self.queue.put((priority, next(self._seq), func, args))

    def size(self) -> int:
        return self.queue.qsize() + len(self._held)

    def resume(self) -> None:
        with self._lock:
            self.paused = False
            held, self._held = self._held, []
        for item in held:
            self.queue.put(item)

    def clear_bulk(self) -> int:
        """Verwirft wartende Massenmails (Login-Mails bleiben). Gibt die Anzahl zurück."""
        with self._lock:
            dropped = len(self._held)
            self._held = []
        keep = []
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            self.queue.task_done()
            if item[0] == PRIORITY_BULK:
                dropped += 1
            else:
                keep.append(item)
        for item in keep:
            self.queue.put(item)
        return dropped

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            priority, _, func, args = item
            if priority == PRIORITY_BULK and self.paused:
                with self._lock:
                    self._held.append(item)
                self.queue.task_done()
                continue
            try:
                func(*args)
            except Exception:
                log.exception("Mailversand fehlgeschlagen")
            finally:
                self.queue.task_done()


mail_queue = MailQueue()


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


def send_admin_login(to: str, link: str, lang: str = "de") -> None:
    send_mail(
        to=to,
        subject=t(lang, "mail.admin_magic.subject"),
        body=t(lang, "mail.admin_magic.body", link=link, minutes=settings.magic_link_ttl_minutes,
               hours=settings.admin_session_hours),
    )


def send_verification_request(to: str, org_name: str, org_email: str, contact_name: str,
                              contact_email: str, website: str, phone: str, link: str,
                              lang: str = "de") -> None:
    send_mail(
        to=to,
        subject=t(lang, "mail.verify_request.subject", org=org_name),
        body=t(lang, "mail.verify_request.body", org=org_name, org_email=org_email,
               contact_name=contact_name, contact_email=contact_email,
               website=website or "-", phone=phone or "-", link=link),
    )


def send_verification_result(to: str, org_name: str, approved: bool, lang: str = "de") -> None:
    key = "approved" if approved else "rejected"
    send_mail(
        to=to,
        subject=t(lang, f"mail.verify_{key}.subject"),
        body=t(lang, f"mail.verify_{key}.body", org=org_name),
    )
