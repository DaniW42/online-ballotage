from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.db import Vote, Invitation, Election
from app.main import app
from conftest import sql


def test_vote_form_and_submit(create_election, anon, db):
    _, tokens = create_election()
    assert anon.get(f"/v/{tokens['a@x.test']}").status_code == 200
    assert anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Ja"}).status_code == 200
    assert db.query(Vote).count() == 1
    assert db.query(Invitation).filter(Invitation.used.is_(True)).count() == 1


def test_second_vote_with_same_token_rejected(create_election, anon, db):
    _, tokens = create_election()
    anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Ja"})
    assert anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Nein"}).status_code == 410
    assert anon.get(f"/v/{tokens['a@x.test']}").status_code == 410
    assert db.query(Vote).count() == 1


def test_unknown_token(anon):
    assert anon.get("/v/gibtsnicht").status_code == 404
    assert anon.post("/v/gibtsnicht", data={"choice": "Ja"}).status_code == 410


def test_invalid_choice_rejected_and_token_stays_valid(create_election, anon, db):
    _, tokens = create_election()
    assert anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Vielleicht"}).status_code == 400
    assert db.query(Invitation).filter(Invitation.used.is_(True)).count() == 0
    assert anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Ja"}).status_code == 200


def test_not_started_and_expired_windows(create_election, anon):
    _, future = create_election(start=60, end=120)
    assert anon.get(f"/v/{future['a@x.test']}").status_code == 403
    assert anon.post(f"/v/{future['a@x.test']}", data={"choice": "Ja"}).status_code == 403


def test_concurrent_double_submit_counts_once(create_election, db):
    _, tokens = create_election()
    token = tokens["a@x.test"]

    def submit(_):
        return TestClient(app, follow_redirects=False).post(f"/v/{token}", data={"choice": "Ja"}).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(submit, range(8)))
    assert sorted(codes).count(200) == 1 and set(codes) <= {200, 410}
    assert db.query(Vote).count() == 1


def test_vote_rejected_after_finalization(create_election, anon, db):
    election_id, tokens = create_election(emails=["a@x.test", "b@x.test", "c@x.test", "d@x.test"])
    sql(db, "update elections set ends_at = now() at time zone 'utc' - interval '1 minute'")
    assert anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Ja"}).status_code == 403
    assert db.query(Vote).count() == 0
