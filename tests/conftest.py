"""Test-Setup: eigene Datenbank, Mails werden abgefangen, Migrationen werden mitgetestet."""
import os
import re
import smtplib
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2
import pytest
from sqlalchemy.engine import make_url

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://kugelung:kugelung@localhost:5432/kugelung_test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("BASE_URL", "http://testserver")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("SMTP_USER", "")
os.environ.setdefault("SMTP_PASSWORD", "")
os.environ.setdefault("SMTP_FROM", "test@example.org")


def _recreate_test_database():
    url = make_url(os.environ["DATABASE_URL"])
    assert url.database.endswith("_test"), "Tests dürfen nur gegen eine *_test-Datenbank laufen"
    conn = psycopg2.connect(
        host=url.host, port=url.port, user=url.username, password=url.password, dbname="postgres"
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{url.database}"')
    conn.close()


_recreate_test_database()

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.ratelimit import limiter  # noqa: E402

Mail = namedtuple("Mail", "to subject body")


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    """Schema kommt aus den Alembic-Migrationen - so wird auch die Migration geprüft."""
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture(autouse=True)
def clean_state():
    db = SessionLocal()
    db.execute(
        __import__("sqlalchemy").text(
            "TRUNCATE votes, invitations, magic_links, elections, organizations CASCADE"
        )
    )
    db.commit()
    db.close()
    limiter.hits.clear()


class Outbox(list):
    fail_for: set

    def to(self, address):
        return [m for m in self if m.to == address]


@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    box = Outbox()
    box.fail_for = set()

    def fake_send(to, subject, body):
        if to in box.fail_for:
            raise smtplib.SMTPException("simulierter Versandfehler")
        box.append(Mail(to, subject, body))

    monkeypatch.setattr("app.mail.send_mail", fake_send)
    return box


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def anon():
    return TestClient(app, follow_redirects=False)


# ---------- Hilfsfunktionen ----------

def local_input(minutes_from_now: int) -> str:
    """Wert für <input type=datetime-local> in Europe/Berlin."""
    dt = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return dt.astimezone(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%dT%H:%M")


def make_login(outbox, email="orga@loge.test", name="Testloge") -> TestClient:
    client = TestClient(app, follow_redirects=False)
    client.post("/register", data={"name": name, "email": email})
    token = re.search(r"/auth/(\S+)", outbox.to(email)[-1].body).group(1)
    response = client.post(f"/auth/{token}")
    assert response.status_code == 303
    return client


@pytest.fixture
def orga(outbox):
    return make_login(outbox)


def vote_token(outbox, email) -> str:
    mails = [m for m in outbox.to(email) if "/v/" in m.body]
    return re.search(r"/v/([\w-]+)", mails[-1].body).group(1)


@pytest.fixture
def create_election(orga, outbox):
    """Legt eine Abstimmung an und gibt (election_id, {email: token}) zurück."""
    def _create(emails=("a@x.test", "b@x.test", "c@x.test"), title="Aufnahme", start=-60, end=60, **extra):
        data = {
            "title": title,
            "starts_at": local_input(start),
            "ends_at": local_input(end),
            "emails_raw": "\n".join(emails),
            "options_raw": "Ja, Nein, Enthaltung",
            **extra,
        }
        response = orga.post("/elections/new", data=data)
        assert response.status_code == 303, response.text[:300]
        election_id = response.headers["location"].rsplit("/", 1)[-1]
        return election_id, {e: vote_token(outbox, e) for e in emails}
    return _create


def sql(db, statement, **params):
    result = db.execute(__import__("sqlalchemy").text(statement), params)
    db.commit()
    return result
