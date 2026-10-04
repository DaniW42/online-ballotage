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


def _admin_mail(outbox):
    return outbox.to(ADMIN)[-1]


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


def test_request_sends_mail_with_data_and_portal_link_to_admin(verification_on, orga, outbox, db):
    assert _request_verification(orga).headers["location"] == "/verification?sent=1"
    mail = outbox.to(ADMIN)[0]
    for expected in ("Testloge", "orga@loge.test", "Max Meister", "max@loge.test", "https://loge.example"):
        assert expected in mail.body
    # Die Mail verweist nur aufs Admin-Portal - kein Link, der selbst etwas freischaltet
    assert f"{settings.base_url}/admin/verifications" in mail.body and "/auth/verify/" not in mail.body
    assert db.query(Organization).one().verification_token_hash  # Antrag ist offen
    page = orga.get("/verification").text
    assert "wird geprüft" in page and "Antrag erneut senden" in page



def test_request_is_rate_limited(verification_on, orga):
    codes = [_request_verification(orga).status_code for _ in range(4)]
    assert codes == [303, 303, 303, 429]


# ---------- Admin-Seite ----------






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




def test_new_request_updates_contact_data(verification_on, orga, outbox, db):
    _request_verification(orga)
    _request_verification(orga, contact_name="Anna Neu")
    db.expire_all()
    assert db.query(Organization).one().contact_name == "Anna Neu"
    assert "Anna Neu" in _admin_mail(outbox).body


def test_old_mail_links_lead_to_portal_and_decide_nothing(verification_on, orga, outbox, anon, db):
    _request_verification(orga)
    assert anon.get("/auth/verify/irgendein-alter-link").headers["location"] == "/admin/verifications"
    assert anon.post("/auth/verify/irgendein-alter-link/approve").status_code in (404, 405)
    assert anon.post("/auth/verify/irgendein-alter-link/reject").status_code in (404, 405)
    db.expire_all()
    org = db.query(Organization).one()
    assert org.verified is False and org.verification_token_hash        # unverändert offen


def test_pending_requests_survive_admin_email_change(verification_on, orga, db, monkeypatch):
    from app import services
    services.sync_admin_identity(db)
    _request_verification(orga)
    monkeypatch.setattr(settings, "admin_email", "neu@x.test")
    services.sync_admin_identity(db)
    db.expire_all()
    assert db.query(Organization).one().verification_token_hash        # Antrag bleibt im Portal sichtbar
