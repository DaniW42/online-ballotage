from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    base_url: str
    smtp_host: str
    smtp_port: int = 587
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_use_tls: bool = True      # STARTTLS (Port 587), Zertifikat wird geprüft
    smtp_ssl: bool = False         # implizites TLS (Port 465) statt STARTTLS
    smtp_timeout_seconds: int = 30

    # Sitzungsdauer: normale Konten in Tagen, Admin-Portal in Stunden
    session_days: int = 10
    admin_session_hours: int = 6

    # Magic Links: Gültigkeitsdauer in Minuten
    magic_link_ttl_minutes: int = 15

    # Anbieterkennzeichnung (Impressum/Datenschutz). Muss für den öffentlichen
    # Betrieb gesetzt werden; solange Pflichtangaben fehlen, zeigt die Seite
    # einen deutlichen Hinweis.
    legal_name: str = ""
    legal_street: str = ""
    legal_city: str = ""          # PLZ und Ort
    legal_country: str = "Deutschland"
    legal_email: str = ""
    legal_phone: str = ""         # optional
    legal_vat_id: str = ""        # optional (USt-IdNr.)
    legal_hosting: str = ""       # optional: Hosting-Anbieter für die Datenschutzerklärung
    repo_url: str = ""            # optional: Link zum Quelltext

    # Zeitzone, in der Beginn/Ende eingegeben und angezeigt werden.
    # Intern wird alles als naive UTC gespeichert.
    timezone: str = "Europe/Berlin"

    # Verifizierung von Logen: Ist ADMIN_EMAIL gesetzt, laufen unverifizierte Logen im
    # TESTMODUS (höchstens test_mode_max_voters Empfänger) und können die Verifizierung
    # beantragen; der Admin erhält eine Mail mit Freigabe-Link. Leer = Verifizierung aus,
    # alle Logen gelten als verifiziert (typisch für selbst betriebene Instanzen).
    admin_email: str = ""
    test_mode_max_voters: int = 3
    # Testmodus: höchstens so viele Einladungen pro Loge und 24 Stunden (gegen Spam
    # über viele kleine Abstimmungen)
    test_mode_daily_invitations: int = 10

    # Obergrenzen gegen Missbrauch und Überlast
    max_recipients: int = 100          # Empfänger pro Abstimmung
    max_title_length: int = 200
    max_options: int = 20
    max_option_length: int = 100
    max_request_bytes: int = 1_000_000

    # Mindestanzahl Eingeladener und abgegebener Stimmen, damit ein Ergebnis
    # überhaupt sichtbar wird (sonst wäre die einzelne Stimme ableitbar).
    min_voters: int = 3

    # Erinnerung an Nicht-Abgestimmte: so viele Stunden vor Fristende
    reminder_hours_before: int = 24

    # Datensparsamkeit: so viele Tage nach Abschluss werden die Einladungsdaten
    # (Email-Adressen, Teilnahmestatus) gelöscht. Stimmen/Ergebnis bleiben.
    retention_days: int = 30

    # Globales Limit für den Mailversand: Mindestabstand zwischen zwei E-Mails in
    # Sekunden (1 = höchstens eine E-Mail pro Sekunde, 0 = aus).
    mail_min_interval_seconds: float = 1.0

    # Rate-Limits (pro Stunde) für Login/Registrierung
    rate_limit_per_email: int = 5
    rate_limit_per_ip: int = 20


    @field_validator("secret_key")
    @classmethod
    def _secret_key_strong(cls, value: str) -> str:
        # Leerer/kurzer Schlüssel = fälschbare Sitzungs-Cookies. Lieber gar nicht starten.
        if len(value) < 32:
            raise ValueError("SECRET_KEY muss mindestens 32 Zeichen lang sein "
                             "(python3 -c \"import secrets;print(secrets.token_urlsafe(32))\")")
        return value

    @field_validator("base_url")
    @classmethod
    def _base_url_clean(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("BASE_URL muss mit http:// oder https:// beginnen")
        return value

    @property
    def legal_complete(self) -> bool:
        return all([self.legal_name, self.legal_street, self.legal_city, self.legal_email])


settings = Settings()
