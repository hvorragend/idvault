# idvault

Register für **Individuelle Datenverarbeitungen (IDV)** nach
**MaRisk AT 7.2**, **DORA** und **BAIT** — entwickelt für Volksbanken,
Sparkassen und sonstige beaufsichtigte Kreditinstitute.

---

## Was ist idvault?

idvault ist eine in sich geschlossene Webanwendung zur vollständigen,
aufsichtsrechtlich konformen Erfassung, Klassifizierung, Prüfung und
Überwachung aller Individuellen Datenverarbeitungen der Bank:

- **Scanner** identifiziert IDV-Kandidaten auf Netzlaufwerken und in Microsoft Teams
- **IDV-Register** dokumentiert Wesentlichkeit, Risiko, DORA-Kritikalität, Verantwortliche
- **Workflow** bildet Entwurf → Prüfung → Genehmigung → Archiv ab (inklusive 4-Augen-Prinzip)
- **Prüfungen & Maßnahmen** verfolgen Regelprüfungen und deren Befunde
- **Test- und Freigabeverfahren** mit 4 Schritten in 2 Phasen für wesentliche IDVs
- **LDAP-Integration** gegen Active Directory mit Gruppen-Rollen-Mapping
- **Audit-Trail** auf Tabellen- und Login-Ebene
- **Export** nach Excel für Revision und Aufsicht

Die Anwendung benötigt keine zusätzliche Serverinfrastruktur und kann als
einzelne ausführbare Datei (`idvault.exe`) betrieben werden.

## Schnellstart

```bash
pip install -r requirements.txt
python run.py
# → http://localhost:5000
# → Erstlogin: admin / idvault2026   (vor Produktiveinsatz deaktivieren!)
```

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
| Authentifizierung | LDAP (ldap3) + lokale Fallback-Authentifizierung |
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

- ✅ Modernes Passwort-Hashing (`pbkdf2:sha256`) mit automatischer Migration von Legacy-SHA-256-Hashes
- ✅ `SECRET_KEY`-Enforcement: beim ersten Start wird `config.json` mit zufälligem Key auto-generiert; ohne Key und ohne `config.json` bricht die Anwendung ab
- ✅ Warnung, wenn Debug-Modus aktiv ist
- ✅ SMTP-Passwort Fernet-verschlüsselt in der Datenbank
- ✅ HTTP-Security-Header (CSP, X-Frame-Options, HSTS) per `after_request`
- ✅ LDAP: Warnung bei deaktivierter Zertifikatsprüfung (UI + Log)
- ✅ Session-Idle-Timeout 4 h + HttpOnly/SameSite/Secure

Noch offene Punkte vor bzw. kurz nach Produktivstart:

- [ ] CSRF-Schutz (Flask-WTF) einführen
- [ ] Rate-Limiting am Login (Flask-Limiter)
- [ ] HTTPS aktivieren (direkt oder per Reverse-Proxy)
- [ ] `SECRET_KEY` aus KeyVault/HSM

Demo-Zugänge (`admin / idvault2026` u. a.) bleiben auf Wunsch
des Auftraggebers **aktiv**; das Restrisiko ist in
[docs/09-schwachstellenanalyse.md Abschnitt 3.3](docs/09-schwachstellenanalyse.md)
dokumentiert.

Vollständige Pre-Go-Live-Checkliste: [docs/05-sicherheitskonzept.md](docs/05-sicherheitskonzept.md) Abschnitt 7.

## Lizenz und Support

Entwickelt für bankinterne Verwendung. Ansprechpartner für
Fachanfragen: IDV-Koordinator der Bank. Issue-Tracking:
[GitHub](https://github.com/hvorragend/idvault).
