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

    # Magic Links und Wahl-Tokens: Gültigkeitsdauer in Minuten
    magic_link_ttl_minutes: int = 15


settings = Settings()
