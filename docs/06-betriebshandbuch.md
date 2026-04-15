# 06 – Betriebshandbuch

---

## 1 Systemvoraussetzungen

### 1.1 Hardware

| Parameter | Empfehlung |
|---|---|
| CPU | 2 Kerne (x86_64) |
| RAM | 4 GB |
| Festplatte | 10 GB + Datenbank-Wachstum |
| Netz | Intern; Zugriff auf AD, SMTP, Fileshares |

### 1.2 Software

| Komponente | Version |
|---|---|
| Windows Server | 2019 / 2022, oder Windows 10/11 |
| Alternativ Linux | Ubuntu 22.04 LTS oder RHEL 8+ |
| Python (nur Quell-Installation) | 3.10 oder neuer |
| Browser (Client) | Edge / Chrome / Firefox in aktueller Version |

## 2 Installation

### 2.1 Variante A – Standalone-Executable (empfohlen)

```cmd
REM 1. idvault.exe in Zielverzeichnis kopieren
C:\idvault\idvault.exe

REM 2. Start
idvault.exe

REM 3. Browser öffnen
http://localhost:5000
```

Beim ersten Start wird `instance/idvault.db` angelegt und die
Demo-Zugangsdaten (`admin / idvault2025`) aktiviert.

### 2.2 Variante B – Quellinstallation

```bash
git clone https://github.com/hvorragend/idvault.git
cd idvault
pip install -r requirements.txt
python run.py
```

### 2.3 Unbeaufsichtigte Installation

Die Anwendung besteht aus einer einzigen Datei (EXE) bzw. dem Projektordner.
Keine MSI, keine Dienste, keine Registry-Einträge.

## 3 Erstkonfiguration

### 3.1 Erstschritte

1. Anmeldung als `admin / idvault2025`
2. Administration → Personen → `admin`-Passwort ändern
3. Administration → Personen: weitere Personen anlegen (oder LDAP-Import)
4. Administration → LDAP / Active Directory einrichten (falls AD vorhanden)
5. Administration → E-Mail-Einstellungen (SMTP) konfigurieren
6. Administration → Scanner-Einstellungen: Scan-Pfade hinterlegen
7. Demo-Zugangsdaten deaktivieren (siehe [05 – Sicherheitskonzept](05-sicherheitskonzept.md) Abschnitt 7)

### 3.2 Umgebungsvariablen

| Variable | Zweck | Default |
|---|---|---|
| `SECRET_KEY` | **Zwingend** zu setzen (≥ 32 Zeichen) | `"dev-change-in-production-!"` |
| `IDV_HTTPS` | HTTPS aktivieren | `0` |
| `IDV_SSL_CERT` | Zertifikatspfad | `instance/certs/cert.pem` |
| `IDV_SSL_KEY` | Privater Schlüssel | `instance/certs/key.pem` |
| `IDV_SSL_AUTOGEN` | Auto-Generierung selbstsigniert | `1` |
| `PORT` | Netzwerk-Port | 5000 HTTP / 5443 HTTPS |
| `IDV_SMTP_HOST` | SMTP-Server | — |
| `IDV_SMTP_PORT` | SMTP-Port | 587 |
| `IDV_SMTP_USER` | SMTP-Benutzer | — |
| `IDV_SMTP_PASSWORD` | SMTP-Passwort | — |
| `IDV_SMTP_FROM` | Absenderadresse | — |
| `IDV_SMTP_TLS` | STARTTLS (1) / SMTPS (0) | 1 |
| `DEBUG` | **Niemals produktiv** | 0 |

## 4 HTTPS-Konfiguration

### 4.1 Schnellstart mit selbstsigniertem Zertifikat

```cmd
set IDV_HTTPS=1
idvault.exe
```

→ Zertifikat wird beim ersten Start unter `instance\certs\` angelegt
(RSA-2048, 10 Jahre, SAN für Hostname + localhost + IPs).

### 4.2 Eigenes Zertifikat verwenden

```cmd
set IDV_HTTPS=1
set IDV_SSL_CERT=C:\zertifikate\idvault-fullchain.pem
set IDV_SSL_KEY=C:\zertifikate\idvault-key.pem
idvault.exe
```

Bei CA-signierten Zertifikaten muss die vollständige Zertifikatskette
(Server-Zertifikat + Zwischenzertifikate) in `fullchain.pem` enthalten sein.

### 4.3 Reverse-Proxy-Alternative

```
[Clients] → [nginx/IIS/Apache] (TLS-Terminierung) → [idvault:5000]
```

Vorteile: zentrale Zertifikatsverwaltung, Let's-Encrypt-Automatisierung,
HSTS/CSP-Header im Proxy konfigurierbar.

## 5 LDAP / Active Directory

### 5.1 Voraussetzungen

- LDAPS-Erreichbarkeit (Port 636)
- Technischer Benutzer (Service-Account) mit Leserechten
- AD-Gruppen für jede idvault-Rolle

### 5.2 Konfiguration

Administration → LDAP / Active Directory → LDAP konfigurieren

| Feld | Beispiel |
|---|---|
| Server-URL | `ldaps://ldap.bank.de` |
| Port | `636` |
| Base-DN | `OU=Benutzer,DC=bank,DC=de` |
| Bind-DN | `CN=svc-idvault,OU=Service,DC=bank,DC=de` |
| Kennwort | (Service-Account-Passwort) |
| Benutzer-Attribut | `sAMAccountName` |
| TLS-Zertifikat prüfen | ✓ |

### 5.3 Gruppen-Rollen-Mapping

| idvault-Rolle | Beispiel-Gruppen-DN |
|---|---|
| IDV-Administrator | `CN=IDV-Administratoren,OU=Gruppen,DC=bank,DC=de` |
| IDV-Koordinator | `CN=IDV-Koordinatoren,OU=Gruppen,DC=bank,DC=de` |
| Fachverantwortlicher | `CN=IDV-Fachverantwortliche,OU=Gruppen,DC=bank,DC=de` |
| Revision | `CN=IDV-Revision,OU=Gruppen,DC=bank,DC=de` |
| IT-Sicherheit | `CN=IDV-IT-Sicherheit,OU=Gruppen,DC=bank,DC=de` |

Vollständigen Gruppen-DN in PowerShell ermitteln:
```powershell
Get-ADGroup -Identity "IDV-Administratoren" | Select DistinguishedName
```

### 5.4 Automatischer Fallback

Ist der LDAP-Server nicht erreichbar, wechselt idvault automatisch auf
die lokale Authentifizierung. In diesem Fall greifen:
- Personen mit gesetztem `password_hash`
- Der Demo-Account `admin / idvault2025` (solange nicht deaktiviert)

### 5.5 Notfall-Zugang (Break-Glass)

Administration → LDAP → "Lokalen Notfall-Zugang im Login-Fenster anzeigen" aktivieren

Damit wird im Login ein zusätzliches Formular angezeigt, das LDAP
vollständig umgeht. Nur bei Bedarf aktivieren und nach Einsatz wieder
deaktivieren. Jede Nutzung ist im Login-Log ersichtlich.

### 5.6 Mitarbeiter aus LDAP importieren

Administration → LDAP → Mitarbeiter importieren

- Lädt alle aktiven AD-Konten
- Optionaler LDAP-Filter (z. B. `(department=Finanzen)`)
- Batch-Import mit Rollenzuordnung aus Gruppen-Mapping

## 6 SMTP / E-Mail

### 6.1 Einstellungen

Administration → E-Mail-Einstellungen (SMTP)

| Feld | Beispiel |
|---|---|
| SMTP-Host | `mail.bank.de` |
| Port | 587 (STARTTLS) oder 465 (SSL) |
| Benutzer | `idvault@bank.de` |
| Passwort | (Postfach-Kennwort) |
| Absenderadresse | `idvault@bank.de` |
| STARTTLS | ✓ bei Port 587, ✗ bei Port 465 |

### 6.2 Benachrichtigungstypen

| Ereignis | Empfänger | Auslöser |
|---|---|---|
| Neue Datei im Scanner | Koordinatoren + Admins | Manuell in Scanner-Funden |
| Prüfung fällig | Fachverantwortlicher | Per Skript/Cronjob |
| Maßnahme überfällig | Verantwortlicher | Per Skript/Cronjob |
| Freigabeverfahren gestartet | Prüfer + Koordinator | Automatisch bei Phase-Start |
| Freigabe bestanden | Koordinatoren/Admins/Entwickler | Automatisch |

## 7 Scanner-Betrieb

### 7.1 Scan-Pfade konfigurieren

Administration → Scanner-Einstellungen

- Scan-Pfade (UNC oder Laufwerksbuchstaben)
- Dateitypen (.xlsx, .xlsm, .py, .sql, ...)
- Ausschlüsse (Temp-Verzeichnisse, Backup-Ordner)

### 7.2 Scan starten

**UI**: In jeder Scanner-Ansicht → Schaltfläche "Scan starten" (Admin/Koordinator)

**CLI**:
```cmd
idvault.exe --scan --config C:\idvault\scanner\config.json
```

### 7.3 Scheduled Task (Windows)

```
Aufgabenplanung → Neue Aufgabe
  Programm:  C:\idvault\idvault.exe
  Argumente: --scan --config C:\idvault\scanner\config.json
  Trigger:   Wöchentlich, Montag 06:00
  Konto:     Dienstkonto mit Leserechten
```

### 7.4 Scan steuern

Die Scan-Schaltfläche in der Webapp zeigt je nach Zustand:

| Zustand | Buttons |
|---|---|
| Scan läuft | Pause / Abbrechen |
| Scan pausiert | Fortsetzen / Abbrechen |
| Scan abgebrochen (Checkpoint vorhanden) | Fortsetzen / Neu starten |
| Kein aktiver Scan | Scan starten |

Details siehe [10 – Scanner](10-scanner.md).

## 8 Backup und Wiederherstellung

### 8.1 Sicherungsobjekt

```
instance/
├── idvault.db            SQLite-Datenbank
├── idvault.log*          Anwendungs-Logs
├── login.log*            Audit-Logs
├── uploads/              Hochgeladene Nachweise
└── certs/                (optional) SSL-Zertifikate
```

### 8.2 Sicherungsmethoden

**Methode A – Offline-Kopie** (anwendungsstopp erforderlich)

```cmd
net stop "idvault"
xcopy /E /I instance \\backup\idvault\%date%
net start "idvault"
```

**Methode B – Online-Backup** (Anwendung bleibt aktiv)

```cmd
sqlite3 instance\idvault.db ".backup \\backup\idvault\%date%\idvault.db"
```

### 8.3 Wiederherstellung

1. Anwendungsprozess stoppen
2. `instance/idvault.db` aus Sicherung zurückspielen
3. Ggf. `idvault.log*`, `login.log*`, `uploads/` ebenfalls zurückspielen
4. Anwendung starten
5. Integritätsprüfung: `PRAGMA integrity_check;`

### 8.4 Aufbewahrungsfristen

| Artefakt | Frist | Rechtsgrundlage |
|---|---|---|
| IDV-Register | dauerhaft | MaRisk AT 5, AT 7.2 |
| Prüfungen / Maßnahmen | 10 Jahre | § 257 HGB, § 147 AO |
| Login-Log | ≥ 12 Monate | Revisionserfordernis |
| Anwendungs-Log | ≥ 6 Monate | Interne Betriebs-Policy |

## 9 Software-Update

### 9.1 Funktionsprinzip

Die `idvault.exe` wird **nie ersetzt**. Updates werden als Sidecar-Dateien
im `updates/`-Ordner neben der EXE abgelegt und beim Start bevorzugt
geladen. Das erhält AppLocker-Hash-Regeln dauerhaft.

### 9.2 Update einspielen

System → Software-Update → ZIP-Datei auswählen → "ZIP hochladen & einspielen"

Anschließend "App neu starten" klicken.

### 9.3 GitHub-Repository-ZIP

Der GitHub-Download-Link kann direkt hochgeladen werden:

```
https://github.com/hvorragend/idvault/archive/refs/heads/main.zip
```

- Das `idvault-main/`-Präfix wird automatisch entfernt
- Nicht relevante Dateien (`.md`, `.gitignore`, …) werden ignoriert
- `webapp/templates/` wird automatisch auf `templates/` gemappt

### 9.4 Rollback

System → Software-Update → "Rollback (Update entfernen)"

Der `updates/`-Ordner wird gelöscht; beim nächsten Start läuft wieder die
gebündelte Version der EXE.

### 9.5 Erlaubte Dateitypen

| Erweiterung | Erlaubt |
|---|:---:|
| `.py`, `.html`, `.json`, `.sql`, `.css`, `.js` | ✓ |
| `.exe`, `.dll`, `.bat`, `.sh` | ✗ |

### 9.6 Versionsanzeige

| Bezeichnung | Bedeutung |
|---|---|
| Gebündelte Version | Version, mit der die EXE gebaut wurde |
| Aktive Version | Version des aktuellen Sidecar-Updates |

## 10 Monitoring

### 10.1 Logs überwachen

| Log | Auf Warnings achten |
|---|---|
| `instance/idvault.log` | ERROR, CRITICAL |
| `instance/login.log` | Brute-Force-Muster (viele FEHLER in kurzer Zeit) |
| `instance/idvault_crash.log` | Existenz bedeutet ungeplanten Anwendungsfehler |

### 10.2 Gesundheitsprüfung

- URL `/auth/login` liefert HTTP 200
- Datenbank: `PRAGMA integrity_check;` gibt `ok` zurück
- Scan-Läufe-Seite: letzter Scan im erwarteten Intervall

### 10.3 Metriken (bei Weiterleitung an SIEM)

- Login-Fehlerrate pro Minute
- Anzahl aktiver Sessions
- Antwortzeiten pro Endpunkt (optional via Reverse-Proxy)

## 11 Fehlerbehandlung

### 11.1 Typische Fehlersituationen

| Symptom | Ursache | Abhilfe |
|---|---|---|
| "Nicht vertrauenswürdiges Zertifikat" im Browser | Selbstsigniertes Zertifikat | CA-Zertifikat installieren oder Ausnahme hinzufügen |
| "Active Directory aktiv" erscheint nicht | LDAP nicht aktiviert oder kein Mapping | Mapping und Aktivierung prüfen |
| Login schlägt ohne Meldung fehl | Kein Gruppen-Mapping passt | Admin-Log prüfen; Mapping ergänzen |
| Scanner läuft, aber keine neuen Funde | DB-Pfade unterschiedlich | `scanner/config.json` `db_path` auf `instance/idvault.db` setzen |
| Update-Upload abgelehnt | Datei enthält unzulässige Extensions | Nur erlaubte Extensions verwenden |
| E-Mail versendet nicht | SMTP falsch konfiguriert | Admin → E-Mail → Einstellungen testen |

### 11.2 Debug-Modus

Debug-Modus **niemals produktiv** verwenden:

```cmd
REM Nur für Entwicklungsumgebungen
set DEBUG=1
python run.py
```

Der Debug-Modus zeigt bei Fehlern vollständige Stacktraces im Browser
und aktiviert automatisches Code-Reloading.

## 12 Abschaltung / Deinstallation

1. Letzte Sicherung anfertigen
2. Anwendungsprozess beenden
3. Gescheduled Tasks entfernen
4. Verzeichnis löschen
5. LDAP-Service-Account deaktivieren
6. SMTP-Postfach-Berechtigungen entziehen
