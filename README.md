# idvault

Register für **Eigenentwicklungen** (inkl. IDVs nach **MaRisk AT 7.2**),
**DORA** und **BAIT** — entwickelt für Volksbanken, Sparkassen und sonstige
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
- **Workflow** bildet Entwurf → Prüfung → Genehmigung → Archiv ab (inklusive 4-Augen-Prinzip)
- **Prüfungen & Maßnahmen** verfolgen Regelprüfungen und deren Befunde
- **Test-, Freigabe- und Archivierungsverfahren** mit 5 Schritten in 3 Phasen für wesentliche Eigenentwicklungen (inkl. revisionssicherer Archivierung der Originaldatei mit SHA-256-Prüfsumme; dokumentierte Nicht-Verfügbarkeit z.B. bei Cognos-Berichten)
- **LDAP-Integration** gegen Active Directory mit Gruppen-Rollen-Mapping
- **Audit-Trail** auf Tabellen- und Login-Ebene
- **Export** nach Excel für Revision und Aufsicht

Die Anwendung benötigt keine zusätzliche Serverinfrastruktur und kann als
einzelne ausführbare Datei (`idvault.exe`) betrieben werden – direkt oder
als nativer Windows-Dienst (`idvault.exe install`).

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
- **BAIT Kap. 4 und 10** – Berechtigungsverwaltung, IDV-Behandlung
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

## Lizenz und Support

Entwickelt für bankinterne Verwendung. Ansprechpartner für
Fachanfragen: IDV-Koordinator der Bank. Issue-Tracking:
[GitHub](https://github.com/hvorragend/idvault).
