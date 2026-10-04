"""Sicherheits-Regressionstests aus dem Audit."""
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import services
from app.config import Settings, settings
from app.db import Election, Invitation, Vote, Organization
from app.main import app
from app.ratelimit import RateLimiter
from conftest import sql, local_input, make_login

VALID_ENV = dict(database_url="postgresql://x/y", base_url="http://x", smtp_host="h", smtp_user="",
                 smtp_password="", smtp_from="a@b.c")


# ---------- Postgres-Systemspalten (xmin/ctid) ----------

def test_vote_and_invitation_rows_cannot_be_joined_via_xmin(create_election, anon, db):
    election_id, tokens = create_election(emails=[f"v{i:02d}@x.test" for i in range(12)])
    for token in tokens.values():
        assert anon.post(f"/v/{token}", data={"choice": "Ja"}).status_code == 200
    xmins = sql(db, "SELECT xmin::text FROM votes UNION ALL SELECT xmin::text FROM invitations").scalars().all()
    assert len(xmins) == 24 and len(set(xmins)) == 1   # alle Zeilen: dieselbe Transaktions-ID


def test_physical_row_order_does_not_follow_voting_order(create_election, anon, db):
    emails = [f"v{i:02d}@x.test" for i in range(12)]
    election_id, tokens = create_election(emails=emails, receipts_enabled="true")
    codes = []
    for email in emails:                                # Abstimmung in fester Reihenfolge
        html = anon.post(f"/v/{tokens[email]}", data={"choice": "Ja"}).text
        codes.append(re.search(r"<code>([0-9a-f-]{36})</code>", html).group(1))
    by_ctid_votes = sql(db, "SELECT id::text FROM votes ORDER BY ctid").scalars().all()
    by_ctid_invs = sql(db, "SELECT email FROM invitations ORDER BY ctid").scalars().all()
    by_cmin_votes = sql(db, "SELECT id::text FROM votes ORDER BY cmin::text::int").scalars().all()
    assert by_ctid_votes != codes and by_ctid_invs != emails and by_cmin_votes != codes


def test_concurrent_votes_in_same_election_all_count(create_election, db):
    emails = [f"p{i}@x.test" for i in range(8)]
    _, tokens = create_election(emails=emails)

    def vote(email):
        return TestClient(app).post(f"/v/{tokens[email]}", data={"choice": "Ja"}).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(vote, emails))
    assert codes == [200] * 8
    assert db.query(Vote).count() == 8


# ---------- Konfiguration ----------

def test_short_or_missing_secret_key_is_rejected():
    with pytest.raises(ValidationError):
        Settings(secret_key="", **VALID_ENV)
    with pytest.raises(ValidationError):
        Settings(secret_key="kurz", **VALID_ENV)
    assert Settings(secret_key="x" * 32, **VALID_ENV)


def test_base_url_is_normalised():
    assert Settings(secret_key="x" * 32, **{**VALID_ENV, "base_url": "https://a.b/ "}).base_url == "https://a.b"
    with pytest.raises(ValidationError):
        Settings(secret_key="x" * 32, **{**VALID_ENV, "base_url": "a.b"})


# ---------- Web ----------

@pytest.mark.parametrize("target", ["/\\evil.test", "//evil.test", "https://evil.test", "/x\r\nSet-Cookie: a=b"])
def test_language_switch_has_no_open_redirect(anon, target):
    response = anon.get("/lang/de", params={"next": target})
    assert response.headers["location"] == "/"


def test_cross_site_posts_are_blocked(anon, outbox):
    assert anon.post("/login", data={"email": "a@x.test"}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert anon.post("/login", data={"email": "a@x.test"}, headers={"origin": "https://evil.test"}).status_code == 403
    assert anon.post("/login", data={"email": "a@x.test"}, headers={"sec-fetch-site": "same-origin"}).status_code == 200
    assert anon.post("/login", data={"email": "a@x.test"}, headers={"origin": "http://testserver"}).status_code == 200


def test_login_csrf_is_blocked(anon, outbox):
    """Angreifer schiebt sein eigenes Login-Token per Formular unter."""
    anon.post("/register", data={"name": "Angreifer", "email": "evil@x.test"})
    token = re.search(r"/auth/(\S+)", outbox.to("evil@x.test")[-1].body).group(1)
    response = anon.post(f"/auth/{token}", headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403 and "set-cookie" not in response.headers


def test_oversized_requests_are_rejected(anon):
    response = anon.post("/login", content=b"email=" + b"a" * 1_100_000,
                         headers={"content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 413


@pytest.mark.parametrize("path", ["/elections/kaputt", "/elections/kaputt/abort", "/elections/kaputt/delete"])
def test_invalid_ids_do_not_crash(orga, path):
    assert orga.get(path).status_code in (303, 307)


def test_invalid_ids_in_posts_do_not_crash(orga, create_election):
    election_id, _ = create_election()
    for path in ("/elections/kaputt/abort", "/elections/kaputt/delete", "/elections/kaputt/voters"):
        assert orga.post(path, data={"confirm": "yes"}).status_code == 303
    response = orga.post(f"/elections/{election_id}/invitations/kaputt/reissue")
    assert "reissue_failed" in response.headers["location"]


def test_invalid_dates_do_not_crash(orga, create_election, db):
    data = {"title": "T", "starts_at": "morgen", "ends_at": "irgendwann",
            "emails_raw": "a@x.test b@x.test c@x.test", "options_raw": ""}
    assert orga.post("/elections/new", data=data).status_code == 400
    election_id, _ = create_election()
    response = orga.post(f"/elections/{election_id}/schedule", data={"ends_at": "kaputt", "starts_at": "x"})
    assert "schedule_failed" in response.headers["location"]


# ---------- Eingabegrenzen ----------

def _form(**extra):
    data = {"title": "T", "starts_at": local_input(-60), "ends_at": local_input(60),
            "emails_raw": "a@x.test b@x.test c@x.test", "options_raw": ""}
    data.update(extra)
    return data


def test_title_must_be_present_and_short(orga, db):
    assert orga.post("/elections/new", data=_form(title="   ")).status_code == 400
    assert orga.post("/elections/new", data=_form(title="x" * 201)).status_code == 400
    assert db.query(Election).count() == 0


def test_option_limits(orga):
    many = ", ".join(f"Option {i}" for i in range(21))
    assert orga.post("/elections/new", data=_form(options_raw=many)).status_code == 400
    assert orga.post("/elections/new", data=_form(options_raw="a, " + "b" * 101)).status_code == 400


def test_recipient_limit(orga, monkeypatch, create_election, db):
    monkeypatch.setattr(settings, "max_recipients", 4)
    five = " ".join(f"r{i}@x.test" for i in range(5))
    assert orga.post("/elections/new", data=_form(emails_raw=five)).status_code == 400
    election_id, _ = create_election()
    response = orga.post(f"/elections/{election_id}/voters", data={"emails_raw": "d@x.test e@x.test"})
    assert "max_recipients" in response.headers["location"]


def test_registration_name_required(anon):
    assert anon.post("/register", data={"name": "   ", "email": "a@x.test"}).status_code == 400
    assert anon.post("/register", data={"name": "x" * 201, "email": "a@x.test"}).status_code == 400


# ---------- Missbrauch ----------

def test_testmode_daily_invitation_cap(orga, monkeypatch):
    monkeypatch.setattr(settings, "admin_email", "admin@x.test")
    codes = []
    for n in range(4):
        emails = " ".join(f"d{n}{i}@x.test" for i in range(3))
        codes.append(orga.post("/elections/new", data=_form(emails_raw=emails)).status_code)
    assert codes == [303, 303, 303, 400]   # 9 Einladungen ok, die nächsten 3 überschreiten 10/Tag


def test_reissue_is_rate_limited(orga, create_election, db, outbox):
    election_id, _ = create_election()
    inv = db.query(Invitation).filter_by(email="b@x.test").one()
    locations = [orga.post(f"/elections/{election_id}/invitations/{inv.id}/reissue").headers["location"]
                 for _ in range(4)]
    assert ["reissued" in loc for loc in locations] == [True, True, True, False]
    assert "reissue_limited" in locations[-1]


def test_unconfirmed_registrations_are_purged_and_renamable(anon, outbox, db):
    anon.post("/register", data={"name": "Falscher Name", "email": "a@loge.test"})
    anon.post("/register", data={"name": "Echte Loge", "email": "a@loge.test"})
    org = db.query(Organization).one()
    assert org.name == "Echte Loge" and org.confirmed_at is None
    sql(db, "update organizations set created_at = created_at - interval '2 days'")
    services.run_maintenance()
    assert db.query(Organization).count() == 0


def test_confirmed_accounts_keep_name_and_survive_purge(orga, anon, db):
    assert db.query(Organization).one().confirmed_at is not None
    anon.post("/register", data={"name": "Übernahme", "email": "orga@loge.test"})
    sql(db, "update organizations set created_at = created_at - interval '2 days'")
    services.run_maintenance()
    db.expire_all()
    assert db.query(Organization).one().name == "Testloge"


def test_ratelimiter_forgets_old_keys():
    limiter = RateLimiter(window_seconds=0)
    for i in range(2000):
        limiter.allow(f"k{i}", 5)
    assert len(limiter.hits) < 1000


def test_stalled_sends_are_marked(create_election, orga, db):
    election_id, _ = create_election()
    sql(db, "update invitations set sent_at = null, send_queued_at = now() at time zone 'utc' - interval '2 hours' "
            "where email = 'b@x.test'")
    services.run_maintenance()
    db.expire_all()
    assert db.query(Invitation).filter_by(email="b@x.test").one().send_error == services.STALLED_SEND_ERROR
    assert db.query(Invitation).filter_by(email="a@x.test").one().send_error is None


# ---------- Serverseitige Abmeldung ----------

def test_logout_ends_all_sessions_on_all_devices(outbox):
    phone = make_login(outbox)
    laptop = TestClient(app, follow_redirects=False)
    laptop.cookies.set("session", phone.cookies.get("session"))   # dieselbe Anmeldung auf einem zweiten Gerät
    assert phone.get("/dashboard").status_code == 200 and laptop.get("/dashboard").status_code == 200
    phone.post("/logout")
    assert laptop.get("/dashboard").headers["location"] == "/login"   # Cookie ist serverseitig ungültig
    assert phone.get("/dashboard").headers["location"] == "/login"


def test_stolen_cookie_is_useless_after_logout(orga):
    stolen = orga.cookies.get("session")
    orga.post("/logout")
    attacker = TestClient(app, follow_redirects=False)
    attacker.cookies.set("session", stolen)
    assert attacker.get("/dashboard").headers["location"] == "/login"
    assert attacker.post("/elections/new", data={}).status_code in (303, 422)


def test_login_works_again_after_logout(orga, outbox):
    orga.post("/logout")
    orga.post("/login", data={"email": "orga@loge.test"})
    token = re.search(r"/auth/(\S+)", outbox.to("orga@loge.test")[-1].body).group(1)
    assert orga.post(f"/auth/{token}").status_code == 303
    assert orga.get("/dashboard").status_code == 200


def test_old_cookies_without_version_still_work(orga, db):
    from app.auth import serializer
    org = db.query(Organization).one()
    legacy = serializer.dumps({"org_id": org.id})        # Cookie aus der Zeit vor der Versionierung
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("session", legacy)
    assert client.get("/dashboard").status_code == 200


def test_defaults():
    assert settings.max_recipients == 100
