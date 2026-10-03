from datetime import datetime

from app.db import Election, Invitation
from conftest import local_input


def _form(emails, **extra):
    data = {"title": "T", "starts_at": local_input(-60), "ends_at": local_input(60),
            "emails_raw": "\n".join(emails), "options_raw": ""}
    data.update(extra)
    return data


def test_minimum_three_addresses(orga, db):
    response = orga.post("/elections/new", data=_form(["a@x.test", "b@x.test"]))
    assert response.status_code == 400
    assert db.query(Election).count() == 0
    # Fehlerfall behält die Eingabe
    assert "a@x.test" in response.text


def test_duplicate_and_messy_addresses_collapse(orga, db):
    response = orga.post("/elections/new", data=_form(["Max <a@x.test>", "A@X.TEST, b@x.test", "c@x.test"]))
    assert response.status_code == 303
    assert sorted(i.email for i in db.query(Invitation)) == ["a@x.test", "b@x.test", "c@x.test"]


def test_options_default_dedupe_and_minimum(orga, db):
    ok = ["a@x.test", "b@x.test", "c@x.test"]
    orga.post("/elections/new", data=_form(ok))
    orga.post("/elections/new", data=_form(ok, options_raw="Rot, rot, Blau , ,Blau"))
    options = [e.options for e in db.query(Election).order_by(Election.created_at)]
    assert options[0] == ["Ja", "Nein", "Enthaltung"]
    assert options[1] == ["Rot", "Blau"]
    assert orga.post("/elections/new", data=_form(ok, options_raw="Nur eine")).status_code == 400


def test_times_are_local_and_stored_as_utc(orga, db):
    ok = ["a@x.test", "b@x.test", "c@x.test"]
    orga.post("/elections/new", data=_form(ok, starts_at="2026-07-01T20:00", ends_at="2026-07-02T20:00"))
    orga.post("/elections/new", data=_form(ok, starts_at="2026-12-01T20:00", ends_at="2026-12-02T20:00"))
    elections = db.query(Election).order_by(Election.starts_at).all()
    assert elections[0].starts_at == datetime(2026, 7, 1, 18, 0)   # Sommerzeit: UTC+2
    assert elections[1].starts_at == datetime(2026, 12, 1, 19, 0)  # Winterzeit: UTC+1


def test_title_newlines_are_removed(orga, db, outbox):
    orga.post("/elections/new", data=_form(["a@x.test", "b@x.test", "c@x.test"], title="Zeile1\r\nBcc: x@evil.test"))
    assert "\n" not in db.query(Election).one().title
    assert all("\n" not in m.subject for m in outbox)


def test_invitation_mails_are_sent(create_election, outbox):
    create_election()
    assert {m.to for m in outbox} >= {"a@x.test", "b@x.test", "c@x.test"}


def test_other_orga_cannot_see_election(create_election, outbox):
    from conftest import make_login
    election_id, _ = create_election()
    other = make_login(outbox, email="other@loge.test", name="Andere")
    assert other.get(f"/elections/{election_id}").headers["location"] == "/dashboard"
    assert other.post(f"/elections/{election_id}/abort").status_code in (303, 302)


def test_verification_gate(orga, db, monkeypatch):
    monkeypatch.setattr("app.main.settings.require_verification", True)
    ok = ["a@x.test", "b@x.test", "c@x.test"]
    assert orga.post("/elections/new", data=_form(ok)).status_code == 400
    from app.db import Organization
    db.query(Organization).update({"verified": True}); db.commit()
    assert orga.post("/elections/new", data=_form(ok)).status_code == 303


def test_saved_recipients_roundtrip(orga, db):
    ok = ["a@x.test", "b@x.test", "c@x.test"]
    orga.post("/elections/new", data=_form(ok, save_recipients="true"))
    assert '"a@x.test"' in orga.get("/elections/new").text
    orga.post("/elections/new", data=_form(["x@x.test", "y@x.test", "z@x.test"]))  # abgewählt: alte Liste bleibt
    assert '"a@x.test"' in orga.get("/elections/new").text
    orga.post("/recipients/clear")
    assert '"a@x.test"' not in orga.get("/elections/new").text
