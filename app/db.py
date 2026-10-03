import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey, Integer, JSON, UniqueConstraint, create_engine
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def new_uuid():
    return str(uuid.uuid4())


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, unique=True)
    verified = Column(Boolean, default=False, nullable=False)
    # Zuletzt verwendete Empfängerliste; füllt das Formular für die nächste Kugelung vor.
    # Kann jederzeit gelöscht werden (Datensparsamkeit).
    saved_recipients = Column(JSON, default=list, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class MagicLink(Base):
    """Login ohne Passwort: Einmal-Link, kurzlebig, an eine Organisation gebunden."""
    __tablename__ = "magic_links"

    id = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    org_id = Column(UUID(as_uuid=False), ForeignKey("organizations.id"), nullable=False)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Election(Base):
    __tablename__ = "elections"

    id = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    org_id = Column(UUID(as_uuid=False), ForeignKey("organizations.id"), nullable=False)
    title = Column(String, nullable=False)
    # Standard: klassische Kugelung. Über "Erweiterte Einstellungen" änderbar.
    options = Column(JSON, default=lambda: ["Ja", "Nein", "Enthaltung"])
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)
    reminder_enabled = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Lebenszyklus: open -> finished (Frist abgelaufen / alle abgestimmt) oder aborted.
    # Ab "finished"/"aborted" ist die Wählerliste eingefroren (kein Verlängern,
    # kein Nachladen von Wählern), sonst ließe sich eine Stimme per Differenz ableiten.
    status = Column(String, default="open", nullable=False)
    finish_reason = Column(String, nullable=True)  # deadline | all_voted | aborted
    finished_at = Column(DateTime, nullable=True)
    # Beim Abschluss festgehalten, damit sie auch nach dem Löschen der
    # Einladungen (Datensparsamkeit) noch angezeigt werden können.
    total_invited = Column(Integer, nullable=True)
    total_voted = Column(Integer, nullable=True)
    result_mail_sent = Column(Boolean, default=False, nullable=False)
    reminder_sent = Column(Boolean, default=False, nullable=False)
    # Optional: Stimmen-Quittung + Liste aller Stimmen nach Abschluss (Prüfbarkeit)
    receipts_enabled = Column(Boolean, default=False, nullable=False)
    # Einladungsdaten (Emails) nach Ablauf der Aufbewahrungsfrist gelöscht
    purged = Column(Boolean, default=False, nullable=False)


class Invitation(Base):
    """Nur zur Berechtigungs- und Einmaligkeitsprüfung. KEINE Referenz auf votes."""
    __tablename__ = "invitations"
    # Dieselbe Adresse darf pro Abstimmung nur einmal eingeladen werden.
    __table_args__ = (UniqueConstraint("election_id", "email", name="uq_invitation_election_email"),)

    id = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    election_id = Column(UUID(as_uuid=False), ForeignKey("elections.id"), nullable=False)
    email = Column(String, nullable=False)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    # Bewusst nur Boolean statt Zeitstempel: ein used_at wäre per Zeitvergleich
    # mit der Stimme korrelierbar und würde die Anonymität aushebeln.
    used = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Versandstatus der Einladungsmail. Wird nur beim (Neu-)Versand beschrieben,
    # NIE im Zuge einer Stimmabgabe - sonst wäre die Zeitkorrelation wieder da.
    sent_at = Column(DateTime, nullable=True)
    send_error = Column(String, nullable=True)


class Vote(Base):
    """Bewusst OHNE jede Fremdschlüssel-Beziehung zu invitations. Das ist die
    technische Grundlage der Anonymität: niemand kann Stimme und Person
    über die Datenbank verknüpfen, auch der Admin nicht."""
    __tablename__ = "votes"

    id = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    election_id = Column(UUID(as_uuid=False), ForeignKey("elections.id"), nullable=False)
    choice = Column(String, nullable=False)
    # Bewusst KEIN Zeitstempel (siehe Invitation.used) und keine
    # Reihenfolge-Information; die ID ist ein zufälliges UUIDv4.


def init_db():
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
