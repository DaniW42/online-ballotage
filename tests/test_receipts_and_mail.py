"""Nachprüfbarkeit für Wähler, Mail-Transport (TLS) und Mail-Warteschlange."""
import re
import smtplib
import ssl
import threading

from app import mail, services
from app.mail import MailQueue, PRIORITY_BULK, PRIORITY_LOGIN
from conftest import ORIGINAL_SEND_MAIL


def _finish_with_receipts(create_election, anon, receipts="true"):
    extra = {"receipts_enabled": receipts} if receipts else {}
    election_id, tokens = create_election(emails=("a@x.test", "b@x.test", "c@x.test", "d@x.test"), **extra)
    codes = {}
    for email, choice in (("a@x.test", "Ja"), ("b@x.test", "Nein"), ("c@x.test", "Ja")):
        html = anon.post(f"/v/{tokens[email]}", data={"choice": choice}).text
        found = re.search(r"<code>([0-9a-f-]{36})</code>", html)
        codes[email] = found.group(1) if found else None
    return election_id, tokens, codes


def test_voter_can_verify_own_receipt_via_link_after_finish(create_election, anon, db):
    election_id, tokens, codes = _finish_with_receipts(create_election, anon)
    assert "Alle Stimmen" not in anon.get(f"/v/{tokens['a@x.test']}").text  # vor dem Ende nichts
    from conftest import sql
    sql(db, "update elections set ends_at = now() at time zone 'utc' - interval '1 minute'")
    services.run_maintenance()
    page = anon.get(f"/v/{tokens['a@x.test']}").text
    assert codes["a@x.test"] in page and codes["b@x.test"] in page
    assert re.search(r"<td>Ja</td><td>2</td>", page)
    listed = re.findall(r"<code>([0-9a-f-]{36})</code>", page)
    assert listed == sorted(listed)                      # zufällige Reihenfolge (nach ID)
    # auch Eingeladene, die nicht abgestimmt haben, können die Zählung prüfen
    assert codes["a@x.test"] in anon.get(f"/v/{tokens['d@x.test']}").text


def test_no_voter_list_without_receipts(create_election, anon, db):
    election_id, tokens, _ = _finish_with_receipts(create_election, anon, receipts=None)
    from conftest import sql
    sql(db, "update elections set ends_at = now() at time zone 'utc' - interval '1 minute'")
    services.run_maintenance()
    page = anon.get(f"/v/{tokens['a@x.test']}").text
    assert "Alle Stimmen" not in page and "<td>Ja</td>" not in page


class RecordingSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.timeout, self.context, self.tls_context = timeout, context, None
        RecordingSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.tls_context = context

    def login(self, *a):
        pass

    def send_message(self, msg):
        pass


def _verifying(context):
    return context is not None and context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_starttls_verifies_certificate_and_uses_timeout(monkeypatch):
    RecordingSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", RecordingSMTP)
    monkeypatch.setattr(mail.settings, "smtp_use_tls", True)
    monkeypatch.setattr(mail.settings, "smtp_ssl", False)
    monkeypatch.setattr(mail.throttle, "interval", 0)
    ORIGINAL_SEND_MAIL("a@x.test", "S", "B")
    smtp = RecordingSMTP.instances[-1]
    assert smtp.timeout and _verifying(smtp.tls_context)


def test_implicit_tls_verifies_certificate(monkeypatch):
    RecordingSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", RecordingSMTP)
    monkeypatch.setattr(mail.settings, "smtp_ssl", True)
    monkeypatch.setattr(mail.throttle, "interval", 0)
    ORIGINAL_SEND_MAIL("a@x.test", "S", "B")
    assert _verifying(RecordingSMTP.instances[-1].context)


def test_mail_queue_sends_login_mails_before_waiting_bulk_mails():
    q = MailQueue()
    order, release, started = [], threading.Event(), threading.Event()
    q.submit(PRIORITY_BULK, lambda: (started.set(), release.wait(5), order.append("erste")))
    started.wait(5)  # Sender-Thread steckt im ersten Versand
    q.submit(PRIORITY_BULK, order.append, "einladung-1")
    q.submit(PRIORITY_BULK, order.append, "einladung-2")
    q.submit(PRIORITY_LOGIN, order.append, "login")
    release.set()
    q.queue.join()
    assert order == ["erste", "login", "einladung-1", "einladung-2"]


def test_mail_queue_survives_failing_job():
    q = MailQueue()
    done = []
    q.submit(PRIORITY_BULK, lambda: 1 / 0)
    q.submit(PRIORITY_BULK, done.append, "weiter")
    q.queue.join()
    assert done == ["weiter"]


def test_targeted_login_lockout_is_not_possible(anon, outbox, monkeypatch):
    import app.main as main
    anon.post("/register", data={"name": "L", "email": "opfer@loge.test"})
    outbox.clear()
    monkeypatch.setattr(main, "_client_ip", lambda request: "6.6.6.6")      # Angreifer
    for _ in range(10):
        anon.post("/login", data={"email": "opfer@loge.test"})
    attacker_mails = len(outbox.to("opfer@loge.test"))
    monkeypatch.setattr(main, "_client_ip", lambda request: "1.2.3.4")      # das Opfer selbst
    anon.post("/login", data={"email": "opfer@loge.test"})
    assert attacker_mails == 5 and len(outbox.to("opfer@loge.test")) == 6
