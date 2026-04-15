# 11 – Build und Deployment

---

## 1 Quellcodebezug

- Repository: `hvorragend/idvault` (GitHub)
- Entwicklungszweig: `main`
- Ausliefer-Tags: `vX.Y.Z` (semantic versioning)
- Lizenzierung: intern

## 2 Build-Artefakt

Das primäre Auslieferartefakt ist ein **Single-File-Executable** für
Windows (`idvault.exe`), erstellt mit **PyInstaller**. Alternative
Ausliefervariante ist die Quell-Installation.

### 2.1 Voraussetzungen für den Build

| Komponente | Version |
|---|---|
| Python | 3.10 oder neuer |
| PyInstaller | aus `requirements-build.txt` |
| Produktionsabhängigkeiten | aus `requirements.txt` |
| Optional Windows-spezifisch | `pywin32` (Dateiinhaber), `xxhash` (Hashing-Performance) |

### 2.2 Build-Ablauf

```cmd
REM Ins Projektverzeichnis wechseln
cd C:\idvault

REM Abhängigkeiten installieren
pip install -r requirements.txt
pip install -r requirements-build.txt

REM EXE bauen
python -m PyInstaller idvault.spec --clean --noconfirm

REM Ergebnis
dist\idvault.exe
```

Der Build dauert typischerweise 1–3 Minuten. Die erzeugte EXE ist
eigenständig lauffähig und benötigt keine lokale Python-Installation.

### 2.3 PyInstaller-Spec

`idvault.spec` steuert den Build-Prozess:

- Einstiegspunkt: `run.py`
- Zu bündelnde Ressourcen: `schema.sql`, `webapp/templates/`, `webapp/static/`, `version.json`, `scanner/`
- Zu bündelnde Bibliotheken: `flask`, `openpyxl`, `ldap3`, `cryptography`, `msal`, `requests`, ggf. `pywin32`
- `debug=False` (keine Debug-Symbole in der EXE)

### 2.4 Frontend-Assets (offline-fähig)

Bootstrap, Bootstrap Icons und QuillJS werden **nicht** von einem CDN geladen,
sondern liegen lokal unter `webapp/static/vendor/` und sind im Repository
eingecheckt. Das ist Voraussetzung für den Betrieb in Netzen ohne
Internet-Zugang (z. B. segmentierten Bank-Netzen) und für eine restriktive
Content-Security-Policy (`script-src 'self'`, `style-src 'self'`,
`font-src 'self'` — keine `cdn.*`-Einträge).

Vor einem Build sollten die Assets mit

```cmd
python scripts/download_vendor_assets.py --check
```

verifiziert werden. Fehlende Dateien werden mit

```cmd
python scripts/download_vendor_assets.py
```

von den offiziellen Release-Archiven (GitHub, npm) nachgeladen. PyInstaller
bündelt anschließend das gesamte Verzeichnis `webapp/static/` in die EXE.

### 2.5 Signierung (Empfehlung für Produktion)

Die erzeugte EXE sollte mit einem Code-Signing-Zertifikat der Bank
signiert werden (zur Vermeidung von SmartScreen-/AppLocker-
Warnungen):

```cmd
signtool sign /f bank-codesign.pfx /p <password> /tr http://timestamp.digicert.com /td sha256 /fd sha256 dist\idvault.exe
```

### 2.6 Prüfsummen

Nach jedem Build:

```cmd
certutil -hashfile dist\idvault.exe SHA256
```

Die Prüfsumme ist im Release-Dokument zu hinterlegen. AppLocker-Regeln
werden gegen genau diesen Hash erstellt.

## 3 Sidecar-Update-Mechanismus

### 3.1 Motivation

In Umgebungen mit AppLocker oder strengen Whitelisting-Regeln darf die
EXE-Binärdatei nicht ersetzt werden, da dies die Hash-Ausnahme ungültig
macht. idvault umgeht dies, indem Updates **neben** der EXE abgelegt
werden und beim Start bevorzugt geladen werden.

### 3.2 Funktionsweise

```
idvault.exe          ← unveränderlich, AppLocker-Hash bleibt gültig
instance/
  idvault.db         ← Laufzeit-Datenbank
updates/             ← wird beim Update-Import angelegt
  version.json       ← aktive Versionsinformation
  webapp/
    routes/
      admin.py       ← überschreibt die gebündelte Datei
  templates/
    admin/
      update.html    ← überschreibt das Template
```

Der Sidecar-Loader (`run.py:28–151`) installiert einen Meta-Path-Finder,
der Python-Module aus dem `updates/`-Verzeichnis vor den im EXE-Bundle
enthaltenen Modulen lädt.

### 3.3 Update-ZIP-Format

```
update-v0.2.0.zip
├── version.json                 ← Pflicht (Versionsinformation + Changelog)
├── webapp/                      ← Python-Module (spiegelt Projektstruktur)
│   ├── __init__.py
│   ├── routes/
│   │   ├── admin.py
│   │   └── idv.py
│   └── email_service.py
└── templates/                   ← Templates (NICHT webapp/templates/ !)
    ├── admin/
    │   └── update.html
    └── idv/
        └── list.html
```

### 3.4 GitHub-ZIP direkt verwendbar

```
https://github.com/hvorragend/idvault/archive/refs/heads/main.zip
```

Die Anwendung erkennt das `idvault-main/`-Präfix automatisch, überspringt
irrelevante Dateien (`.md`, `.gitignore`, `.spec`) und mappt
`webapp/templates/` korrekt auf `templates/`.

### 3.5 Sicherheit der Update-Annahme

| Kontrolle | Umsetzung |
|---|---|
| Nur Administrator-Rolle | `@admin_required`-Decorator |
| Whitelist Dateitypen | `.py`, `.html`, `.json`, `.sql`, `.css`, `.js` |
| Path-Traversal-Schutz | `..`-Detection, Normalisierung |
| Maximale Uploadgröße | 32 MB |
| Rollback per Klick | `Admin → Software-Update → Rollback` |

### 3.6 Versionsanzeige

| Bezeichnung | Bedeutung |
|---|---|
| Gebündelte Version | Version, mit der die EXE gebaut wurde (aus dem Bundle) |
| Aktive Version | Version des aktuellen Sidecar-Updates (aus `updates/version.json`) |

### 3.7 Rollback

Ein Klick auf "Rollback" löscht den `updates/`-Ordner; beim nächsten
Start wird die gebündelte Version aus der EXE geladen.

## 4 Deployment-Szenarien

### 4.1 Szenario A – Standalone-Windows-Server

1. `idvault.exe` auf Server kopieren (z. B. `C:\idvault\`)
2. Umgebungsvariablen setzen (`SECRET_KEY`, `IDV_HTTPS=1`)
3. `idvault.exe` starten (manuell oder als Scheduled Task beim Systemstart)
4. Firewall-Regel für Port 5443 (HTTPS) bereitstellen
5. Scheduled Task für Scanner einrichten

### 4.2 Szenario B – Windows-Dienst

Empfehlung: Einsatz von `nssm` (Non-Sucking Service Manager) oder
ähnlichem Dienst-Wrapper:

```cmd
nssm install idvault C:\idvault\idvault.exe
nssm set idvault AppEnvironmentExtra SECRET_KEY=<...>
nssm set idvault AppEnvironmentExtra IDV_HTTPS=1
nssm start idvault
```

### 4.3 Szenario C – Linux / Reverse-Proxy

```bash
# Systemd-Service
/etc/systemd/system/idvault.service
---
[Unit]
Description=idvault IDV-Register
After=network.target

[Service]
User=idvault
WorkingDirectory=/opt/idvault
Environment=SECRET_KEY=<...>
ExecStart=/opt/idvault/.venv/bin/gunicorn -w 4 -b 127.0.0.1:8000 "webapp:create_app()"
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```nginx
# nginx
server {
    listen 443 ssl http2;
    server_name idvault.intern;

    ssl_certificate     /etc/ssl/idvault/fullchain.pem;
    ssl_certificate_key /etc/ssl/idvault/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Security-Header (ergänzt die in idvault fehlenden)
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
        add_header X-Frame-Options "DENY" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Content-Security-Policy "default-src 'self'" always;
    }
}
```

## 5 CI / CD

### 5.1 Empfohlenes CI-Stage-Modell

| Stage | Werkzeuge |
|---|---|
| Lint | `ruff check .` |
| Static Security | `bandit -r .` |
| Dependency Audit | `pip-audit -r requirements.txt` |
| Unit-Tests | `pytest` (nach Einführung) |
| Build EXE | `pyinstaller idvault.spec --clean --noconfirm` (Windows-Runner) |
| Hash veröffentlichen | SHA-256 des Artefakts dokumentieren |
| Signieren | `signtool` mit Code-Signing-Zertifikat |

### 5.2 Branch-Strategie

- `main` → auslieferbar
- `develop` → Integrationszweig
- `feature/<ticket>` → Entwicklungsbranches
- `claude/<task>-<hash>` → automatisiert erzeugte Dokumentations-Branches

## 6 Versionierung

### 6.1 Schema

`MAJOR.MINOR.PATCH` (semver-orientiert):

- **MAJOR**: inkompatible Änderungen am Datenmodell / an der UI
- **MINOR**: neue Funktionen, rückwärtskompatibel
- **PATCH**: Fehlerbehebungen

### 6.2 `version.json`

```json
{
  "version": "0.1.0",
  "released": "2026-04-13",
  "changelog": [
    {
      "version": "0.1.0",
      "date": "2026-04-13",
      "changes": [
        "Erstveröffentlichung"
      ]
    }
  ]
}
```

Die Webapp zeigt die aktive Version im Footer und unter
`Admin → Software-Update`.

## 7 Erstausrollung – Checkliste

- [ ] EXE gebaut und signiert
- [ ] Prüfsumme dokumentiert
- [ ] `SECRET_KEY` sicher erzeugt und in KeyVault hinterlegt
- [ ] HTTPS-Zertifikat aus interner CA erzeugt
- [ ] LDAP-Service-Account angelegt und AD-Gruppen erstellt
- [ ] Scanner-Dienstkonto angelegt, Leserechte auf Shares
- [ ] SMTP-Postfach `idvault@...` angelegt
- [ ] Backup-Job eingerichtet und getestet
- [ ] Scheduled Task für Scanner eingerichtet
- [ ] Demo-Zugangsdaten deaktiviert
- [ ] Remediation der kritischen Schwachstellen abgeschlossen (vgl. [09 – Schwachstellenanalyse](09-schwachstellenanalyse.md))
- [ ] Datenschutz-Folgeabschätzung vorgenommen
- [ ] Freigabe durch Geschäftsleitung + ISB

## 8 Rücksetzungsplan

Falls ein Release unerwartet Probleme verursacht:

1. **Sidecar-Update**: `Admin → Software-Update → Rollback` (sofort)
2. **EXE-Update**: vorherige EXE aus Ausliefer-Archiv zurückspielen
3. **Datenbank-Rollback**: Wiederherstellung aus letzter Sicherung
4. **Kommunikation**: Information an betroffene Fachbereiche
5. **Root-Cause-Analyse**: Ursachenanalyse und Ticket im
   Entwicklungs-Backlog
