# ballotage.online

Selbst hostbares Tool für **anonyme Online-Abstimmungen** (z. B. Freimaurer-Kugelungen).
Passwortloser Login per Magic Link, Einladung per E-Mail, Einmal-Links zur Stimmabgabe –
und eine Datenstruktur, in der Stimme und Person **nicht verknüpfbar** sind.

Die Webseite enthält Startseite, Erklärung, Sicherheitsseite, FAQ, Selbst-Hosting-Anleitung,
Impressum und Datenschutzerklärung. Alle Texte stehen in Sprachdateien (derzeit Deutsch),
siehe [docs/TRANSLATING.md](docs/TRANSLATING.md).

## Lizenz

[Elastic License 2.0 (ELv2)](LICENSE) – Quelltext frei einsehbar, nutzbar, veränderbar und
weitergebbar, auch kommerziell. Einzige Einschränkung: Niemand darf die Software Dritten als
gehosteten/verwalteten Dienst anbieten, der im Wesentlichen dieselbe Funktionalität bereitstellt.
Das erfüllt nicht die Open Source Definition der OSI und ist daher als **„source-available“**
zu bezeichnen.

## Entwicklung

Stack: FastAPI, PostgreSQL, Jinja2, Docker Compose.

```
cp .env.example .env     # DB_PASSWORD, SECRET_KEY, BASE_URL=http://localhost:8000 ausfüllen
docker compose up -d --build
```

`docker compose up` lädt automatisch `docker-compose.override.yml` (Mailpit als Test-Mailserver,
Weboberfläche auf http://localhost:8025, SMTP-Werte sind dort vorbelegt). App: http://localhost:8000.

Tests (eigene Datenbank `kugelung_test`, Schema aus den Alembic-Migrationen):

```
make test
```

Schema-Änderungen laufen über Alembic (`migrations/`); der Container führt beim Start
`alembic upgrade head` aus.

## Produktivbetrieb

Zwei Varianten, ausführlich unter `/self-hosting` bzw. in `app/locales/de.json`:

- **Eigenständig mit automatischem HTTPS (Caddy):**
  `docker compose -f docker-compose.yml -f docker-compose.standalone.yml up -d --build`
  (`SITE_ADDRESS` in der `.env` setzen)
- **Hinter eigenem Reverse-Proxy (z. B. Nginx Proxy Manager):**
  `docker compose -f docker-compose.yml up -d --build`, Proxy auf Port 8000 des App-Containers.
  `/v/` und `/auth/` vom Access-Log ausnehmen, `FORWARDED_ALLOW_IPS` auf die Proxy-IP setzen.

Mailpit (Override-Datei) startet dabei **nicht**. Vor dem öffentlichen Betrieb müssen die `LEGAL_*`-Angaben (Impressum) und `ADMIN_EMAIL` gesetzt sein.

Konfiguration über Umgebungsvariablen: siehe `.env.example` und `app/config.py`
(u. a. `TIMEZONE`, `REQUIRE_VERIFICATION`, `MIN_VOTERS`, `RETENTION_DAYS`,
`REMINDER_HOURS_BEFORE`, `RATE_LIMIT_PER_EMAIL`, `RATE_LIMIT_PER_IP`, `LEGAL_*`).
Logen freigeben (bei `REQUIRE_VERIFICATION=true`):
`docker compose exec app python -m app.cli verify loge@example.org`.

Betrieb mit genau **einem** uvicorn-Worker (Wartungslauf und Rate-Limits liegen im Prozess).

## Sicherheitsrelevantes (für Entwickler)

- **Anonymität**: `votes` hat keine Fremdschlüssel-Beziehung zu `invitations` und **keine
  Zeitspalte**; `invitations` speichert nur `used` (Boolean) statt eines Zeitstempels. Diese
  Trennung darf nicht aufgeweicht werden (auch keine Logging-Korrelation von Token-Verbrauch
  und Stimmeneingang). Die Tests in `tests/test_anonymity.py` sichern das ab.
- **Access-Logs**: Der App-Container startet mit `--no-access-log`; Proxy-Logs für `/v/*` und
  `/auth/*` müssen ebenfalls abgeschaltet sein.
- **Tokens**: `secrets.token_urlsafe(32)`, serverseitig nur als SHA-256-Hash gespeichert.
- **Nach Abschluss eingefroren**: kein Verlängern/Nachladen/Neu-Ausstellen mehr, sonst wäre
  eine Stimme aus der Differenz zweier Zwischenstände ableitbar.
- **Strikte CSP**: keine Inline-Skripte/-Styles; Skripte liegen in `app/static/js/`.

## Offene Punkte

- Antragsworkflow für die Verifizierung von Logen (aktuell `REQUIRE_VERIFICATION` + CLI)
- Mehrere Administratoren pro Loge, Self-Service-Löschung von Konten
- Kryptografische Entkopplung von Token und Stimme (Blind Signatures)
