import uuid

from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey, Integer, JSON, UniqueConstraint, create_engine
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings
from .timeutil import utcnow

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
    # Erster erfolgreicher Login. Nie bestätigte Registrierungen werden nach 24 h gelöscht.
    confirmed_at = Column(DateTime, nullable=True)
    # Wird beim Abmelden erhöht: macht alle bisher ausgestellten Sitzungs-Cookies ungültig
    # (serverseitige Abmeldung auf allen Geräten).
    session_version = Column(Integer, default=0, nullable=False, server_default="0")
    # Vom Admin gesperrt: kein Login, keine neuen Mails für diese Loge
    blocked = Column(Boolean, default=False, nullable=False, server_default="false")
    last_active_at = Column(DateTime, nullable=True)
    # Individuelle Obergrenze für Empfänger pro Abstimmung (sonst MAX_RECIPIENTS)
    max_recipients = Column(Integer, nullable=True)
    # Zuletzt verwendete Empfängerliste; füllt das Formular für die nächste Kugelung vor.
    # Kann jederzeit gelöscht werden (Datensparsamkeit).
    saved_recipients = Column(JSON, default=list, nullable=False)
    # Antrag auf Verifizierung (Angaben der Loge für den Admin)
    contact_name = Column(String, nullable=True)
    contact_email = Column(String, nullable=True)
    contact_website = Column(String, nullable=True)
    contact_phone = Column(String, nullable=True)
    verification_requested_at = Column(DateTime, nullable=True)
    # Gesetzt = offener Verifizierungsantrag (Zufallswert, kein Link); nach der Entscheidung gelöscht
    verification_token_hash = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow)


class MagicLink(Base):
    """Login ohne Passwort: Einmal-Link, kurzlebig, an eine Organisation gebunden."""
    __tablename__ = "magic_links"

    id = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    # org_id ist leer bei Admin-Login-Links
    org_id = Column(UUID(as_uuid=False), ForeignKey("organizations.id"), nullable=True)
    admin = Column(Boolean, default=False, nullable=False, server_default="false")
    token_hash = Column(String, nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class Election(Base):
    __tablename__ = "elections"

    id = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    org_id = Column(UUID(as_uuid=False), ForeignKey("organizations.id"), nullable=False)
    title = Column(String, nullable=False)
    # Standard: klassische Kugelung. Über "Erweiterte Einstellungen" änderbar.
    options = Column(JSON, default=lambda: ["Weiß", "Schwarz", "Enthaltung"])
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)
    reminder_enabled = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    # Sprache für Einladungs-/Ergebnis-Mails und die Abstimmungsseiten der Wähler
    language = Column(String, default="de", nullable=False, server_default="de")

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
    # Fehlversuche der Ergebnis-Mail (exponentielles Warten); nach zu vielen Versuchen
    # aufgegeben - das Ergebnis bleibt im Dashboard sichtbar, Löschfristen laufen weiter.
    result_mail_attempts = Column(Integer, default=0, nullable=False, server_default="0")
    result_mail_failed = Column(Boolean, default=False, nullable=False, server_default="false")
    reminder_sent = Column(Boolean, default=False, nullable=False)
    # False bei geplanter Abstimmung: Einladungsmails gehen erst zum Beginn raus
    # (die Roh-Tokens existieren vorher nicht; sie werden beim Versand erzeugt).
    invitations_dispatched = Column(Boolean, default=True, nullable=False, server_default="true")
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
    created_at = Column(DateTime, default=utcnow)
    # Versandstatus der Einladungsmail. Wird nur beim (Neu-)Versand beschrieben,
    # NIE im Zuge einer Stimmabgabe - sonst wäre die Zeitkorrelation wieder da.
    sent_at = Column(DateTime, nullable=True)
    send_error = Column(String, nullable=True)
    # Wann der Versand angestoßen wurde; bleibt eine Mail danach ohne Ergebnis
    # (z. B. Neustart während des Versands), wird sie als Fehler markiert.
    send_queued_at = Column(DateTime, nullable=True)


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


class AdminState(Base):
    """Einzelne Zeile (id=1): Zustand des Admin-Portals."""
    __tablename__ = "admin_state"

    id = Column(Integer, primary_key=True, default=1)
    session_version = Column(Integer, default=0, nullable=False, server_default="0")
    mail_paused = Column(Boolean, default=False, nullable=False, server_default="false")
    # Hash der ADMIN_EMAIL, für die Sitzungen/Links ausgestellt wurden. Ändert sich die
    # Adresse, werden alte Admin-Sitzungen und offene Admin-Login-Links ungültig.
    admin_email_hash = Column(String, nullable=True)


class AuditLog(Base):
    """Protokoll der Admin-Aktionen. Nur Metadaten (Aktion, Ziel), nie Abstimmungsinhalte."""
    __tablename__ = "audit_log"

    id = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    at = Column(DateTime, default=utcnow, nullable=False, index=True)
    action = Column(String, nullable=False)
    target = Column(String, nullable=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
