"""Admin-Portal: Login, Sitzungen, Verifizierung, Logen, Mail, Datenschutz, Protokoll."""
import re

import pytest
from fastapi.testclient import TestClient

from app import mail as mail_module
from app import services
from app.auth import SESSION_MAX_AGE, ADMIN_SESSION_MAX_AGE
from app.config import settings
from app.db import Organization, Election, Invitation, Vote, AuditLog, AdminState, MagicLink
from app.main import app
from conftest import sql, local_input, make_login

ADMIN = "admin@ballotage.test"
ADMIN_PAGES = ["/admin", "/admin/verifications", "/admin/orgs", "/admin/mail", "/admin/system",
               "/admin/privacy", "/admin/audit"]


@pytest.fixture(autouse=True)
def admin_on(monkeypatch):
    monkeypatch.setattr(settings, "admin_email", ADMIN)


def admin_login(outbox) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    client.post("/login", data={"email": ADMIN})
    token = re.search(r"/auth/(\S+)", outbox.to(ADMIN)[-1].body).group(1)
    response = client.post(f"/auth/{token}")
    assert response.status_code == 303 and response.headers["location"] == "/admin"
    return client


@pytest.fixture
def admin(outbox):
    return admin_login(outbox)


def _org(db, **filters):
    return db.query(Organization).filter_by(**filters).one()


def _request_verification(client, **extra):
    data = {"contact_name": "Max Meister", "contact_email": "max@loge.test",
            "contact_website": "https://loge.example", "contact_phone": ""}
    data.update(extra)
    return client.post("/verification", data=data)


# ---------- Login und Sitzungen ----------

def test_admin_login_creates_admin_session_only(outbox):
    client = TestClient(app, follow_redirects=False)
    client.post("/login", data={"email": ADMIN})
    mail = outbox.to(ADMIN)[0]
    assert "Admin" in mail.subject and "6 Stunden" in mail.body
    token = re.search(r"/auth/(\S+)", mail.body).group(1)
    response = client.post(f"/auth/{token}")
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1 and cookies[0].startswith("admin_session=")
    assert f"Max-Age={ADMIN_SESSION_MAX_AGE}" in cookies[0] and "samesite=strict" in cookies[0].lower()
    assert ADMIN_SESSION_MAX_AGE == 6 * 3600
    assert client.get("/admin").status_code == 200
    assert client.get("/dashboard").headers["location"] == "/login"   # kein Logen-Konto


def test_normal_sessions_last_ten_days(outbox):
    assert SESSION_MAX_AGE == 10 * 86400
    client = TestClient(app, follow_redirects=False)
    client.post("/register", data={"name": "L", "email": "a@loge.test"})
    token = re.search(r"/auth/(\S+)", outbox.to("a@loge.test")[-1].body).group(1)
    assert f"Max-Age={10 * 86400}" in client.post(f"/auth/{token}").headers["set-cookie"]


def test_admin_address_cannot_register_as_lodge_and_is_not_revealed(anon, outbox, db):
    response = anon.post("/register", data={"name": "Fake", "email": ADMIN})
    normal = anon.post("/register", data={"name": "Echt", "email": "neu@loge.test"})
    assert response.status_code == normal.status_code == 200
    assert "reserviert" not in response.text and "Wir haben eine E-Mail" in response.text
    assert not outbox.to(ADMIN) and db.query(Organization).filter_by(email=ADMIN).count() == 0


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_require_admin_session(anon, orga, path):
    assert anon.get(path).headers["location"] == "/login"
    assert orga.get(path).headers["location"] == "/login"      # normales Logenkonto genügt nicht


def test_admin_posts_require_admin_session(anon, orga, db):
    org_id = db.query(Organization).one().id
    for client in (anon, orga):
        for path in (f"/admin/orgs/{org_id}/verify", f"/admin/orgs/{org_id}/block",
                     f"/admin/orgs/{org_id}/delete", "/admin/mail/pause", "/admin/mail/test",
                     "/admin/privacy/erase"):
            assert client.post(path, data={"email": "a@b.de", "confirm_email": "a@b.de"}).headers["location"] == "/login"
    assert db.query(Organization).one().verified is False


def test_admin_disabled_without_admin_email(outbox, monkeypatch):
    client = admin_login(outbox)
    monkeypatch.setattr(settings, "admin_email", "")
    assert client.get("/admin").headers["location"] == "/login"     # Cookie nutzlos
    anon = TestClient(app, follow_redirects=False)
    anon.post("/login", data={"email": ADMIN})
    assert not outbox.to(ADMIN)[1:]


def test_logout_ends_admin_sessions_everywhere(outbox, admin):
    second = TestClient(app, follow_redirects=False)
    second.cookies.set("admin_session", admin.cookies.get("admin_session"))
    assert second.get("/admin").status_code == 200
    admin.post("/logout")
    assert second.get("/admin").headers["location"] == "/login"
    assert admin.get("/admin").headers["location"] == "/login"


def test_admin_header_link(admin):
    html = admin.get("/").text
    assert 'href="/admin"' in html and 'action="/logout"' in html


def test_admin_cookie_is_not_a_lodge_session_and_vice_versa(admin, orga):
    stolen = TestClient(app, follow_redirects=False)
    stolen.cookies.set("admin_session", orga.cookies.get("session"))   # Logen-Cookie als Admin-Cookie
    assert stolen.get("/admin").headers["location"] == "/login"
    stolen2 = TestClient(app, follow_redirects=False)
    stolen2.cookies.set("session", admin.cookies.get("admin_session"))
    assert stolen2.get("/dashboard").headers["location"] == "/login"


# ---------- Übersicht, keine Abstimmungsinhalte ----------

def test_admin_pages_never_show_election_content(admin, orga, create_election, anon, db):
    election_id, tokens = create_election(title="GEHEIME-AUFNAHME-XYZ")
    anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Ja"})
    org_id = db.query(Organization).one().id
    for path in ADMIN_PAGES + [f"/admin/orgs/{org_id}", "/admin/privacy?email=a@x.test"]:
        html = admin.get(path).text
        assert "GEHEIME-AUFNAHME-XYZ" not in html and "b@x.test" not in html, path
        assert admin.get(path).status_code == 200


def test_dashboard_numbers(admin, orga, create_election):
    create_election()
    html = admin.get("/admin").text
    assert "Testmodus" in html and "Laufende Abstimmungen" in html


# ---------- Verifizierungs-Warteschlange ----------

def test_queue_shows_requests_and_approves(admin, orga, outbox, db):
    _request_verification(orga)
    page = admin.get("/admin/verifications").text
    assert "Testloge" in page and "Max Meister" in page and "https://loge.example" in page
    assert 'href="https://loge.example' not in page                     # nicht klickbar
    assert "offene(r) Verifizierungsantrag" in admin.get("/admin").text
    org = _org(db)
    assert admin.post(f"/admin/orgs/{org.id}/verify").headers["location"].endswith("msg=verified")
    db.expire_all()
    org = _org(db)
    assert org.verified and org.verification_token_hash is None
    assert "verifiziert" in outbox.to("orga@loge.test")[-1].subject
    assert "Keine offenen Anträge" in admin.get("/admin/verifications").text


def test_reject_requires_typed_name_and_deletes_with_mail(admin, orga, outbox, db, create_election):
    create_election()
    _request_verification(orga)
    org = _org(db)
    page = admin.get(f"/admin/orgs/{org.id}/delete?notify=1").text
    assert 'name="confirm_name"' in page and "Betroffene Abstimmungen: 1" in page
    wrong = admin.post(f"/admin/orgs/{org.id}/delete", data={"confirm_name": "falsch", "notify": "1"})
    assert "bad_confirmation" in wrong.headers["location"] and db.query(Organization).count() == 1
    ok = admin.post(f"/admin/orgs/{org.id}/delete", data={"confirm_name": " testloge ", "notify": "1"})
    assert ok.headers["location"].endswith("msg=deleted")
    assert [db.query(m).count() for m in (Organization, Election, Invitation, Vote)] == [0] * 4
    assert db.query(MagicLink).filter(MagicLink.admin.is_(False)).count() == 0
    assert "nicht verifizieren" in outbox.to("orga@loge.test")[-1].body
    assert db.query(AuditLog).filter_by(action="delete_org").count() == 1


def test_request_mail_points_to_portal_which_requires_login(orga, outbox, anon):
    _request_verification(orga)
    body = outbox.to(ADMIN)[-1].body
    assert "/admin/verifications" in body and "/auth/verify/" not in body
    assert anon.get("/admin/verifications").headers["location"] == "/login"


# ---------- Logen ----------

def test_org_list_search_filter_and_detail(admin, orga, outbox, db, create_election):
    make_login(outbox, email="zweite@loge.test", name="Zweite Loge")
    create_election()
    html = admin.get("/admin/orgs").text
    assert "Testloge" in html and "Zweite Loge" in html and "2 Treffer" in html
    assert "Zweite" in admin.get("/admin/orgs?q=zweite").text and "Testloge" not in admin.get("/admin/orgs?q=zweite").text
    db.query(Organization).filter_by(name="Zweite Loge").update({"verified": True}); db.commit()
    assert "Testloge" not in admin.get("/admin/orgs?status=verified").text
    detail = admin.get(f"/admin/orgs/{_org(db, name='Testloge').id}").text
    assert "1 offen/geplant" in detail and "Eingeladene insgesamt" in detail


def test_org_search_treats_wildcards_literally(admin, orga):
    assert "0 Treffer" in admin.get("/admin/orgs?q=%25%25").text


def test_unknown_org_ids_do_not_crash(admin):
    for path in ("/admin/orgs/kaputt", "/admin/orgs/kaputt/block", "/admin/orgs/kaputt/delete",
                 "/admin/orgs/00000000-0000-0000-0000-000000000000"):
        assert admin.get(path).status_code in (200, 303)
    assert admin.post("/admin/orgs/kaputt/verify").status_code == 303


def test_verify_and_unverify_toggle_testmode(admin, orga, db, create_election):
    org = _org(db)
    admin.post(f"/admin/orgs/{org.id}/verify")
    five = " ".join(f"r{i}@x.test" for i in range(5))
    data = {"title": "T", "starts_at": local_input(-60), "ends_at": local_input(60), "emails_raw": five, "options_raw": ""}
    assert orga.post("/elections/new", data=data).status_code == 303
    admin.post(f"/admin/orgs/{org.id}/unverify")
    db.expire_all()
    assert _org(db).verified is False
    assert orga.post("/elections/new", data=data).status_code == 400      # wieder Testmodus


def test_block_requires_confirmation_page_and_ends_sessions(admin, orga, outbox, db):
    org = _org(db)
    assert "Loge sperren?" in admin.get(f"/admin/orgs/{org.id}/block").text
    assert orga.get("/dashboard").status_code == 200
    admin.post(f"/admin/orgs/{org.id}/block", data={"action": "block"})
    assert orga.get("/dashboard").headers["location"] == "/login"        # bestehende Sitzung beendet
    outbox.clear()
    anon = TestClient(app, follow_redirects=False)
    anon.post("/login", data={"email": "orga@loge.test"})
    assert not outbox.to("orga@loge.test")                                # kein neuer Login-Link
    anon.post("/register", data={"name": "X", "email": "orga@loge.test"})
    assert not outbox.to("orga@loge.test")
    admin.post(f"/admin/orgs/{org.id}/block", data={"action": "unblock"})  # entsperren
    anon.post("/login", data={"email": "orga@loge.test"})
    assert outbox.to("orga@loge.test")


def test_blocked_org_gets_no_bulk_mails(admin, orga, outbox, db, create_election):
    election_id, _ = create_election(start=60 * 24, end=60 * 72, reminder_enabled="true")
    org = _org(db)
    admin.post(f"/admin/orgs/{org.id}/block", data={"action": "block"})
    sql(db, "update elections set starts_at = now() at time zone 'utc' - interval '1 minute'")
    outbox.clear()
    services.run_maintenance()
    assert not outbox                                                      # kein Versand zum Beginn
    admin.post(f"/admin/orgs/{org.id}/block", data={"action": "unblock"})   # Sperre aufheben
    services.run_maintenance()
    assert len(outbox.to("a@x.test")) == 1


def test_per_org_recipient_limit(admin, orga, db):
    org = _org(db)
    admin.post(f"/admin/orgs/{org.id}/verify")
    data = lambda n: {"title": "T", "starts_at": local_input(-60), "ends_at": local_input(60),
                      "emails_raw": " ".join(f"r{i}@x.test" for i in range(n)), "options_raw": ""}
    admin.post(f"/admin/orgs/{org.id}/limit", data={"max_recipients": "4"})
    assert orga.post("/elections/new", data=data(5)).status_code == 400
    assert orga.post("/elections/new", data=data(4)).status_code == 303
    admin.post(f"/admin/orgs/{org.id}/limit", data={"max_recipients": "150"})
    assert orga.post("/elections/new", data=data(120)).status_code == 303   # über dem Standard (100)
    assert "limit_invalid" in admin.post(f"/admin/orgs/{org.id}/limit", data={"max_recipients": "abc"}).headers["location"]
    admin.post(f"/admin/orgs/{org.id}/limit", data={"max_recipients": ""})
    db.expire_all()
    assert _org(db).max_recipients is None


# ---------- Mail ----------

def test_mail_pause_resume_clear(admin, db):
    admin.post("/admin/mail/pause")
    assert mail_module.mail_queue.paused and db.query(AdminState).one().mail_paused
    assert "angehalten" in admin.get("/admin/mail").text
    admin.post("/admin/mail/resume")
    db.expire_all()
    assert not mail_module.mail_queue.paused and not db.query(AdminState).one().mail_paused
    assert admin.post("/admin/mail/clear").headers["location"].endswith("mail_cleared")


def test_queue_holds_bulk_but_not_login_mails_while_paused():
    from app.mail import MailQueue, PRIORITY_BULK, PRIORITY_LOGIN
    q, done = MailQueue(), []
    q.paused = True
    q.submit(PRIORITY_BULK, done.append, "einladung")
    q.submit(PRIORITY_LOGIN, done.append, "login")
    q.queue.join()
    assert done == ["login"] and q.size() == 1
    q.resume()
    q.queue.join()
    assert done == ["login", "einladung"]


def test_queue_clear_bulk_keeps_login_mails():
    from app.mail import MailQueue, PRIORITY_BULK, PRIORITY_LOGIN
    q, done = MailQueue(), []
    q.paused = True
    q.submit(PRIORITY_BULK, done.append, "a")
    q.submit(PRIORITY_BULK, done.append, "b")
    q.queue.join()
    assert q.clear_bulk() == 2 and q.size() == 0
    q.submit(PRIORITY_LOGIN, done.append, "login")
    q.queue.join()
    assert done == ["login"]


def test_pause_survives_restart(admin, db):
    admin.post("/admin/mail/pause")
    mail_module.mail_queue.paused = False                    # simulierter Neustart
    mail_module.mail_queue.paused = services.get_admin_state(db).mail_paused
    assert mail_module.mail_queue.paused


def test_smtp_test_mail_success_and_failure(admin, outbox):
    ok = admin.post("/admin/mail/test").text
    assert "angenommen" in ok and "nicht zugestellt" in ok and outbox.to(ADMIN)[-1].subject.startswith("Test-Mail")
    outbox.fail_for = {ADMIN}
    failed = admin.post("/admin/mail/test").text
    assert "fehlgeschlagen" in failed and "SMTPException" in failed


def test_mail_page_lists_failed_counts_per_org_without_addresses(admin, orga, outbox, db):
    outbox.fail_for = {"b@x.test"}
    orga.post("/elections/new", data={"title": "T", "starts_at": local_input(-60), "ends_at": local_input(60),
                                      "emails_raw": "a@x.test b@x.test c@x.test", "options_raw": ""})
    html = admin.get("/admin/mail").text
    assert "Testloge" in html and "b@x.test" not in html


# ---------- System ----------

def test_system_page(admin):
    html = admin.get("/admin/system").text
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    assert f"<code>{head}</code>" in html and "Konfigurations-Check" in html and "10 Tage" in html and "6 Stunden" in html
    services.run_maintenance()
    assert "vor " in admin.get("/admin/system").text


# ---------- Datenschutz ----------

def test_privacy_lookup_and_erase(admin, orga, create_election, db):
    election_id, _ = create_election()
    orga.post("/recipients", data={"emails_raw": "a@x.test, z@x.test"})
    orga.post(f"/elections/{election_id}/abort", data={"confirm": "yes"})   # Abstimmung beendet
    page = admin.get("/admin/privacy?email=A@X.test").text
    assert "1 Einladung(en)" in page and "Gespeicherte Empfängerliste" in page
    assert admin.get("/admin/privacy?email=nirgends@x.test").text.count("keine Daten gespeichert") == 1
    bad = admin.post("/admin/privacy/erase", data={"email": "a@x.test", "confirm_email": "anders@x.test"})
    assert "bad_confirmation" in bad.headers["location"]
    assert db.query(Invitation).filter_by(email="a@x.test").count() == 1
    admin.post("/admin/privacy/erase", data={"email": "a@x.test", "confirm_email": "A@X.test"})
    db.expire_all()
    assert db.query(Invitation).filter_by(email="a@x.test").count() == 0
    assert db.query(Invitation).filter(Invitation.email.like("geloescht-%")).count() == 1
    assert db.query(Invitation).count() == 3                              # Zähler bleiben richtig
    assert _org(db).saved_recipients == ["z@x.test"]
    entry = db.query(AuditLog).filter_by(action="erase_email").one()
    assert "a@x.test" not in (entry.target or "") and "Kennung" in entry.target
    import hashlib
    assert hashlib.sha256(b"a@x.test").hexdigest()[:10] not in entry.target  # nicht per Liste umkehrbar


def test_erase_clears_verification_contact_data(admin, orga, db):
    _request_verification(orga)
    admin.post("/admin/privacy/erase", data={"email": "max@loge.test", "confirm_email": "max@loge.test"})
    db.expire_all()
    org = _org(db)
    assert org.contact_email is None and org.contact_name is None and org.contact_website is None


def test_privacy_rejects_invalid_address(admin):
    assert "gültige E-Mail-Adresse" in admin.get("/admin/privacy?email=kein-email").text


# ---------- Protokoll ----------

def test_audit_log_records_actions_without_email_addresses(admin, orga, db):
    org = _org(db)
    admin.post(f"/admin/orgs/{org.id}/verify")
    admin.post(f"/admin/orgs/{org.id}/block", data={"action": "block"})
    admin.post("/admin/mail/pause")
    page = admin.get("/admin/audit").text
    for label in ("Admin-Anmeldung", "Loge verifiziert", "Loge gesperrt", "Massenmails angehalten"):
        assert label in page
    assert "orga@loge.test" not in page and "Testloge" in page


def test_audit_log_is_purged_after_a_year(admin, db):
    admin.post("/admin/mail/pause")
    sql(db, "update audit_log set at = at - interval '400 days'")
    services.run_maintenance()
    assert db.query(AuditLog).count() == 0


def test_changing_admin_email_invalidates_sessions_and_links(admin, outbox, db, monkeypatch):
    services.sync_admin_identity(db)                       # Stand für ADMIN festhalten
    anon = TestClient(app, follow_redirects=False)
    anon.post("/login", data={"email": ADMIN})              # offener Link an die alte Adresse
    old_link = re.search(r"/auth/(\S+)", outbox.to(ADMIN)[-1].body).group(1)
    monkeypatch.setattr(settings, "admin_email", "neu@ballotage.test")
    services.sync_admin_identity(db)                       # simulierter Neustart mit neuer Adresse
    assert admin.get("/admin").headers["location"] == "/login"
    assert "error=expired" in anon.post(f"/auth/{old_link}").headers["location"]


# ---------- Audit 2 ----------

def test_block_is_explicit_not_a_toggle(admin, orga, db):
    org = _org(db)
    admin.post(f"/admin/orgs/{org.id}/block", data={"action": "block"})
    admin.post(f"/admin/orgs/{org.id}/block", data={"action": "block"})       # doppelt abgeschickt
    db.expire_all()
    assert _org(db).blocked is True
    assert admin.post(f"/admin/orgs/{org.id}/block").headers["location"] == "/admin/orgs"   # ohne Ziel: nichts


def test_queued_mails_are_not_sent_after_block_or_erase(admin, orga, db, outbox, create_election):
    from app.services import deliver_invitation
    election_id, _ = create_election()
    inv = db.query(Invitation).filter_by(email="b@x.test").one()
    outbox.clear()
    admin.post(f"/admin/orgs/{_org(db).id}/block", data={"action": "block"})
    deliver_invitation(inv.id, "b@x.test", "T", "http://x/v/abc", "p")       # Job aus der Warteschlange
    assert not outbox
    admin.post(f"/admin/orgs/{_org(db).id}/block", data={"action": "unblock"})
    deliver_invitation(inv.id, "anders@x.test", "T", "http://x/v/abc", "p")  # Adresse inzwischen geändert
    assert not outbox
    deliver_invitation("00000000-0000-0000-0000-000000000000", "b@x.test", "T", "l", "p")  # Einladung gelöscht
    assert not outbox


def test_send_errors_do_not_store_addresses(orga, db, outbox):
    import smtplib

    def refuse(to, subject, body):
        raise smtplib.SMTPRecipientsRefused({to: (550, b"no such user")})
    from app import mail as m
    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(m, "send_mail", refuse)
    try:
        orga.post("/elections/new", data={"title": "T", "starts_at": local_input(-60), "ends_at": local_input(60),
                                          "emails_raw": "a@x.test b@x.test c@x.test", "options_raw": ""})
    finally:
        mp.undo()
    errors = [i.send_error for i in db.query(Invitation)]
    assert all(e for e in errors) and not any("@x.test" in e for e in errors)


def test_erase_skips_running_elections_to_prevent_double_votes(admin, orga, create_election, db, anon):
    election_id, tokens = create_election()
    anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Ja"})
    response = admin.post("/admin/privacy/erase", data={"email": "a@x.test", "confirm_email": "a@x.test"})
    assert "erased_partial" in response.headers["location"]
    assert db.query(Invitation).filter_by(email="a@x.test").count() == 1      # bleibt bis zum Abschluss
    # erneutes Einladen derselben Adresse bleibt damit ausgeschlossen
    assert "n=0" in orga.post(f"/elections/{election_id}/voters", data={"emails_raw": "a@x.test"}).headers["location"]


def test_dispatch_and_reminders_wait_while_mail_is_paused(admin, orga, db, outbox, create_election):
    create_election(start=60 * 24, end=60 * 72)
    admin.post("/admin/mail/pause")
    sql(db, "update elections set starts_at = now() at time zone 'utc' - interval '1 minute'")
    outbox.clear()
    services.run_maintenance()
    db.expire_all()
    assert not outbox and db.query(Election).one().invitations_dispatched is False
    admin.post("/admin/mail/resume")
    services.run_maintenance()
    assert len(outbox.to("a@x.test")) == 1


def test_blocked_lodges_data_is_still_purged(admin, orga, create_election, anon, db, outbox):
    election_id, tokens = create_election()
    for e in ("a@x.test", "b@x.test", "c@x.test"):
        anon.post(f"/v/{tokens[e]}", data={"choice": "Ja"})
    admin.post(f"/admin/orgs/{_org(db).id}/block", data={"action": "block"})
    services.run_maintenance()                                              # keine Ergebnis-Mail (gesperrt)
    sql(db, "update elections set finished_at = finished_at - interval '40 days'")
    services.run_maintenance()
    assert db.query(Invitation).count() == 0


def test_blocked_unconfirmed_accounts_are_not_purged(admin, anon, db, outbox):
    anon.post("/register", data={"name": "Spam", "email": "spam@x.test"})
    org = _org(db, email="spam@x.test")
    admin.post(f"/admin/orgs/{org.id}/block", data={"action": "block"})
    sql(db, "update organizations set created_at = created_at - interval '3 days'")
    services.run_maintenance()
    assert db.query(Organization).filter_by(email="spam@x.test").count() == 1


def test_saved_list_respects_per_lodge_limit(admin, orga, db):
    org = _org(db)
    admin.post(f"/admin/orgs/{org.id}/limit", data={"max_recipients": "150"})
    orga.post("/recipients", data={"emails_raw": " ".join(f"r{i}@x.test" for i in range(120))})
    db.expire_all()
    assert len(_org(db).saved_recipients) == 120


def test_admin_login_not_blocked_by_global_address_cap(outbox, monkeypatch):
    import app.main as main
    for i in range(6):                                                      # viele IPs eines Angreifers
        monkeypatch.setattr(main, "_client_ip", lambda request, i=i: f"6.6.6.{i}")
        for _ in range(5):
            TestClient(app).post("/login", data={"email": ADMIN})
    before = len(outbox.to(ADMIN))
    monkeypatch.setattr(main, "_client_ip", lambda request: "1.2.3.4")
    TestClient(app).post("/login", data={"email": ADMIN})
    assert len(outbox.to(ADMIN)) == before + 1
