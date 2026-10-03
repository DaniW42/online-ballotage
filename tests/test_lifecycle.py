import re

from app import services
from app.db import Election, Invitation, Vote, MagicLink
from conftest import sql, local_input

EMAILS = ("a@x.test", "b@x.test", "c@x.test")


def _vote(anon, tokens, choices):
    for email, choice in choices.items():
        assert anon.post(f"/v/{tokens[email]}", data={"choice": choice}).status_code == 200


def _results_mails(outbox):
    return [m for m in outbox if m.subject.startswith("Ergebnis")]


def test_all_voted_finalizes_and_mails_result_once(create_election, anon, orga, outbox, db):
    election_id, tokens = create_election()
    _vote(anon, tokens, {"a@x.test": "Ja", "b@x.test": "Ja"})
    page = orga.get(f"/elections/{election_id}").text
    assert "<td>Ja</td>" not in page  # noch kein Zwischenstand
    _vote(anon, tokens, {"c@x.test": "Nein"})
    page = orga.get(f"/elections/{election_id}").text
    assert re.search(r"<td>Ja</td><td>2</td>", page) and re.search(r"<td>Enthaltung</td><td>0</td>", page)
    services.run_maintenance(); services.run_maintenance()
    mails = _results_mails(outbox)
    assert len(mails) == 1 and mails[0].to == "orga@loge.test"
    assert "Ja: 2" in mails[0].body and "Nein: 1" in mails[0].body and "3 von 3" in mails[0].body
    assert db.query(Election).one().finish_reason == "all_voted"


def test_deadline_finalizes_via_maintenance(create_election, anon, db):
    election_id, tokens = create_election(emails=("a@x.test", "b@x.test", "c@x.test", "d@x.test"))
    _vote(anon, tokens, {"a@x.test": "Ja", "b@x.test": "Nein", "c@x.test": "Ja"})
    sql(db, "update elections set ends_at = now() at time zone 'utc' - interval '1 minute'")
    services.run_maintenance()
    db.expire_all()
    election = db.query(Election).one()
    assert (election.status, election.finish_reason) == ("finished", "deadline")
    assert (election.total_invited, election.total_voted) == (4, 3)
    assert db.query(Vote).count() == 3


def test_fewer_than_three_votes_means_no_result_and_votes_deleted(create_election, anon, orga, db, outbox):
    election_id, tokens = create_election()
    _vote(anon, tokens, {"a@x.test": "Nein"})
    sql(db, "update elections set ends_at = now() at time zone 'utc' - interval '1 minute'")
    services.run_maintenance()
    assert db.query(Vote).count() == 0
    mails = _results_mails(outbox)
    assert len(mails) == 1 and "Nein:" not in mails[0].body and "weniger als 3" in mails[0].body
    assert "<td>Nein</td>" not in orga.get(f"/elections/{election_id}").text


def test_abort_deletes_votes_and_never_shows_result(create_election, anon, orga, db, outbox):
    election_id, tokens = create_election()
    _vote(anon, tokens, {"a@x.test": "Ja", "b@x.test": "Ja"})
    # ohne Bestätigung passiert nichts - der Abbruch geht nur über die Bestätigungsseite
    unconfirmed = orga.post(f"/elections/{election_id}/abort")
    assert unconfirmed.headers["location"].endswith(f"/elections/{election_id}/abort")
    assert db.query(Vote).count() == 2
    assert orga.get(f"/elections/{election_id}/abort").status_code == 200
    assert orga.post(f"/elections/{election_id}/abort", data={"confirm": "yes"}).status_code == 303
    assert db.query(Vote).count() == 0
    page = orga.get(f"/elections/{election_id}").text
    assert "<td>Ja</td>" not in page
    assert anon.post(f"/v/{tokens['c@x.test']}", data={"choice": "Ja"}).status_code == 403
    services.run_maintenance()
    assert not _results_mails(outbox)  # bei Abbruch keine Ergebnis-Mail


def test_extend_only_while_open(create_election, orga, db):
    election_id, tokens = create_election()
    before = db.query(Election).one().ends_at
    assert "msg=extended" in orga.post(f"/elections/{election_id}/extend", data={"ends_at": local_input(180)}).headers["location"]
    db.expire_all()
    assert db.query(Election).one().ends_at > before
    # nicht verkürzen
    assert "extend_failed" in orga.post(f"/elections/{election_id}/extend", data={"ends_at": local_input(5)}).headers["location"]
    # nach Abschluss gesperrt
    sql(db, "update elections set ends_at = now() at time zone 'utc' - interval '1 minute'")
    assert "extend_failed" in orga.post(f"/elections/{election_id}/extend", data={"ends_at": local_input(500)}).headers["location"]


def test_finalized_election_is_frozen_for_voters_and_reissue(create_election, anon, orga, db):
    election_id, tokens = create_election()
    _vote(anon, tokens, {"a@x.test": "Ja", "b@x.test": "Ja", "c@x.test": "Ja"})
    orga.get(f"/elections/{election_id}")  # löst Abschluss aus
    assert db.query(Election).one().status == "finished"
    assert "not_open" in orga.post(f"/elections/{election_id}/voters", data={"emails_raw": "d@x.test"}).headers["location"]
    assert db.query(Invitation).count() == 3
    inv = db.query(Invitation).first()
    assert "reissue_failed" in orga.post(f"/elections/{election_id}/invitations/{inv.id}/reissue").headers["location"]


def test_add_voters_skips_existing(create_election, orga, outbox, db):
    election_id, _ = create_election()
    response = orga.post(f"/elections/{election_id}/voters", data={"emails_raw": "Max <A@X.TEST>, d@x.test"})
    assert "n=1" in response.headers["location"]
    assert sorted(i.email for i in db.query(Invitation)) == ["a@x.test", "b@x.test", "c@x.test", "d@x.test"]
    assert len(outbox.to("a@x.test")) == 1 and len(outbox.to("d@x.test")) == 1


def test_reissue_invalidates_old_link_and_rejects_used(create_election, anon, orga, outbox, db):
    election_id, tokens = create_election()
    anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Ja"})
    b = db.query(Invitation).filter_by(email="b@x.test").one()
    orga.post(f"/elections/{election_id}/invitations/{b.id}/reissue")
    new_token = re.search(r"/v/([\w-]+)", outbox.to("b@x.test")[-1].body).group(1)
    assert new_token != tokens["b@x.test"]
    assert anon.get(f"/v/{tokens['b@x.test']}").status_code == 404
    assert anon.get(f"/v/{new_token}").status_code == 200
    a = db.query(Invitation).filter_by(email="a@x.test").one()
    assert "reissue_failed" in orga.post(f"/elections/{election_id}/invitations/{a.id}/reissue").headers["location"]


def test_send_failure_is_recorded_and_reissue_resends(create_election, orga, outbox, db):
    outbox.fail_for = {"b@x.test"}
    data = {"title": "T", "starts_at": local_input(-60), "ends_at": local_input(60),
            "emails_raw": "a@x.test\nb@x.test\nc@x.test", "options_raw": ""}
    election_id = orga.post("/elections/new", data=data).headers["location"].rsplit("/", 1)[-1]
    db.expire_all()
    b = db.query(Invitation).filter_by(email="b@x.test").one()
    assert b.send_error and b.sent_at is None
    assert db.query(Invitation).filter_by(email="a@x.test").one().sent_at is not None
    outbox.fail_for = set()
    orga.post(f"/elections/{election_id}/invitations/{b.id}/reissue")
    db.expire_all()
    b = db.query(Invitation).filter_by(email="b@x.test").one()
    assert b.send_error is None and b.sent_at is not None


def test_result_mail_is_retried_after_failure(create_election, anon, outbox, db):
    election_id, tokens = create_election()
    _vote(anon, tokens, {"a@x.test": "Ja", "b@x.test": "Ja", "c@x.test": "Nein"})
    outbox.fail_for = {"orga@loge.test"}
    services.run_maintenance()
    assert not _results_mails(outbox) and db.query(Election).one().result_mail_sent is False
    outbox.fail_for = set()
    services.run_maintenance(); services.run_maintenance()
    db.expire_all()
    assert len(_results_mails(outbox)) == 1 and db.query(Election).one().result_mail_sent is True


def test_reminder_goes_to_pending_voters_with_new_link_once(create_election, anon, outbox, db):
    election_id, tokens = create_election(reminder_enabled="true")
    anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Ja"})
    sql(db, "update elections set created_at = created_at - interval '3 days', "
            "ends_at = now() at time zone 'utc' + interval '20 hours'")
    outbox.clear()
    services.run_maintenance(); services.run_maintenance()
    assert sorted(m.to for m in outbox) == ["b@x.test", "c@x.test"]
    assert all("ungültig" in m.body for m in outbox)
    assert anon.get(f"/v/{tokens['b@x.test']}").status_code == 404
    new = re.search(r"/v/([\w-]+)", outbox.to("b@x.test")[0].body).group(1)
    assert anon.get(f"/v/{new}").status_code == 200


def test_no_reminder_for_short_election_or_when_disabled(create_election, outbox, db):
    create_election(reminder_enabled="true")  # läuft nur 2h: Erinnerung wäre direkt nach der Einladung
    create_election(title="ohne")
    outbox.clear()
    services.run_maintenance()
    assert not outbox


def test_retention_purges_invitations_but_keeps_counts_and_votes(create_election, anon, orga, outbox, db):
    election_id, tokens = create_election()
    _vote(anon, tokens, {"a@x.test": "Ja", "b@x.test": "Ja", "c@x.test": "Nein"})
    services.run_maintenance()
    sql(db, "update elections set finished_at = finished_at - interval '40 days'")
    services.run_maintenance()
    db.expire_all()
    election = db.query(Election).one()
    assert election.purged and db.query(Invitation).count() == 0 and db.query(Vote).count() == 3
    page = orga.get(f"/elections/{election_id}").text
    assert "3 von 3" in page


def test_retention_waits_for_result_mail(create_election, anon, outbox, db):
    election_id, tokens = create_election()
    _vote(anon, tokens, {"a@x.test": "Ja", "b@x.test": "Ja", "c@x.test": "Nein"})
    outbox.fail_for = {"orga@loge.test"}
    services.run_maintenance()
    sql(db, "update elections set finished_at = finished_at - interval '40 days'")
    services.run_maintenance()
    assert db.query(Invitation).count() == 3


def test_old_magic_links_are_cleaned_up(anon, outbox, db):
    anon.post("/register", data={"name": "L", "email": "a@loge.test"})
    sql(db, "update magic_links set expires_at = now() at time zone 'utc' - interval '2 days'")
    services.run_maintenance()
    assert db.query(MagicLink).count() == 0


def test_receipts_only_when_enabled(create_election, anon, orga):
    election_id, tokens = create_election(receipts_enabled="true")
    done = anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Ja"}).text
    assert re.search(r"<code>[0-9a-f-]{36}</code>", done)
    election_id2, tokens2 = create_election(title="ohne", emails=("d@x.test", "e@x.test", "f@x.test"))
    assert "<code>" not in anon.post(f"/v/{tokens2['d@x.test']}", data={"choice": "Ja"}).text
