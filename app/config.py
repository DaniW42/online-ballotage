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
    smtp_use_tls: bool = True

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

    # Wenn true, dürfen nur verifizierte Logen Abstimmungen anlegen
    # (Freigabe per `python -m app.cli verify <email>`).
    require_verification: bool = False

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


    @property
    def legal_complete(self) -> bool:
        return all([self.legal_name, self.legal_street, self.legal_city, self.legal_email])


settings = Settings()
