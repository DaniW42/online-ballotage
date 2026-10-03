# Häufige Fragen

Diese FAQ beschreibt, wie sich die Software tatsächlich verhält. Sie ist so
geschrieben, dass sie später unverändert auf die Webseite übernommen werden kann.

## Grundlagen

### Wofür ist das Tool gedacht?
Für geheime Abstimmungen in Vereinen und Logen, zum Beispiel Kugelungen: Eine
Person legt eine Abstimmung an, die Stimmberechtigten erhalten per E-Mail einen
persönlichen Link und stimmen darüber anonym ab.

### Brauche ich ein Passwort?
Nein. Organisatoren melden sich mit einem Link an, den sie per E-Mail erhalten
(15 Minuten gültig, einmal verwendbar). Wähler brauchen gar kein Konto, nur den
Link aus ihrer Einladung.

### Was kostet es, und unter welcher Lizenz steht es?
Der Quelltext steht unter der Elastic License 2.0. Sie dürfen die Software
einsehen, nutzen, verändern und weitergeben, auch kommerziell. Nicht erlaubt ist,
sie Dritten als gehosteten oder verwalteten Dienst mit im Wesentlichen gleicher
Funktionalität anzubieten. Das ist „source-available", aber nicht „Open Source" im
Sinne der OSI.

## Anonymität

### Wie wird die Anonymität sichergestellt?
Die Tabelle der Stimmen hat keine Verbindung zur Tabelle der Einladungen: keinen
Fremdschlüssel, keine gemeinsame ID. Zusätzlich speichern wir bewusst keinen
Zeitpunkt der Stimmabgabe. Die Einladung merkt sich nur „hat abgestimmt: ja/nein",
die Stimme nur Auswahl und eine Zufalls-ID. Eine Zuordnung über Zeitnähe oder
Einfügereihenfolge ist deshalb nicht möglich. Die Liste der Beteiligten wird nach
E-Mail-Adresse sortiert, nicht nach Reihenfolge der Einladung oder Abstimmung.

### Kann der Organisator sehen, wer wie abgestimmt hat?
Nein. Er sieht, wer **schon abgestimmt hat** und wer noch nicht, aber nicht, wie.
Das Ergebnis erscheint erst nach Abschluss.

### Gegen wen schützt das, und gegen wen nicht?
Es schützt gegen jeden, der nachträglich die Datenbank oder ein Backup liest,
einschließlich des Organisators und der Administratoren mit reinem Datenbankzugriff.

Es schützt **nicht** gegen jemanden, der den laufenden Server kontrolliert und den
Programmcode verändert oder Zugriff auf alle Server- und Proxy-Logs hat. Der Server
sieht beim Abstimmen kurz Link und Auswahl im selben Vorgang. Betreiben Sie die
Instanz deshalb in der Hand einer Person oder Stelle, der die Stimmberechtigten
vertrauen, und halten Sie Zugriffs-Logs für die Pfade `/v/*` und `/auth/*` aus.
Eine kryptografische Lösung, die auch das ausschließt, bietet das Tool bisher nicht.

### Was ist mit Logdateien?
Der App-Container schreibt keine Zugriffsprotokolle, weil die Links die geheimen
Tokens enthalten. Prüfen Sie dasselbe bei einem vorgeschalteten Reverse-Proxy
(Nginx, Nginx Proxy Manager) und schließen Sie dort `/v/*` und `/auth/*` vom
Access-Log aus.

### Was passiert bei sehr kleinen Gruppen?
Eine Abstimmung braucht mindestens **drei** E-Mail-Adressen. Ein Ergebnis wird
außerdem nur angezeigt, wenn mindestens drei Stimmen abgegeben wurden. Bei weniger
würde die einzelne Stimme erkennbar. In diesem Fall wird kein Ergebnis angezeigt und
die abgegebenen Stimmen werden gelöscht.
Auch bei größeren Gruppen gilt: Fällt das Ergebnis einstimmig aus, ist die Wahl
jedes Einzelnen damit logischerweise bekannt. Das lässt sich technisch nicht
vermeiden.

### Kann dieselbe Person zweimal abstimmen?
Pro Abstimmung wird jede E-Mail-Adresse nur einmal eingeladen und jeder Link ist nur
einmal gültig. Adressen werden in Kleinschreibung verglichen. Zwei verschiedene
Adressen derselben Person (zum Beispiel `max@…` und `max+loge@…`) erkennt das Tool
nicht als dieselbe Person. Das prüft die Loge bei der Empfängerliste.

## Ablauf einer Abstimmung

### Wie lege ich eine Abstimmung an?
Titel, Beginn und Ende angeben, E-Mail-Adressen einfügen (beliebig formatiert, etwa
aus Outlook oder Excel; das Tool zieht die Adressen selbst heraus) und
abschicken. Alle erhalten sofort ihren Link. Beginn und Ende gelten in der
eingestellten Zeitzone (Standard: Europe/Berlin).

### Welche Antwortmöglichkeiten gibt es?
Standard ist die klassische Kugelung: Ja, Nein, Enthaltung. Unter „Erweiterte
Einstellungen" lassen sich eigene Optionen festlegen (mindestens zwei, kommagetrennt).

### Wann endet eine Abstimmung?
Entweder wenn die Frist abläuft oder sobald alle Eingeladenen abgestimmt haben, je
nachdem, was zuerst eintritt. Danach lassen sich keine Stimmen mehr abgeben.

### Wann und wie bekomme ich das Ergebnis?
Nach dem Ende wird das Ergebnis **einmalig** per E-Mail an den Organisator gesendet:
Zeitraum, Beteiligung und Anzahl je Option. Es bleibt außerdem auf der
Detailseite einsehbar. Vorher ist es nicht abrufbar, auch nicht teilweise. Die
Bewertung, ab wann eine Kugelung als angenommen oder abgelehnt gilt, überlässt das
Tool bewusst der Loge. Es zeigt die genaue Zahl je Option.

### Kann ich eine Abstimmung abbrechen?
Ja. Ein Abbruch zeigt **nie** ein Ergebnis an, und die bis dahin abgegebenen
Stimmen werden gelöscht. Das ist Absicht: Sonst ließe sich eine Abstimmung nach
den ersten Stimmen stoppen, um das Zwischenergebnis zu sehen.

### Kann ich die Frist verlängern?
Ja, solange die Abstimmung noch läuft. Nach dem Ende ist das nicht mehr möglich. Sonst
ließe sich durch Verlängern und Nachladen weiterer Wähler eine einzelne Stimme aus
der Differenz ableiten.

### Wie lade ich nachträglich jemanden ein, oder wie stelle ich einen verlorenen Link neu aus?
Auf der Detailseite einer laufenden Abstimmung: weitere Adressen einfügen (bereits
eingeladene werden übersprungen) oder bei einer Person auf „Neuer Link" klicken.
Der alte Link wird dabei ungültig, und das funktioniert nur, solange die Person
noch nicht abgestimmt hat. Gespeichert wird nur eine Prüfsumme des Links; deshalb
lässt sich ein alter Link nicht noch einmal verschicken, nur ersetzen.

### Was ist, wenn eine Einladung nicht ankommt?
In der Teilnehmerliste sehen Sie pro Adresse, ob der Versand geklappt hat oder
fehlgeschlagen ist. Mit „Neuer Link" versenden Sie die Einladung erneut.

### Gibt es Erinnerungen?
Optional (Erweiterte Einstellungen): 24 Stunden vor Fristende erhalten alle, die noch
nicht abgestimmt haben, eine Erinnerung. Sie enthält einen **neuen** Link, der
frühere Link wird damit ungültig. Bei Abstimmungen, die ohnehin nur kurz laufen,
wird keine Erinnerung versendet.

### Kann ich die Empfängerliste wiederverwenden?
Ja. Die zuletzt verwendete Liste wird gespeichert und bei der nächsten Abstimmung
vorbefüllt. Beim Anlegen können Sie das abwählen. Im Dashboard lässt sich die Liste
jederzeit löschen.

## Nachprüfbarkeit

### Kann ich als Wähler prüfen, dass meine Stimme gezählt wurde?
Optional, vom Organisator einzuschalten (Erweiterte Einstellungen,
„Stimmen nachprüfbar machen"). Dann erhalten Wähler nach der Stimmabgabe einen
Quittungscode. Nach dem Ende erscheint eine Liste aller Codes mit der gezählten
Auswahl, in zufälliger Reihenfolge. So kann jeder prüfen, dass die eigene Stimme
enthalten und korrekt erfasst ist. Der Code ist keiner Person zugeordnet. Wer ihn
weitergibt, kann allerdings seine Stimme belegen. Ohne Aktivierung gibt es weder
Code noch Liste, und die Standardeinstellung ist aus.

## Datenschutz

### Welche Daten werden gespeichert, und wie lange?
- **Organisation:** Name der Loge und E-Mail-Adresse für den Login.
- **Einladungen:** E-Mail-Adresse, Versandstatus und „hat abgestimmt" je Wähler. Sie
  werden **30 Tage nach Abschluss oder Abbruch gelöscht** (einstellbar). Die Anzahl
  Eingeladener und Beteiligter bleibt als Zahl erhalten.
- **Stimmen:** Auswahl und Zufalls-ID, ohne Zeitstempel, ohne Personenbezug. Sie bleiben
  für das Ergebnis erhalten. Stimmen aus abgebrochenen Abstimmungen und aus Abstimmungen
  unter drei Stimmen werden sofort gelöscht.
- **Gespeicherte Empfängerliste:** bis Sie sie löschen.
- **Login-Links:** werden nach Ablauf aufgeräumt.

Die konkrete datenschutzrechtliche Bewertung (Verzeichnis der Verarbeitungstätigkeiten,
Auftragsverarbeitung bei Fremdhosting) hängt vom Betrieb ab und ist Sache des
Betreibers.

### Werden Daten an Dritte übertragen?
Nur die E-Mails über Ihren eigenen SMTP-Server. Es gibt keine Tracker, keine
externen Schriftarten oder Skripte.

## Betrieb

### Wie installiere ich es?
Mit Docker Compose, siehe README. Sie benötigen einen SMTP-Zugang und am besten
einen Reverse-Proxy mit HTTPS.

### Kann sich jeder als Loge registrieren?
Standardmäßig ja, und der Login-Link beweist nur den Zugriff auf die Adresse.
Setzen Sie `REQUIRE_VERIFICATION=true`, damit nur freigegebene Logen
Abstimmungen anlegen dürfen. Freigabe per
`docker compose exec app python -m app.cli verify loge@example.org`.
Login und Registrierung sind pro Adresse und IP rate-limitiert.

### Was, wenn der Server zwischendurch neu gestartet wird?
Alles liegt in der Datenbank. Ein Neustart verzögert höchstens den Abschluss und die
Ergebnis-Mail um Sekunden. Die Hintergrundprüfung läuft alle 30 Sekunden, und die
Detailseite schließt eine fällige Abstimmung auch selbst ab. Betreiben Sie genau
einen App-Prozess (ein uvicorn-Worker).
