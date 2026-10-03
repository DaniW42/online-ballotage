"""Empfängerliste, Abstimmungen löschen, Konto löschen."""
from app import services
from app.db import Election, Invitation, Vote, Organization, MagicLink
from conftest import sql, make_login


def test_recipients_page_view_edit_clear(orga, db):
    assert "Noch keine Empfängerliste" in orga.get("/dashboard").text
    orga.post("/recipients", data={"emails_raw": "Max <Max@X.test>, anna@x.test\nanna@x.test"})
    page = orga.get("/recipients?saved=1").text
    assert "anna@x.test" in page and "max@x.test" in page and "2 Adressen" in page
    assert db.query(Organization).one().saved_recipients == ["anna@x.test", "max@x.test"]
    orga.post("/recipients", data={"emails_raw": "nur@x.test"})
    db.expire_all()
    assert db.query(Organization).one().saved_recipients == ["nur@x.test"]
    orga.post("/recipients/clear")
    db.expire_all()
    assert db.query(Organization).one().saved_recipients == []


def test_recipients_requires_login(anon):
    assert anon.get("/recipients").headers["location"] == "/login"
    assert anon.post("/recipients", data={"emails_raw": "a@x.test"}).headers["location"] == "/login"


def test_delete_finished_election_removes_everything(create_election, anon, orga, db):
    election_id, tokens = create_election()
    for e, c in (("a@x.test", "Ja"), ("b@x.test", "Ja"), ("c@x.test", "Nein")):
        anon.post(f"/v/{tokens[e]}", data={"choice": c})
    services.run_maintenance()  # Abschluss + Ergebnis-Mail
    assert orga.get(f"/elections/{election_id}/delete").status_code == 200
    # ohne Bestätigung passiert nichts
    assert orga.post(f"/elections/{election_id}/delete").headers["location"].endswith("/delete")
    assert db.query(Election).count() == 1
    assert orga.post(f"/elections/{election_id}/delete", data={"confirm": "yes"}).headers["location"] == "/dashboard?msg=deleted"
    assert (db.query(Election).count(), db.query(Vote).count(), db.query(Invitation).count()) == (0, 0, 0)
    assert "wurde gelöscht" in orga.get("/dashboard?msg=deleted").text


def test_delete_running_election_aborts_first_without_result(create_election, anon, orga, db, outbox):
    election_id, tokens = create_election()
    anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Ja"})
    orga.post(f"/elections/{election_id}/delete", data={"confirm": "yes"})
    assert db.query(Election).count() == 0 and db.query(Vote).count() == 0
    services.run_maintenance()
    assert not [m for m in outbox if m.subject.startswith("Ergebnis")]


def test_delete_waits_for_result_mail(create_election, anon, orga, db, outbox):
    election_id, tokens = create_election()
    for e in ("a@x.test", "b@x.test", "c@x.test"):
        anon.post(f"/v/{tokens[e]}", data={"choice": "Ja"})
    orga.get(f"/elections/{election_id}")  # schließt ab, Mail noch nicht versendet
    response = orga.post(f"/elections/{election_id}/delete", data={"confirm": "yes"})
    assert "delete_blocked" in response.headers["location"] and db.query(Election).count() == 1
    services.run_maintenance()
    assert orga.post(f"/elections/{election_id}/delete", data={"confirm": "yes"}).headers["location"].endswith("deleted")


def test_cannot_delete_foreign_election(create_election, outbox, db):
    election_id, _ = create_election()
    other = make_login(outbox, email="other@loge.test", name="Andere")
    assert other.get(f"/elections/{election_id}/delete").status_code == 303  # kein Zugriff auf fremde Abstimmung
    other.post(f"/elections/{election_id}/delete", data={"confirm": "yes"})
    assert db.query(Election).count() == 1


def test_account_page_and_blocked_deletion(create_election, orga, db):
    election_id, _ = create_election(title="Offen")
    page = orga.get("/account").text
    assert "orga@loge.test" in page and "Offen" in page and 'name="confirm_email"' not in page
    response = orga.post("/account/delete", data={"confirm_email": "orga@loge.test"})
    assert response.headers["location"] == "/account?error=blocked"
    assert db.query(Organization).count() == 1
    assert "nicht gelöscht werden" in orga.get("/account?error=blocked").text


def test_account_deletion_requires_matching_email_and_clears_session(orga, db):
    assert 'name="confirm_email"' in orga.get("/account").text
    assert orga.post("/account/delete", data={"confirm_email": "falsch@x.test"}).headers["location"] == "/account?error=email"
    assert db.query(Organization).count() == 1
    response = orga.post("/account/delete", data={"confirm_email": " ORGA@loge.test "})
    assert response.status_code == 200 and "Max-Age=0" in response.headers["set-cookie"]
    assert (db.query(Organization).count(), db.query(MagicLink).count()) == (0, 0)


def test_account_can_be_deleted_after_all_elections_deleted(create_election, orga, db):
    election_id, _ = create_election()
    orga.post(f"/elections/{election_id}/delete", data={"confirm": "yes"})
    assert orga.post("/account/delete", data={"confirm_email": "orga@loge.test"}).status_code == 200
    assert db.query(Organization).count() == 0


def test_account_requires_login(anon):
    assert anon.get("/account").headers["location"] == "/login"
    assert anon.post("/account/delete", data={"confirm_email": "x"}).headers["location"] == "/login"
