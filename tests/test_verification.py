"""Verifizierung von Logen: Testmodus, Antrag, Freigabe/Ablehnung durch den Admin."""
import re

import pytest

from app.config import settings
from app.db import Organization, Election, Invitation, Vote, MagicLink
from conftest import local_input, make_login

ADMIN = "admin@ballotage.test"
REQUEST = {"contact_name": "Max Meister", "contact_email": "max@loge.test",
           "contact_website": "https://loge.example", "contact_phone": ""}


@pytest.fixture
def verification_on(monkeypatch):
    monkeypatch.setattr(settings, "admin_email", ADMIN)


def _election_form(emails, **extra):
    data = {"title": "T", "starts_at": local_input(-60), "ends_at": local_input(60),
            "emails_raw": "\n".join(emails), "options_raw": ""}
    data.update(extra)
    return data


def _five():
    return [f"v{i}@x.test" for i in range(5)]


def _admin_link(outbox):
    mail = outbox.to(ADMIN)[-1]
    return re.search(r"(/auth/verify/[\w-]+)", mail.body).group(1)


def _request_verification(orga, **override):
    return orga.post("/verification", data={**REQUEST, **override})


# ---------- ohne ADMIN_EMAIL ist alles frei ----------

def test_verification_disabled_by_default(orga, db):
    assert orga.post("/elections/new", data=_election_form(_five())).status_code == 303
    page = orga.get("/verification").text
    assert "keine Verifizierung nötig" in page
    assert "TESTMODUS" not in orga.get("/dashboard").text
    assert _request_verification(orga).headers["location"] == "/verification"
    assert db.query(Organization).one().verification_requested_at is None


# ---------- Testmodus ----------

def test_unverified_org_limited_to_three_recipients(verification_on, orga, db):
    response = orga.post("/elections/new", data=_election_form(_five()))
    assert response.status_code == 400 and "Testmodus" in response.text
    assert db.query(Election).count() == 0
    assert orga.post("/elections/new", data=_election_form(_five()[:3])).status_code == 303


def test_testmode_is_displayed(verification_on, orga, create_election):
    election_id, _ = create_election()
    for page in (orga.get("/dashboard").text, orga.get("/elections/new").text,
                 orga.get(f"/elections/{election_id}").text):
        assert "TESTMODUS" in page and 'href="/verification"' in page


def test_testmode_blocks_adding_voters_beyond_limit(verification_on, orga, create_election, db):
    election_id, _ = create_election()
    response = orga.post(f"/elections/{election_id}/voters", data={"emails_raw": "d@x.test"})
    assert "testmode_limit" in response.headers["location"]
    assert db.query(Invitation).count() == 3


def test_verified_org_has_no_limit_and_no_banner(verification_on, orga, db):
    db.query(Organization).update({"verified": True}); db.commit()
    assert orga.post("/elections/new", data=_election_form(_five())).status_code == 303
    assert "TESTMODUS" not in orga.get("/dashboard").text


# ---------- Antrag ----------

@pytest.mark.parametrize("override", [
    {"contact_name": ""},
    {"contact_email": "kein-email"},
    {"contact_website": "", "contact_phone": ""},
])
def test_request_requires_name_email_and_website_or_phone(verification_on, orga, outbox, override):
    response = _request_verification(orga, **override)
    assert response.status_code == 400
    assert not outbox.to(ADMIN)


def test_phone_alone_is_enough(verification_on, orga, outbox):
    response = _request_verification(orga, contact_website="", contact_phone="+49 123 456")
    assert response.headers["location"] == "/verification?sent=1"
    assert "+49 123 456" in outbox.to(ADMIN)[0].body


def test_request_sends_mail_with_data_and_link_to_admin(verification_on, orga, outbox, db):
    assert _request_verification(orga).headers["location"] == "/verification?sent=1"
    mail = outbox.to(ADMIN)[0]
    for expected in ("Testloge", "orga@loge.test", "Max Meister", "max@loge.test", "https://loge.example"):
        assert expected in mail.body
    assert f"{settings.base_url}/auth/verify/" in mail.body
    raw = re.search(r"/auth/verify/([\w-]+)", mail.body).group(1)
    org = db.query(Organization).one()
    assert org.verification_token_hash and raw not in org.verification_token_hash  # nur der Hash liegt in der DB
    page = orga.get("/verification").text
    assert "wird geprüft" in page and "Antrag erneut senden" in page


def test_new_request_invalidates_old_link(verification_on, orga, outbox, anon):
    _request_verification(orga)
    old = _admin_link(outbox)
    _request_verification(orga, contact_name="Anna Neu")
    new = _admin_link(outbox)
    assert old != new
    assert anon.get(old).status_code == 404 and anon.get(new).status_code == 200


def test_request_is_rate_limited(verification_on, orga):
    codes = [_request_verification(orga).status_code for _ in range(4)]
    assert codes == [303, 303, 303, 429]


# ---------- Admin-Seite ----------

def test_review_page_shows_data_without_linking_website(verification_on, orga, outbox, anon, create_election):
    create_election()
    _request_verification(orga)
    html = anon.get(_admin_link(outbox)).text
    assert "Testloge" in html and "Max Meister" in html and "https://loge.example" in html
    assert 'href="https://loge.example' not in html          # Webseite bewusst nicht klickbar
    assert "noindex" in html and "/lang/" not in html


def test_review_links_are_dead_without_admin_email_or_with_bad_token(verification_on, orga, outbox, anon, monkeypatch):
    _request_verification(orga)
    link = _admin_link(outbox)
    assert anon.get("/auth/verify/gibtsnicht").status_code == 404
    assert anon.post("/auth/verify/gibtsnicht/approve").status_code == 404
    assert anon.post(link.replace("/auth/verify/", "/auth/verify/") + "/unknown").status_code == 404
    monkeypatch.setattr(settings, "admin_email", "")
    assert anon.get(link).status_code == 404


def test_decision_requires_post(verification_on, orga, outbox, anon):
    _request_verification(orga)
    assert anon.get(_admin_link(outbox) + "/approve").status_code == 405


def test_approve_verifies_notifies_and_invalidates_link(verification_on, orga, outbox, anon, db):
    _request_verification(orga)
    link = _admin_link(outbox)
    response = anon.post(link + "/approve")
    assert response.status_code == 200 and "freigeschaltet" in response.text
    org = db.query(Organization).one()
    assert org.verified and org.verification_token_hash is None
    assert "verifiziert" in outbox.to("orga@loge.test")[-1].subject
    assert anon.get(link).status_code == 404 and anon.post(link + "/approve").status_code == 404
    assert orga.post("/elections/new", data=_election_form(_five())).status_code == 303
    assert "TESTMODUS" not in orga.get("/dashboard").text
    assert "ist verifiziert" in orga.get("/verification").text


def test_reject_deletes_org_with_all_data_and_notifies(verification_on, orga, outbox, anon, db, create_election):
    election_id, tokens = create_election()
    anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Ja"})
    _request_verification(orga)
    link = _admin_link(outbox)
    response = anon.post(link + "/reject")
    assert response.status_code == 200 and "abgelehnt" in response.text
    assert [db.query(m).count() for m in (Organization, Election, Invitation, Vote, MagicLink)] == [0] * 5
    assert "nicht verifizieren" in outbox.to("orga@loge.test")[-1].body
    assert orga.get("/dashboard").headers["location"] == "/login"


def test_verification_page_requires_login(anon):
    assert anon.get("/verification").headers["location"] == "/login"
    assert anon.post("/verification", data=REQUEST).headers["location"] == "/login"


def test_account_page_shows_verification_status(verification_on, orga, db):
    assert "TESTMODUS" in orga.get("/account").text
    _request_verification(orga)
    assert "Antrag wird geprüft" in orga.get("/account").text
    db.query(Organization).update({"verified": True, "verification_token_hash": None}); db.commit()
    assert "verifiziert" in orga.get("/account").text


def test_account_page_hides_verification_when_disabled(orga):
    assert "Verifizierung" not in orga.get("/account").text
