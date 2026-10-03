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

    # Zeitzone, in der Beginn/Ende eingegeben und angezeigt werden.
    # Intern wird alles als naive UTC gespeichert.
    timezone: str = "Europe/Berlin"

    # Wenn true, dürfen nur verifizierte Logen Abstimmungen anlegen
    # (Freigabe per `python -m app.cli verify <email>`).
    require_verification: bool = False

    # Rate-Limits (pro Stunde) für Login/Registrierung
    rate_limit_per_email: int = 5
    rate_limit_per_ip: int = 20


settings = Settings()
