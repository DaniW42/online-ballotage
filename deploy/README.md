# Produktivbetrieb mit fertigem Image

Das Image baut GitHub Actions bei jedem Push auf `main` nach
`ghcr.io/daniw42/online-ballotage` (`:latest` und `:sha-…`).

## Stacks
- `deploy/docker-compose.prod.yml`: App, Postgres und tägliche Datensicherung, ohne offene Ports.
  Die App hängt im externen Docker-Netz `PROXY_NETWORK`, in dem auch der Reverse-Proxy
  bzw. `cloudflared` läuft. Ziel für den Proxy: `http://ballotage-app:8000`.
- `deploy/docker-compose.cloudflared.yml`: optional, Cloudflare Tunnel (Token-basiert).

Das externe Netz einmalig anlegen, falls noch nicht vorhanden: `docker network create <name>`.

## Variablen

| Variable | Pflicht | Hinweis |
|---|---|---|
| `DB_PASSWORD` | ja | langes Zufallspasswort |
| `SECRET_KEY` | ja | mind. 32 Zeichen: `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | ja | Mailserver braucht ein gültiges TLS-Zertifikat |
| `SMTP_FROM_NAME` | | Absendername, Standard `ballotage.online` |
| `SMTP_USE_TLS` / `SMTP_SSL` | | STARTTLS (587, Standard) bzw. implizites TLS (465) |
| `PROXY_NETWORK` | ja | Name des externen Docker-Netzes des Reverse-Proxys |
| `BACKUP_DIR` | ja | Host-Verzeichnis für die Datensicherungen |
| `ADMIN_EMAIL` | empfohlen | aktiviert Admin-Portal und Testmodus für neue Logen |
| `LEGAL_NAME`, `LEGAL_STREET`, `LEGAL_CITY`, `LEGAL_EMAIL` | ja | Impressum |
| `LEGAL_PHONE`, `LEGAL_VAT_ID`, `LEGAL_HOSTING`, `REPO_URL` | | optional |
| `LEGAL_CDN` | | `cloudflare` (Standard im Stack), wenn der Verkehr über Cloudflare läuft |
| `BASE_URL` | | Standard `https://ballotage.online` |
| `APP_TAG` | | Standard `latest`; für Rollback z. B. `sha-abc1234` |

**Update:** neues Image ziehen und Stack neu starten (`docker compose pull && docker compose up -d`).
Migrationen laufen beim Start automatisch.

## Cloudflare
Läuft der Verkehr über Cloudflare: in der Zone **Email Address Obfuscation, Rocket Loader
und Web Analytics ausschalten** – sie fügen Skripte ein, die die Content-Security-Policy
blockiert (die Impressums-Adresse wäre sonst z. B. unsichtbar).

## Abnahme
- `https://<domain>/healthz` → `ok`
- Mit `ADMIN_EMAIL` anmelden → Admin-Portal › Mail › „Test-Mail an mich senden“
- Admin-Portal › System › Konfigurations-Check ohne Warnungen

## Datensicherung
`ballotage-backup` erstellt täglich um 03:15 einen `pg_dump` nach `BACKUP_DIR`
(7 Tages- und 4 Wochensicherungen; die Datenschutzerklärung nennt höchstens 35 Tage).
Die Dateien enthalten Einladungs-Adressen – Kopien außerhalb des Servers verschlüsselt ablegen.

Wiederherstellen (App stoppen, nur `db` laufen lassen):
```
gunzip -c <BACKUP_DIR>/last/ballotage-latest.sql.gz | docker exec -i ballotage-db psql -U ballotage -d ballotage
```
