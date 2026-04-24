# idvault

Register für **Eigenentwicklungen** (inkl. IDVs nach **MaRisk AT 7.2**)
und **DORA** — entwickelt für Volksbanken, Sparkassen und sonstige
beaufsichtigte Kreditinstitute.

> **Begriffsklärung:** „Eigenentwicklung" ist in diesem System der
> Oberbegriff für alle erfassten Datenverarbeitungen (Arbeitshilfen,
> IDVs, Eigenprogrammierungen, Auftragsprogrammierungen). „IDV" bezeichnet
> ausschließlich das regulatorische Klassifikationsergebnis einer
> Eigenentwicklung nach MaRisk AT 7.2.

---

## Was ist idvault?

idvault ist eine in sich geschlossene Webanwendung zur vollständigen,
aufsichtsrechtlich konformen Erfassung, Klassifizierung, Prüfung und
Überwachung aller Eigenentwicklungen der Bank:

- **Scanner** identifiziert Kandidaten für Eigenentwicklungen auf Netzlaufwerken und in Microsoft Teams
- **Register der Eigenentwicklungen** dokumentiert Wesentlichkeit, Risiko, DORA-Kritikalität, Verantwortliche
- **Workflow** bildet Entwurf → Prüfung → Freigabe → Archiv ab (inklusive 4-Augen-Prinzip und Funktionstrennung)
- **Prüfungen & Maßnahmen** verfolgen Regelprüfungen und deren Befunde
- **Test-, Freigabe- und Archivierungsverfahren** mit 5 Schritten in 3 Phasen für wesentliche Eigenentwicklungen (inkl. revisionssicherer Archivierung der Originaldatei mit SHA-256-Prüfsumme; dokumentierte Nicht-Verfügbarkeit z.B. bei Cognos-Berichten)
- **LDAP-Integration** gegen Active Directory mit Gruppen-Rollen-Mapping
- **Audit-Trail** auf Tabellen- und Login-Ebene
- **Export** nach Excel für Revision und Aufsicht

Die Anwendung benötigt keine zusätzliche Serverinfrastruktur und kann als
einzelne ausführbare Datei (`idvault.exe`) betrieben werden – direkt oder
als nativer Windows-Dienst (`idvault.exe install`).

## Feature-Überblick

### Scanner für Eigenentwicklungen

- **Netzlaufwerk- und Teams/SharePoint-Scanner** mit konfigurierbaren
  Include-/Exclude-Pfaden, Whitelist/Blacklist für Ordner und Dateinamen
  sowie erweiterten Standardmustern (Temp-Dateien, Backups, System-Ordner).
- **UNC-zu-Laufwerksbuchstaben-Mapping** — Findings werden mit dem im
  Fachbereich üblichen Pfad angezeigt, nicht mit dem UNC-Pfad des
  Service-Users.
- **Scanner als technischer AD-Benutzer**, sodass auch Laufwerke
  gescannt werden, auf die der Anwendungs-Service keinen Zugriff hat.
  Anmeldung läuft über `WNetAddConnection2` (ohne EXE-Neubau sidecar-fähig).
- **Geplante Scans** (Cron-ähnlicher Zeitplan), manueller Scan-Start
  aus der Topbar, Live-Scan-Log im Admin-Bereich.
- **Automatische Klassifizierung** gescannter Dateien nach
  Dateinamen-Präfix/-Suffix oder Regex — konfigurierbar pro
  Organisationseinheit (OE-Scope).
- **Drei Auto-Link-Pfade für Funde**:
  1. Konfidenz-Staffel über Ähnlichkeitsanalyse (konfigurierbar),
  2. SHA-256-Hash-Dubletten als automatischer Zusatz-Link,
  3. Versions-Serien-Fingerprint (z.B. „Meldung_2024Q1.xlsx" / „Meldung_2024Q2.xlsx").
- **Bulk-Operationen**: Mehrfach-Zuordnung, Bulk-Löschen für Admins,
  Mehrfach-Ignorieren, sortierbare Prioritätsliste.
- **Pfad-Profile** mit Admin-CRUD-UI bilden typische Ablagepfade
  (z. B. `\\server\controlling\Risiko`) auf Verantwortliche und OE ab
  und füllen Masken vor.

### Self-Service für Fachbereiche

- **Schlankere IDV-Anlage**: Pflichtfelder konzentriert im Entwurf,
  **Vollständigkeits-Gauge** zeigt Fortschritt vor Einreichung.
- **Self-Service-Bulk-Registrierung**: Mehrere Funde in einem Schritt
  zu Eigenentwicklungen machen.
- **Anonyme Quick-Action-Links (Magic-Link)** für alle Freigabe-Schritte
  — keine Anmeldung nötig, wenn die Aufgabe an einen externen
  Fachverantwortlichen gegeben wird.
- **Dreistufige Eskalations-Automatik** bei Self-Service-Links
  (Erinnerung → Eskalation an Vertreter → Eskalation an IDV-Koordinator).
- **Owner-Mail-Digest**: Neue Scanner-Funde werden dem voraussichtlichen
  Owner in einer Sammelmail zugestellt, mit Sofort-Schwelle für
  risikorelevante Funde.
- **Pool-Claim**: Mehrere Freigabe-Verantwortliche teilen sich einen
  Pool, erhalten eine Benachrichtigung und einen täglichen Reminder.

### IDV-Register

- **Dynamische Wesentlichkeitskriterien** mit Detail-Checkboxen —
  Kriterienkatalog konfigurierbar, nicht hart im Code verdrahtet.
- **Konfigurierbare Klassifizierungs-Regeln** mit Prefix-, Suffix-
  und Regex-Matching pro Organisationseinheit.
- **Versionierung**: Jede Änderung erzeugt eine neue Version,
  Freigabe-Einstufung wird nicht ungeprüft übernommen.
- **Geschäftsprozess- und OE-Zuordnung** mit durchsuchbarer Combobox.
- **Datei-Metadaten** (Hash, Änderungsdatum, Besitzer, Größe)
  werden aus dem Scanner-Fund übernommen.

### Test-, Freigabe- und Archivierungsverfahren (3 Phasen)

- **Phase 1 – Fachliche Konzeption & Test**: Testfälle aus einer
  konfigurierbaren **Testfall-Vorlagen-Bibliothek** (OE- und
  klassifikationsbezogen), darunter regulatorisch konforme
  Standard-Vorlagen für Excel und Cognos.
- **Phase 2 – Technischer Test & Freigabe**: Technischer Tester
  erhält Scanner-Metadaten als Prefill, **Prüfzeugnis der technischen
  Abnahme** wird automatisch erzeugt. Bewusste Akzeptanz
  „kein Zell-/Blattschutz" mit Begründung möglich, inkl. Report
  für Excel-Dateien ohne Zell-/Blattschutz.
- **Phase 3 – Ein-Klick-Archivierung**: Originaldatei wird
  revisionssicher eingefroren, SHA-256-Abgleich gegen die getestete
  Version verhindert nachträgliche Manipulation.
- **Verschlankter Patch-Workflow** für kleinere Versionsänderungen.
- **Stille Freigabe** für nicht-wesentliche Eigenentwicklungen
  (automatischer Statuswechsel ohne separate Genehmigung).
- **Funktionstrennung (SoD)**: Entwickler einer IDV ist von
  Abschluss-/Ablehnungshandlungen im Freigabeverfahren ausgeschlossen,
  Test-Formular-Pfad ist gegen SoD-Umgehung abgesichert.

### Dashboard, Berichte & Reporting

- **Prozesskennzahlen-Sektion** mit konfigurierbaren KPI-Kacheln
  (Durchlaufzeiten, Rückläufe, überfällige Maßnahmen) — Excel-Export
  für Aufsichts-Reporting.
- **Ausnahmen-Dashboard** für den IDV-Koordinator: zeigt abgelaufene
  Freigaben, fehlende Prüfzeugnisse, nicht-zugeordnete Funde,
  Eskalationen.
- **Berichte & Auswertungen** mit ApexCharts-Visualisierung
  (Donut- und Stacked-Bar-Diagramme für Statusverteilung, Entwicklung
  über die letzten Monate) und Tab-Navigation nach
  **Organisationseinheit**, **Fachverantwortlichem** und
  **Scan-Verzeichnis / Teilscan**.
- **Excel-Export** aller Register-, Prüfungs- und Auswertungsdaten.

### Cognos / agree21Analysen-Integration

- **Import der Berichtsübersicht** (TSV/CSV/XLSX) mit automatischem
  Mapping der Spaltenköpfe (Umfeld, Bank-ID, Bericht, Ordner, …).
- **„Als IDV registrieren"** direkt aus der Berichtsliste
  (Einzel- oder Bulk-Aktion).
- **Zusammenfassen** mehrerer Cognos-Berichte zu einer einzelnen
  Eigenentwicklung.
- **Ignorieren / Reaktivieren** irrelevanter Berichte.

### Prüfungen & Maßnahmen

- **Regelprüfungen** dokumentieren den Prüfzyklus einer IDV
  (letzte Prüfung, nächste Prüfung, Prüfungsergebnis, Befunde).
- **Maßnahmen** werden aus Befunden abgeleitet, Zuständigen
  zugewiesen und bis zur Erledigung nachverfolgt.
- **Nachweis-Upload** (Rich-Text + Dateianhang) für fachlichen
  und technischen Test, Path-Traversal- und Ownership-sicher
  ausgeliefert.

### Stammdatenverwaltung

- **CRUD-UIs** für Personen, Organisationseinheiten, Geschäftsprozesse,
  Plattformen, Klassifizierungen, Wesentlichkeitskriterien,
  Pfad-Profile, Testfall-Vorlagen, Freigabe-Pools.
- **CSV-Import** für Mitarbeiter und Geschäftsprozesse inkl.
  herunterladbarer Import-Vorlage.
- **Bulk-Aktionen** für Personen und Geschäftsprozesse
  (Aktivieren, Deaktivieren, Rolle setzen, Löschen).
- **Konfigurierbares Glossar** mit Admin-UI: Abgrenzung
  Anwendungsentwicklung / Eigenprogrammierung / Auftragsprogrammierung /
  IDV / Arbeitshilfe, direkt in der Anwendung pflegbar.

### Administration & Betrieb

- **Lokale Benutzer in `config.json`** oder LDAP (Active Directory)
  mit Gruppen-Rollen-Mapping inkl. LDAP-Testverbindung und
  LDAP-Benutzer-Import. Lokaler Notfall-Admin bleibt auch bei
  LDAP-Ausfall möglich.
- **SMTP** mit drei Verbindungsmodi (STARTTLS / SSL / kein TLS),
  Testversand aus der Admin-UI, vollständiges Versandlog,
  Passwort Fernet-verschlüsselt in der DB.
- **Natives Windows-Dienst-Framework** (pywin32 ServiceFramework)
  mit automatischem Dienstneustart nach Update, EnumServicesStatusEx-
  basierter Dienst-Erkennung und erweiterten Start-Diagnosen.
- **Update-Workflow** per signiertem Sidecar-ZIP mit
  **Rollback-Funktion** und Update-Log — in regulierten Umgebungen
  vollständig abschaltbar.
- **Scanner-Steuerung** aus dem Admin-Bereich: Starten, Pausieren,
  Fortsetzen, Abbrechen, Bereinigen; Live-Status und konfigurierbarer
  Scan-User (Run-As mit Test-Verbindung).
- **Log-Viewer** in der Web-UI: Anwendungslog, Scan-Log,
  Crash-Log, Login-Log, Update-Log, Mail-Versandlog — mit Suche
  und Filter.
- **Rate-Limits** für Login, Upload und Scanner konfigurierbar
  unter `/admin/rate-limits`.
- **Steuerbare Standardanzeige** („Suche & Filter") und UI-
  Einstellungen über die Admin-Oberfläche.
- **Testinstallation** per `IDV_DEMO_DATA=true` (Stammdaten,
  Beispiel-Personen, Beispiel-IDVs, Prüfungen und Maßnahmen).

### UX / Frontend

- Einheitliches Layout mit Breadcrumb-Topbar, Avatar-Chip,
  Pill-Badges, KPI-Cards und KPI-Shadow für Scan-Seiten.
- Tailwind-artige Utility-Klassen in einer zentralen `idvault.css`,
  eckiger Look, dedizierte Druck- und Reduced-Motion-Stylesheets.
- Kollabierbare Filter, Bulk-Action-Bar, sortierbare Tabellen,
  Sidebar mit anklickbarem Logo.
- **Vollständig Offline-fähig**: Bootstrap, Bootstrap Icons und
  QuillJS werden lokal aus `webapp/static/vendor/` ausgeliefert.

## Schnellstart

```bash
pip install -r requirements.txt
python run.py
# → http://localhost:5000
```

Beim ersten Start wird `config.json` mit einem zufälligen `SECRET_KEY`
automatisch angelegt. Es gibt **keine Demo-/Default-Benutzer** mehr —
lokale Benutzer werden ausschließlich in `config.json` unter
`IDV_LOCAL_USERS` deklariert (oder kommen über LDAP). Zwei Varianten:

```jsonc
"IDV_LOCAL_USERS": [
  // Variante A (empfohlen): werkzeug-Hash
  { "username": "admin",
    "password_hash": "pbkdf2:sha256:600000$…$…",
    "role": "IDV-Administrator" },

  // Variante B (bequem, z.B. Erstinstallation):
  // Klartext-Passwort – wird beim Start automatisch gehasht
  { "username": "koordinator",
    "password": "bitte-aendern",
    "role": "IDV-Koordinator" }
]
```

Hash erzeugen mit:
```bash
python -c "from werkzeug.security import generate_password_hash; \
           print(generate_password_hash('mein-passwort', method='pbkdf2:sha256'))"
```

Für eine Testinstallation mit Beispieldaten `IDV_DEMO_DATA` auf `true` setzen:

```jsonc
"IDV_DEMO_DATA": true
```

Beim ersten Start werden dann Stammdaten (Personen, OEs, Geschäftsprozesse,
Beispiel-IDVs, Prüfungen, Maßnahmen) einmalig eingespielt. Im Produktivbetrieb
bleibt der Wert auf `false`.

Vollständiges Beispiel: [`config.json.example`](config.json.example).
Ohne lokalen Benutzer und ohne LDAP ist nach dem Start kein Login
möglich — bewusst, um Default-Credentials auszuschließen.
Klartext-Passwörter in `config.json` sind optional zulässig; in diesem
Fall muss die Datei per NTFS-ACL bzw. Unix-Berechtigung (`0640`) auf
den Service-User eingeschränkt werden.

Für die Standalone-EXE siehe [docs/11-build-deployment.md](docs/11-build-deployment.md).

## Dokumentation

Die vollständige Dokumentation liegt im Ordner **[`docs/`](docs/)** und
gliedert sich wie folgt:

| Dokument | Zielgruppe |
|---|---|
| [01 – Anwendungsdokumentation](docs/01-anwendungsdokumentation.md) | Fachbereich, Anwender |
| [02 – Pflichtenheft](docs/02-pflichtenheft.md) | Entwicklung, Auftraggeber |
| [03 – Architektur](docs/03-architektur.md) | Architekten, Revision |
| [04 – Datenmodell](docs/04-datenmodell.md) | Entwickler, DBA |
| [05 – Sicherheitskonzept](docs/05-sicherheitskonzept.md) | IT-Sicherheit |
| [06 – Betriebshandbuch](docs/06-betriebshandbuch.md) | Betrieb, Administratoren |
| [07 – Aufsichtsrechtliche Konformität](docs/07-aufsichtsrecht.md) | Revision, Prüfer |
| [08 – Quellcodeanalyse](docs/08-quellcodeanalyse.md) | Revision, IT-Sicherheit |
| [09 – Schwachstellenanalyse](docs/09-schwachstellenanalyse.md) | IT-Sicherheit |
| [10 – Scanner](docs/10-scanner.md) | Administratoren |
| [11 – Build & Deployment](docs/11-build-deployment.md) | Entwicklung, Betrieb |
| [12 – Glossar](docs/12-glossar.md) | Alle |

Einstiegspunkt und Inhaltsverzeichnis: [`docs/README.md`](docs/README.md).

## Technologie

| Schicht | Technologie |
|---|---|
| Sprache | Python 3.10+ |
| Web-Framework | Flask, Jinja2 |
| Datenbank | SQLite (WAL) |
| Authentifizierung | LDAP (ldap3) + lokale Benutzer aus `config.json` |
| CSRF / Rate-Limit | Flask-WTF (`CSRFProtect`), Flask-Limiter |
| HTML-Sanitizing | bleach (Stored-XSS-Schutz für Rich-Text-Felder) |
| Verschlüsselung | cryptography (Fernet) |
| Build | PyInstaller (Single-File-EXE) |
| Export | openpyxl (XLSX) |
| Frontend | Bootstrap 5.3.3, Bootstrap Icons 1.11.3, QuillJS 1.3.7 — **lokal ausgeliefert**, keine CDN-/Internet-Verbindung nötig |

Siehe [docs/03-architektur.md](docs/03-architektur.md) für Details.

> **Offline-Betrieb:** Alle Frontend-Assets (CSS, JS, Icon-Fonts) liegen unter
> `webapp/static/vendor/` und werden von Flask direkt ausgeliefert. Die
> Anwendung funktioniert vollständig in Netzen ohne Internet-Zugang
> (z. B. segmentierte Bank-Netze). Bezug/Upgrade der Vendor-Assets:
> `python scripts/download_vendor_assets.py`.

## Regulatorische Einordnung

idvault unterstützt die Umsetzung folgender Anforderungen:

- **MaRisk AT 7.2 Tz. 7** – IDV-Register, Klassifizierung, Prüfungen, Freigabeverfahren
- **DORA Art. 8 / 17** – Identifikation kritischer Funktionen, Incident-Management
- **DSGVO Art. 32** – Technisch-organisatorische Maßnahmen
- **HGB § 239 / § 257** – Ordnungsmäßigkeit und Aufbewahrung

Vollständiges Compliance-Mapping: [docs/07-aufsichtsrecht.md](docs/07-aufsichtsrecht.md).

## Sicherheitshinweise für den Produktivbetrieb

Bereits umgesetzte Hardening-Maßnahmen (Details: [docs/09-schwachstellenanalyse.md](docs/09-schwachstellenanalyse.md)):

**Authentifizierung & Session**

- ✅ Modernes Passwort-Hashing (`pbkdf2:sha256`) mit automatischer Migration von Legacy-SHA-256-Hashes
- ✅ Keine Demo-/Default-Benutzer im Quellcode — lokale Benutzer ausschließlich über `IDV_LOCAL_USERS` in `config.json` (werkzeug-Hash empfohlen, Klartext-Passwort optional und wird beim Start automatisch gehasht)
- ✅ Rate-Limiting am Login (Flask-Limiter, konfigurierbar unter `/admin/rate-limits`, Default 5/min, 30/h)
- ✅ Logout nur per POST + CSRF-Token
- ✅ Session-Idle-Timeout 4 h + `HttpOnly` / `SameSite=Lax` / `Secure` (automatisch bei HTTPS)

**Anfragen-Härtung**

- ✅ CSRF-Schutz (Flask-WTF `CSRFProtect`) für alle 77 POST-Formulare; AJAX-Wrapper setzt automatisch `X-CSRFToken`-Header
- ✅ HTTP-Security-Header per `after_request`: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`, HSTS (bei HTTPS)
- ✅ Nonce-basiertes CSP — `script-src 'self' 'nonce-…'` ohne `unsafe-inline`; alle inline Event-Handler auf Event-Delegation umgestellt
- ✅ Eingabelängen-/Steuerzeichen-Validierung als globaler `before_request`-Hook (`IDV_LOCAL_USERS`-konforme Längen, CR/LF-Block in Single-Line-Feldern)
- ✅ Upload-Rate-Limit (über `/admin/rate-limits`, Default 10/min, 60/h) auf ZIP-Update und CSV-Importe

**Daten-Härtung**

- ✅ Stored-XSS-Schutz: `nachweise_text` aus QuillJS wird mit `bleach` sanitiert (strikte Tag-/Attribut-Whitelist)
- ✅ Path-Traversal/IDOR am Nachweis-Download behoben — Downloads werden per ID + Ownership-Check ausgeliefert
- ✅ Broken Access Control behoben — `ensure_can_read_idv` / `ensure_can_write_idv` in allen schreibenden Eigenentwicklungs-/Tests-/Reviews-/Measures-/Freigaben-Routen
- ✅ Upload-Magic-Byte-Validierung — verhindert polyglot-Uploads (z.B. SVG getarnt als PNG)
- ✅ Admin-RCE-Vektor (Sidecar-ZIP-Update) über `Administration → Update` deaktivierbar (app_settings-Schalter)
- ✅ SMTP-Passwort Fernet-verschlüsselt in der Datenbank

**Konfiguration & Betrieb**

- ✅ `SECRET_KEY`-Enforcement: ohne `SECRET_KEY` und nicht im DEBUG-Modus bricht der Start ab (`run.py`); beim ersten Start wird `config.json` mit zufälligem Key auto-generiert
- ✅ Warnung, wenn Debug-Modus aktiv ist
- ✅ LDAP: Warnung bei deaktivierter Zertifikatsprüfung (UI + Log)
- ✅ Konkrete Exception-Typen + Logging in kritischen Pfaden (Auth, Bulk-Deletes, SMTP-Verschlüsselung, E-Mail-Notification)

Noch offene Punkte vor bzw. kurz nach Produktivstart:

- [ ] HTTPS aktivieren (direkt via `IDV_HTTPS=1` oder per Reverse-Proxy)
- [ ] `SECRET_KEY` aus KeyVault/HSM beziehen (Betriebsauflage)
- [ ] In regulierten Umgebungen: Sidecar-Updates in der Admin-Oberfläche (`Administration → Update`) deaktivieren und Updates ausschließlich über signierte EXE-Builds einspielen
- [ ] **Single-Process-Betrieb beibehalten.** Die Anwendung verwendet einen
      In-Process-Writer-Thread (`webapp/db_writer.py`), um SQLite-Locks unter
      parallelem Scan + Web-Traffic zu vermeiden. Mehrere Worker-Prozesse
      (gunicorn `--workers >1`, uwsgi `--processes >1`) bringen die
      `database is locked`-Race zurück — **immer `--workers 1`** verwenden
      und stattdessen Threads skalieren (gunicorn `--threads`, waitress
      `threads=`, cheroot `numthreads=`). Bei Multi-Worker-Deployment
      zusaetzlich: Flask-Limiter-Storage auf Redis umstellen.
- [ ] Externer Penetrationstest beauftragen
- [ ] Test-Suite (pytest) mit ≥ 70 % Abdeckung der Kernlogik (Sprint 4)

Vollständige Pre-Go-Live-Checkliste: [docs/05-sicherheitskonzept.md](docs/05-sicherheitskonzept.md) Abschnitt 7.

## Copyright

Copyright &copy; 2026 **Volksbank Gronau-Ahaus eG** und
**Carsten Volmer** (Entwicklung). Alle Rechte vorbehalten.

## Lizenz und Support

**Proprietäre Software – kein Open Source.** Alle Rechte liegen
gemeinschaftlich bei der Volksbank Gronau-Ahaus eG und Carsten Volmer.
Das öffentliche Repository auf GitHub dient ausschließlich dem Hosting
und räumt durch Klonen oder Forken keine Nutzungsrechte ein.

Nutzung, Vervielfältigung, Modifikation und Weitergabe sind
ausschließlich auf Grundlage eines entgeltlichen Lizenzvertrages
zulässig; eine zeitlich befristete Evaluierung (30 Tage, nicht
produktiv) ist gestattet. Vollständige Bedingungen: [`LICENSE`](LICENSE).

Lizenzanfragen und Fachsupport: IDV-Koordinator der Volksbank
Gronau-Ahaus eG. Issue-Tracking:
[GitHub](https://github.com/hvorragend/idvault).
