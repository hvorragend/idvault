# 03 – Systemarchitektur

---

## 1 Überblick

idvault ist eine monolithische Web-Anwendung mit integriertem
Dateisystem-Scanner. Die Architektur folgt dem **Drei-Schichten-Modell**
(Presentation · Application · Persistence) und ist bewusst schlank
gehalten, um den Betrieb in restriktiven Banknetzen (ohne Internet, ohne
Container-Infrastruktur) zu ermöglichen.

```
┌───────────────────────────────────────────────────────────────┐
│                          Browser                              │
│                 (HTML + Bootstrap + JS)                       │
└───────────────────────┬───────────────────────────────────────┘
                        │ HTTPS (LDAPS-Analog für AD)
                        │
┌───────────────────────┴───────────────────────────────────────┐
│                     Presentation Layer                        │
│               Flask / Jinja2 / Blueprints                     │
│  auth · dashboard · idv · admin · funde · freigaben · ...     │
└───────────────────────┬───────────────────────────────────────┘
                        │
┌───────────────────────┴───────────────────────────────────────┐
│                     Application Layer                         │
│     db.py  ·  ldap_auth.py  ·  email_service.py  ·  ...       │
│     Business-Logik, Validierung, Workflow-Steuerung           │
└───────────────────────┬───────────────────────────────────────┘
                        │
┌───────────────────────┴───────────────────────────────────────┐
│                     Persistence Layer                         │
│                      SQLite (WAL)                             │
│              idv_register · idv_files · persons · ...         │
└───────────────────────────────────────────────────────────────┘

       ↑                           ↑                           ↑
       │                           │                           │
┌──────┴──────┐            ┌───────┴───────┐           ┌───────┴──────┐
│  Scheduler  │            │  Dateisystem  │           │  Active      │
│  (Windows   │            │  Scanner      │           │  Directory   │
│   Task)     │            │  idv_scanner  │           │  (LDAPS)     │
└─────────────┘            └───────────────┘           └──────────────┘
```

## 2 Technologiestack

| Schicht | Technologie | Version | Begründung |
|---|---|---|---|
| Sprache | Python | 3.10+ | LTS-Fähigkeit; breite Verfügbarkeit in Windows-Banken |
| Web-Framework | Flask | ≥ 3.0 | Minimalistisch, geeignet für eingebetteten Betrieb |
| Template-Engine | Jinja2 | via Flask | Auto-Escaping aktiv (XSS-Schutz) |
| Datenbank | SQLite | via Python-Stdlib | Keine Serverinstallation, WAL-Modus |
| Authentifizierung | ldap3 | ≥ 2.9 | LDAPS gegen Active Directory |
| Verschlüsselung | cryptography (Fernet) | ≥ 42.0 | AES-128-CBC, HMAC-SHA256 für Service-Passwörter |
| SSL/TLS | Python-Stdlib `ssl` | — | Selbstsigniert oder CA-signiert |
| Excel-Export | openpyxl | ≥ 3.1 | .xlsx-Generierung |
| WSGI-Server (Prod) | gunicorn | ≥ 21.0 | UNIX-Produktiv-Modus |
| Build | PyInstaller | build-req | Single-File-Executable |
| E-Mail | stdlib `smtplib` + `email` | — | Keine externen Abhängigkeiten |
| Frontend | Bootstrap 5 (über CDN/local) | — | Responsive UI |

## 3 Komponentenübersicht

### 3.1 Verzeichnisstruktur

```
idvault/
├── run.py                         Startpunkt, Sidecar-Updates, SSL
├── db.py                          Datenbank-Schicht (CRUD, Queries)
├── ssl_utils.py                   HTTPS/Zertifikats-Management
├── schema.sql                     Kompletter DDL-Skript
├── version.json                   Aktive Version + Changelog
├── requirements.txt               Produktionsabhängigkeiten
├── requirements-build.txt         Build-Zeit-Abhängigkeiten
├── idvault.spec                   PyInstaller-Konfiguration
├── webapp/
│   ├── __init__.py                Flask-Applikations-Fabrik (create_app)
│   ├── db_flask.py                Request-Kontext-Wrapper für db.py
│   ├── ldap_auth.py               LDAP-Bind + JIT-Provisioning
│   ├── login_logger.py            Audit-Logger für Login-Ereignisse
│   ├── email_service.py           SMTP-Versand
│   ├── routes/
│   │   ├── __init__.py            Decorators (login/admin/write_access_required)
│   │   ├── auth.py                Login/Logout
│   │   ├── dashboard.py           Kennzahlen-Übersicht
│   │   ├── idv.py                 IDV-CRUD, Liste, Detail, Export
│   │   ├── admin.py               Administration (50+ Routen)
│   │   ├── funde.py               Scanner-Eingang
│   │   ├── freigaben.py           Test- und Abnahmeverfahren
│   │   ├── measures.py            Maßnahmen
│   │   ├── reports.py             Auswertungen
│   │   ├── reviews.py             Prüfungen
│   │   └── tests.py               Test-Fälle
│   └── templates/                 Jinja2-Templates
└── scanner/
    ├── idv_scanner.py             Dateisystem-Scanner
    ├── teams_scanner.py           Microsoft-Teams-Scanner (optional)
    ├── idv_export.py              Standalone-Export
    ├── config.json                Scanner-Konfiguration
    └── requirements.txt           Scanner-Abhängigkeiten
```

### 3.2 Logische Komponenten

| Komponente | Aufgabe | Datei(en) |
|---|---|---|
| **Applikationsfabrik** | Flask-Instanz erzeugen, Blueprints registrieren, Logging konfigurieren | `webapp/__init__.py` |
| **Startpunkt** | Prozessstart, Umgebungsvariablen auswerten, SSL-Kontext, Sidecar-Loader | `run.py` |
| **Sidecar-Loader** | Lädt Python-Module aus `updates/` bevorzugt vor dem Bundle | `run.py:28-151` |
| **Datenbank-Schicht** | Verbindungsaufbau, PRAGMAs, Migrationen, Queries | `db.py` |
| **SSL-Utility** | Zertifikats-Generierung, SSL-Context-Bau | `ssl_utils.py` |
| **Authentifizierung** | LDAP-Bind, JIT-Provisioning, Rollenauflösung | `webapp/ldap_auth.py` |
| **Login-Audit** | Protokollierung aller Anmeldeversuche | `webapp/login_logger.py` |
| **E-Mail-Service** | SMTP-Versand, Template-Rendering | `webapp/email_service.py` |
| **Autorisierungs-Decorators** | login_required, admin_required, write_access_required, own_write_required | `webapp/routes/__init__.py` |
| **Blueprints** | Modulare Routing-Einheiten pro Funktionsbereich | `webapp/routes/*.py` |
| **Scanner (FS)** | SHA-256-Hash, Move-Detection, Excel-Analyse | `scanner/idv_scanner.py` |
| **Scanner (Teams)** | Graph-API-Abfragen, Delta-Tokens | `scanner/teams_scanner.py` |

## 4 Laufzeitmodelle

### 4.1 Standalone-Executable (Windows, primäres Deployment)

```
┌─────────────────────────────────────────────────────────────┐
│                    idvault.exe                              │
│  ┌───────────────┐   ┌─────────────────┐                    │
│  │ Python-Runtime│   │ Flask Dev-Server │                    │
│  │ (eingebettet) │   │ (Single-Prozess) │                    │
│  └───────────────┘   └─────────────────┘                    │
│         ↓                     ↓                             │
│  ┌──────────────────────────────────────┐                  │
│  │           Sidecar-Loader             │                  │
│  │    (updates/ vor Bundle laden)       │                  │
│  └──────────────────────────────────────┘                  │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│  instance/idvault.db       (SQLite WAL)                     │
│  instance/idvault.log      (Application Log)                │
│  instance/login.log        (Audit Log)                      │
│  instance/certs/cert.pem   (optional)                       │
│  instance/uploads/         (Nachweise)                      │
│  updates/                  (Sidecar-Updates)                │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 Entwicklungs-/Skriptbetrieb (Linux/macOS/Windows)

```
python run.py
    → webapp/__init__.py::create_app()
    → Blueprints laden
    → Flask dev-server auf :5000 (oder :5443 mit IDV_HTTPS=1)
```

### 4.3 Produktivbetrieb (Linux, optional)

```
gunicorn -w 4 -b 0.0.0.0:8000 "webapp:create_app()"
    → reverse proxy (nginx/Apache) davor
    → TLS-Terminierung im Proxy
```

### 4.4 Scanner-Betrieb (separat)

```
Windows Task Scheduler (wöchentlich)
    → idvault.exe --scan --config C:\idvault\scanner\config.json
        → scanner/idv_scanner.py::main()
        → scan_paths durchlaufen, Hashes berechnen
        → Ergebnisse in idv_files/idv_file_history schreiben
        → scan_runs-Eintrag erzeugen
```

## 5 Schnittstellen

### 5.1 HTTP(S) (eingehend)

- Port 5000 (HTTP, Default) oder 5443 (HTTPS, `IDV_HTTPS=1`)
- Authentifizierung: Session-Cookie nach Login
- Content-Type: `text/html`, `application/json` (AJAX), `application/octet-stream` (Downloads)

### 5.2 LDAP (ausgehend)

- Protokoll: LDAPS (TLS-Port 636)
- Bibliothek: `ldap3`
- Service-Bind mit technischem Benutzer, gefolgt von User-Bind zur Passwortprüfung
- Attribute: `sAMAccountName`, `givenName`, `sn`, `mail`, `telephoneNumber`, `memberOf`, `userAccountControl`

### 5.3 SMTP (ausgehend)

- Standardports: 587 (STARTTLS) oder 465 (SMTPS)
- Konfiguration: Datenbank (`app_settings`) oder Umgebungsvariablen (`IDV_SMTP_*`)
- Formatierung: HTML-E-Mails mit Plaintext-Fallback

### 5.4 Microsoft Graph API (ausgehend, optional)

- OAuth2 Client-Credentials-Flow via MSAL
- Endpunkte: `/v1.0/users`, `/v1.0/groups/{id}/drive`, Delta-Abfragen
- Nur aktiviert, wenn Teams-Scanner verwendet wird

### 5.5 Dateisystem (ausgehend, lesend)

- UNC-Pfade (`\\server\freigabe`) oder Laufwerksbuchstaben
- Nur Lese-Rechte erforderlich
- Hash-Berechnung mit SHA-256 (optional `xxhash` für größere Dateien)

## 6 Datenfluss

### 6.1 Typische IDV-Erfassung (Scanner → Register)

```
1. Scanner-Task läuft
     └─ scanner/idv_scanner.py → idv_files (Bearbeitungsstatus=Neu)

2. Koordinator öffnet /funde/eingang
     └─ Template listet neue Dateien

3. Klick "Als IDV registrieren"
     └─ POST /idv/neu?file_id=…
     └─ db.py::create_idv()
          ├─ INSERT idv_register
          ├─ INSERT idv_history (aktion=erstellt)
          └─ UPDATE idv_files SET bearbeitungsstatus='Registriert'

4. Statuswechsel Entwurf→Genehmigt
     └─ POST /idv/<id>/status
     └─ db.py::change_status()
          └─ INSERT idv_history (aktion=status_geaendert)

5. Regelprüfung
     └─ POST /reviews/neu/<idv_id>
     └─ INSERT pruefungen + UPDATE idv_register.naechste_pruefung
```

### 6.2 LDAP-Authentifizierung

```
1. POST /auth/login (username, password)
     └─ auth.py::login()
          ├─ ldap_auth.py::authenticate()
          │      ├─ LDAPS-Connection zum konfigurierten Server
          │      ├─ Bind mit Service-Account
          │      ├─ User-DN suchen (sAMAccountName)
          │      ├─ Bind mit User-DN + Passwort
          │      ├─ memberOf auslesen
          │      └─ Rolle aus ldap_group_role_mapping ableiten
          ├─ JIT-Provisioning: person anlegen/updaten
          ├─ Login protokollieren (login_logger.py)
          └─ Session setzen (user_id, user_role, person_id)
```

## 7 Sicherheitsarchitektur

Detailliert beschrieben in [05 – Sicherheitskonzept](05-sicherheitskonzept.md).
Architekturrelevante Eckpunkte:

- Session-State: Flask-Sessions (signiert, clientseitig)
- Authentifizierung: Zweistufig (LDAP bevorzugt, lokal fallback)
- Autorisierung: Decorator-basierte Role-Based Access Control
- Transportverschlüsselung: TLS (HTTPS, LDAPS)
- Datenverschlüsselung: Fernet für Service-Account-Passwörter
- Audit: separate Log-Datei für Logins, History-Tabelle für Datenänderungen

## 8 Deployment-Architektur

### 8.1 Empfohlene Infrastruktur

| Komponente | Empfehlung |
|---|---|
| Server | Windows Server 2019+ (oder Linux) |
| CPU | 2 Kerne |
| RAM | 4 GB |
| Festplatte | 10 GB (zzgl. Platz für Scanner-Daten) |
| Netz | Intern, Zugang zum AD, zu Fileshares, zum SMTP-Server |
| Reverse-Proxy (optional) | nginx, Apache, IIS, Traefik |

### 8.2 Netzsegmente

```
[Clients] ──HTTPS──→ [idvault-Server] ──LDAPS──→ [AD]
                           │
                           ├──SMTP──→ [Mailserver]
                           │
                           ├──SMB──→ [Fileshare 1..n]
                           │
                           └──HTTPS──→ [Microsoft Graph]   (optional)
```

### 8.3 Skalierbarkeitsgrenzen

| Ressource | Grenze |
|---|---|
| Anzahl IDVs | Praktisch unbegrenzt (SQLite) |
| Gleichzeitige Benutzer | Bis ~50 gleichzeitige Leser, bis ~5 gleichzeitige Schreiber (WAL) |
| Scan-Volumen | Bis 500.000 Dateien pro Scan (Checkpoint-fähig) |
| Migration PostgreSQL | Vorgesehen bei >50 gleichzeitigen Schreibern |

## 9 Logging-Architektur

| Logger | Datei | Rotation | Zweck |
|---|---|---|---|
| `idvault.log` | `instance/` | 1 MB × 7 | Anwendungs-Log (WARNING+) |
| `login.log` | `instance/` | 2 MB × 10 | Audit-Log für Logins |
| `idvault_crash.log` | `instance/` | 2 MB × 1 | Python-Traceback bei Start-Fehlern |
| `idv_scanner.log` | `scanner/` (konfigurierbar) | je Scanner-Run | Scan-Verlauf, Hash-Fehler |

## 10 Ausfallverhalten

| Ausfall | Verhalten |
|---|---|
| LDAP nicht erreichbar | Automatischer Fallback auf lokale Authentifizierung |
| SMTP nicht erreichbar | E-Mail-Versand scheitert stumm, Log-Eintrag in `idvault.log` |
| Netzlaufwerk nicht erreichbar | Scanner überspringt Pfad, `scan_runs.errors` wird inkrementiert |
| Datenbank-Fehler | 500-Fehler, Stacktrace in `idvault.log` und `idvault_crash.log` |
| Stromausfall | WAL-Journal erlaubt konsistenten Zustand nach Neustart |

## 11 Build- und Lieferarchitektur

Siehe [11 – Build & Deployment](11-build-deployment.md).

## 12 Begründete Entwurfsentscheidungen

| Entscheidung | Begründung |
|---|---|
| **SQLite statt PostgreSQL** | Zero-Configuration; keine zusätzliche Serverinstallation in Banknetzen |
| **PyInstaller statt Docker** | Keine Container-Laufzeit auf typischen Bank-Servern verfügbar |
| **Session-Cookies statt Tokens** | Browser-basierte UI; keine SPA |
| **Blueprints statt Microservices** | Monolith ausreichend für Fachanforderungen; geringere Betriebskomplexität |
| **Sidecar-Updates** | AppLocker-Kompatibilität (EXE-Hash bleibt gültig) |
| **ISO 8601 UTC-Zeitstempel** | Zeitzonensicherheit für internationale Prüfer |
| **JSON-Felder für Listen** | Vermeidung von n:m-Tabellen für einfache Tag-Listen |
