"""Strukturelle Anonymitätsgarantien - das Herzstück des Projekts."""
import re
from pathlib import Path

from sqlalchemy import inspect

from app.db import engine, Invitation, Vote
from conftest import sql

ROOT = Path(__file__).resolve().parent.parent


def test_votes_table_has_only_id_election_choice():
    columns = {c["name"] for c in inspect(engine).get_columns("votes")}
    assert columns == {"id", "election_id", "choice"}


def test_votes_has_no_relation_to_invitations():
    foreign_keys = inspect(engine).get_foreign_keys("votes")
    assert [fk["referred_table"] for fk in foreign_keys] == ["elections"]


def test_votes_has_no_time_columns():
    types = {str(c["type"]).upper() for c in inspect(engine).get_columns("votes")}
    assert not any("TIME" in t or "DATE" in t for t in types)


def test_invitations_has_no_vote_time_column():
    """Keine Zeitspalte, die bei der Stimmabgabe gesetzt werden könnte (updated_at, used_at, ...)."""
    names = {c["name"] for c in inspect(engine).get_columns("invitations")}
    assert "used_at" not in names and "updated_at" not in names and "cast_at" not in names
    assert not Invitation.__table__.c.created_at.onupdate
    assert not any(c.onupdate for c in Invitation.__table__.columns)


def test_voting_changes_only_the_used_flag(db, create_election, anon):
    election_id, tokens = create_election()
    before = {r.email: dict(r._mapping) for r in db.execute(
        __import__("sqlalchemy").text("select * from invitations"))}
    anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Ja"})
    db.expire_all()
    after = {r.email: dict(r._mapping) for r in db.execute(
        __import__("sqlalchemy").text("select * from invitations"))}
    changed = {k for k in before["a@x.test"] if before["a@x.test"][k] != after["a@x.test"][k]}
    assert changed == {"used"}
    assert before["b@x.test"] == after["b@x.test"]


def test_ballot_list_is_not_in_insertion_order(db, create_election, anon, orga):
    """Quittungsliste ist nach zufälliger ID sortiert, nicht nach Abgabe."""
    election_id, tokens = create_election(
        emails=[f"v{i}@x.test" for i in range(12)], receipts_enabled="true")
    for token in tokens.values():
        anon.post(f"/v/{token}", data={"choice": "Ja"})
    page = orga.get(f"/elections/{election_id}").text
    codes = re.findall(r"<code>([0-9a-f-]{36})</code>", page)
    assert len(codes) == 12 and codes == sorted(codes)


def test_no_access_log_in_container_command():
    assert "--no-access-log" in (ROOT / "Dockerfile").read_text()


def test_privacy_headers(anon):
    response = anon.get("/login")
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert anon.get("/static/style.css").headers["cache-control"].startswith("public")


def test_vote_is_stored_without_link_to_invitation(db, create_election, anon):
    election_id, tokens = create_election()
    anon.post(f"/v/{tokens['a@x.test']}", data={"choice": "Nein"})
    vote = db.query(Vote).one()
    assert vote.choice == "Nein"
    assert not hasattr(vote, "invitation_id") and not hasattr(vote, "cast_at")
