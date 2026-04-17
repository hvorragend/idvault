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

Beim ersten Start wird `instance/idvault.db` angelegt, die
Demo-Zugangsdaten (`admin / idvault2026`) aktiviert und — falls noch
keine `config.json` vorhanden ist — automatisch eine `config.json` mit
einem zufälligen `SECRET_KEY` neben der EXE erzeugt.

### 2.2 Variante B – Quellinstallation

```bash
git clone https://github.com/hvorragend/idvault.git
cd idvault
pip install -r requirements.txt
python run.py
```

### 2.3 Variante C – Windows-Dienst (empfohlen für Produktivbetrieb)

idvault.exe bringt ein natives Windows-Service-Framework mit und kann
direkt beim Service Control Manager (SCM) registriert werden –
ohne externen Wrapper wie NSSM oder winsw.

**Voraussetzungen**

- `idvault.exe` liegt im Zielverzeichnis (z. B. `C:\idvault\`)
- `config.json` mit `SECRET_KEY` vorhanden (siehe Abschnitt 3)
- PowerShell / CMD **als Administrator**

**Dienst einrichten**

```powershell
# 1. Dienstnamen in config.json festlegen (empfohlen)
#    "IDV_SERVICE_NAME": "idvault"

# 2. Dienst registrieren (Dienstname aus IDV_SERVICE_NAME oder Default "idvault")
C:\idvault\idvault.exe install

# 3. Dienstkonto auf LOCAL SYSTEM belassen (Standard) – kein AD-User nötig
#    Der Scan-User wird separat in der Web-UI konfiguriert (Abschnitt 7.6)

# 4. Dienst starten
C:\idvault\idvault.exe start
```

Weitere Verwaltungsbefehle:

| Befehl | Wirkung |
|---|---|
| `idvault.exe install` | Dienst registrieren |
| `idvault.exe start` | Dienst starten |
| `idvault.exe stop` | Dienst stoppen |
| `idvault.exe restart` | Dienst neu starten |
| `idvault.exe remove` | Dienst entfernen |

**Neustart aus der Web-UI**

Administration → Software-Update → „App neu starten" erkennt automatisch,
ob idvault als Dienst läuft, und führt dann `sc start <name>` aus statt
eine neue EXE-Instanz zu spawnen. Der Dienstname wird per PID-Abgleich
selbständig ermittelt (funktioniert ohne `IDV_SERVICE_NAME` bei nativer
Registrierung); bei NSSM/winsw-Wrappern `IDV_SERVICE_NAME` in
`config.json` eintragen.

### 2.4 Unbeaufsichtigte Installation

Die Anwendung besteht aus einer einzigen Datei (EXE) bzw. dem Projektordner.
Keine MSI, keine Registry-Einträge außer dem SCM-Eintrag bei
`idvault.exe install`.

## 3 Erstkonfiguration

### 3.1 Erstschritte

1. Anmeldung als `admin / idvault2026`
2. Administration → Personen → `admin`-Passwort ändern
3. Administration → Personen: weitere Personen anlegen (oder LDAP-Import)
4. Administration → LDAP / Active Directory einrichten (falls AD vorhanden)
5. Administration → E-Mail-Einstellungen (SMTP) konfigurieren
6. Administration → Scanner-Einstellungen: Scan-Pfade hinterlegen
7. Demo-Zugangsdaten deaktivieren (siehe [05 – Sicherheitskonzept](05-sicherheitskonzept.md) Abschnitt 7)

### 3.2 Konfigurationsdatei (config.json)

Die bevorzugte Methode zur Konfiguration ist die Datei `config.json`
neben der EXE (bzw. im Projektverzeichnis). Sie wird beim ersten Start
**automatisch angelegt**, falls weder die Datei noch die Env-Variable
`SECRET_KEY` vorhanden ist.

Vorlage für manuelle Anpassung:

```
config.json.example  →  config.json kopieren und bearbeiten
```

Beispielinhalt (Auszug – die komplette Vorlage mit allen Sektionen
steht in `config.json.example`):

```json
{
  "SECRET_KEY": "zufaelliger-schluessel-mind-32-zeichen",
  "PORT": 5000,
  "IDV_HTTPS": 0,

  "scanner": {
    "scan_paths": ["\\\\fileserver\\abteilung$"],
    "db_path": "instance/idvault.db",
    "log_path": "instance/logs/idv_scanner.log"
  },

  "teams": {
    "tenant_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "client_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "client_secret": "ENV:TEAMS_SCANNER_SECRET",
    "teams": [{ "team_id": "..." }]
  }
}
```

> **Sicherheitshinweis:** `config.json` enthält den `SECRET_KEY` im
> Klartext. Dateizugriffsrechte auf den Betriebssystemnutzer der
> Anwendung einschränken.

#### Präzedenz der Konfigurationsquellen

Seit der Konsolidierung der Einstellungen gilt folgende Reihenfolge
(höchste zuerst):

1. **OS-Umgebungsvariable** – z.B. `SECRET_KEY=...` im systemd-Dienst.
2. **`config.json`** – Top-Level-Keys (SECRET_KEY, PORT, …) und die
   Unter-Sektionen `scanner`, `teams`, `ldap`.
3. **SQLite-Tabellen `app_settings` / `ldap_config`** – werden über die
   Web-UI (`/admin/mail`, `/admin/ldap-config`, `/admin/scanner-einstellungen`)
   gepflegt.
4. **Hardcoded-Defaults** – aus `schema.sql` bzw. dem Quellcode.

Ein Eintrag in `config.json["ldap"]` überschreibt pro Feld die DB-Werte
und wird in der Admin-UI als `read-only` mit Badge *„Aus config.json"*
angezeigt. SMTP-Zugangsdaten werden ausschließlich in `app_settings`
gepflegt – die früheren `IDV_SMTP_*`-Schlüssel in `config.json` hatten
keine Wirkung mehr und sind aus dem Auto-Template entfernt worden.

### 3.3 Umgebungsvariablen

OS-Umgebungsvariablen haben **immer Vorrang** über `config.json` – sie
eignen sich als Override in CI/CD-Pipelines, Docker-Containern oder
Skripten.

| Variable | Zweck | Default |
|---|---|---|
| `SECRET_KEY` | Flask Session Secret (≥ 32 Zeichen). Wird beim ersten Start ohne `config.json` automatisch generiert. | — (auto) |
| `IDV_HTTPS` | HTTPS aktivieren | `0` |
| `IDV_SSL_CERT` | Zertifikatspfad | `instance/certs/cert.pem` |
| `IDV_SSL_KEY` | Privater Schlüssel | `instance/certs/key.pem` |
| `IDV_SSL_AUTOGEN` | Auto-Generierung selbstsigniert | `1` |
| `PORT` | Netzwerk-Port | 5000 HTTP / 5443 HTTPS |
| `IDV_DB_PATH` | Datenbankpfad | `instance/idvault.db` |
| `IDV_INSTANCE_PATH` | Instanzverzeichnis | `instance/` |
| `IDV_SERVICE_NAME` | Windows-Dienstname für Neustart aus Web-UI und `idvault.exe install`. Wird bei nativer SCM-Registrierung automatisch erkannt; bei NSSM/winsw manuell setzen. | `""` (auto) |
| `DEBUG` | **Niemals produktiv** | 0 |

SMTP-Zugangsdaten werden nicht mehr über Umgebungsvariablen gesteuert.
Sie liegen in der SQLite-Tabelle `app_settings` und werden über
`Administration → E-Mail-Einstellungen` verwaltet.

## 4 HTTPS-Konfiguration

### 4.1 Schnellstart mit selbstsigniertem Zertifikat

In `config.json` eintragen:

```json
{
  "IDV_HTTPS": 1
}
```

→ Zertifikat wird beim ersten Start unter `instance\certs\` angelegt
(RSA-2048, 10 Jahre, SAN für Hostname + localhost + IPs).

### 4.2 Eigenes Zertifikat verwenden

In `config.json` eintragen:

```json
{
  "IDV_HTTPS": 1,
  "IDV_SSL_CERT": "C:\\zertifikate\\idvault-fullchain.pem",
  "IDV_SSL_KEY": "C:\\zertifikate\\idvault-key.pem"
}
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
- Der Demo-Account `admin / idvault2026` (solange nicht deaktiviert)

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
idvault.exe --scan --config C:\idvault\config.json
```

### 7.3 Scheduled Task (Windows)

```
Aufgabenplanung → Neue Aufgabe
  Programm:  C:\idvault\idvault.exe
  Argumente: --scan --config C:\idvault\config.json
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

### 7.5 Fehlersuche: „Pfad nicht erreichbar" beim Dienst-Betrieb

Wird idvault als Windows-Dienst betrieben und die Webapp startet den Scanner
über einen hinterlegten Scan-User (Administration → Scanner-Einstellungen →
Run-As), kann der Scanner den UNC-Pfad trotz Zugriffsrechten des Scan-Users
nicht erreichen. Typische Log-Ausgabe:

```
[INFO]    Scanner-Identität: LOCAL SERVICE (Konsolen-Session-ID: 0)
[WARNING] Pfad nicht erreichbar: \\server\freigabe – PermissionError [WinError 5]: Zugriff verweigert
[WARNING]   Hinweis: Bitte prüfen, ob der oben geloggte Scanner-Benutzer …
```

Prüfpunkte in dieser Reihenfolge:

1. **Scanner-Identität** – Die erste Log-Zeile nach `Scan-Run #N gestartet`
   zeigt, unter welcher Identität der Scanner-Subprocess tatsächlich läuft.
   Endet der Name auf `$` (z. B. `DOMAIN\HOST$`), handelt es sich um das
   **Computer-Account** des Servers – der Dienst läuft als `LOCAL SYSTEM`
   (oder `NETWORK SERVICE`) und die hinterlegte `Run-As`-Konfiguration
   wurde nicht angewandt. Im *stdout/stderr-Mitschnitt* findest du in
   dem Fall eine `[IDVAULT-START]`-Zeile mit dem exakten Grund.
2. **`[IDVAULT-START]`-Zeilen auswerten:**
   - `Run-As-Passwort … konnte nicht entschlüsselt werden` – der
     `SECRET_KEY` hat sich seit dem Speichern der Konfiguration geändert.
     Passwort in Administration → Scanner-Einstellungen → Run-As erneut
     eintragen und speichern.
   - `Run-As-Benutzer … gespeichert, aber kein Passwort hinterlegt` –
     Password-Feld war beim letzten Speichern leer; erneut eintragen.
   - `pywin32-Module fehlen im EXE-Build: …` – der Build enthält nicht
     alle benötigten Hidden-Imports. EXE mit der aktuellen `idvault.spec`
     (`pywintypes`, `win32api`, `win32con`, `win32event`, `win32file`,
     `win32process`, `win32security`, `ntsecuritycon`, `win32service`,
     `win32serviceutil`, `servicemanager`, `win32timezone`) neu bauen.
     Oder den Dienst direkt als Scan-User betreiben – siehe 7.6.
   - `pywin32 nicht verfügbar – Dienst-Modus nicht möglich: <Modul>` –
     beim Aufruf von `idvault.exe install/start/stop/remove` fehlt ein
     pywin32-Dienstmodul im EXE-Build. Gleiche Abhilfe wie oben: neu
     bauen mit vollständiger `idvault.spec`.
   - **Fehler 1053** („Der Dienst antwortete nicht rechtzeitig auf die
     Start- oder Steuerungsanforderung") beim Starten über
     `services.msc` / `sc start idvault` – die EXE hat
     `StartServiceCtrlDispatcher()` nicht innerhalb von ~30 s erreicht.
     Ab Version mit Lazy-App-Build sollte das nicht mehr auftreten
     (Flask-App wird erst nach SCM-Connect innerhalb `SvcDoRun` gebaut).
     Falls doch: langsames `%TEMP%` (Netzlaufwerk-Redirect, Virenscanner,
     BitLocker-Entschlüsselung) – EXE lokal auf SSD ablegen, oder die
     systemweite SCM-Wartezeit in der Registry anpassen:
     `HKLM\SYSTEM\CurrentControlSet\Control\ServicesPipeTimeout`
     (DWORD, Millisekunden; erfordert Neustart).
   - **Fehler 1067** („Der Prozess wurde unerwartet beendet") beim
     Stoppen oder Starten des Dienstes – der Python-Prozess hat den
     Status `SERVICE_STOPPED` nicht an SCM gemeldet, bevor er endete.
     Ab aktueller Version wird der Status explizit gemeldet; wenn 1067
     weiterhin erscheint, liegt ein Crash des Flask-Threads vor.
     Traceback in `instance/logs/idvault_crash.log` prüfen (siehe
     dort auch den Event-Log-Eintrag mit dem Text „`_run_server()`
     abgebrochen").
   - **Dienst läuft, Website nicht erreichbar** – Flask ist im Daemon-
     Thread abgestürzt, der Dienst-Main-Thread wartet aber weiter auf
     das Stop-Event. Auslöser war typischerweise ein relativer DB-Pfad
     (`IDV_DB_PATH=instance/idvault.db`) in Kombination mit der
     CWD=`C:\Windows\System32`, die SCM beim Dienststart setzt.
     Ab aktueller Version wird CWD im Dienst-Modus auf das
     EXE-Verzeichnis umgeschaltet und relative DB-/Instance-Pfade
     werden am EXE-Anker aufgelöst. Bei älteren Builds Abhilfe:
     absolute Pfade in `config.json` setzen.
   - `LogonUser(…, NETWORK_CLEARTEXT) fehlgeschlagen` – pywin32 ist
     vollständig, aber der Logon schlug fehl. Detail-Ursachen im
     Klammertext:
     - `WinError 1326` (0x52E): Passwort des Scan-Users falsch oder
       abgelaufen. Passwort in Administration → Scanner-Einstellungen →
       Run-As erneut eintragen.
     - `WinError 1385` (0x569, „nicht die Berechtigung, sich in dieser
       Art anzumelden"): Sollte mit `LOGON_NETWORK_CLEARTEXT` nicht mehr
       auftreten (kein `SeBatchLogonRight` erforderlich). Falls doch:
       Konto in `secpol.msc` → „Auf diesen Computer vom Netzwerk aus
       zugreifen" eintragen.
   - `CreateProcessAsUser fehlgeschlagen … WinError 1314` (0x522,
     „Ein erforderliches Recht steht dem Client nicht zur Verfügung"):
     Der idvault-Dienst läuft nicht als LOCAL SYSTEM (fehlendes
     `SeAssignPrimaryTokenPrivilege`). Dienstkonto auf LOCAL SYSTEM
     umstellen (services.msc → Eigenschaften → Anmelden).
2. **WinError 5 (Zugriff verweigert)** – Der Scan-User darf das Share nicht
   lesen. NTFS- und Freigabe-Berechtigungen prüfen, ggf. Gruppenmitgliedschaft
   durch `gpupdate /force` + neuerliches Anmelden aktualisieren.
3. **WinError 53 / 67 (Netzwerkpfad nicht gefunden)** – DNS- oder
   NetBIOS-Auflösung scheitert. Vom Server aus
   `ping fis.i4024.pb.rz.bankenit.de` und `net view \\<server>` prüfen.
4. **WinError 1326 (Anmeldung fehlgeschlagen)** – Passwort des Scan-Users
   im idvault falsch / abgelaufen.
5. **Dienstkonto selbst** – Wenn der idvault-Dienst als Domänen-Account
   eingerichtet ist, muss dieses Konto zumindest `Anmelden als Dienst`
   besitzen; es muss jedoch nicht Zugriff auf das Share haben, solange der
   konfigurierte Scan-User greift.

Das Scanner-Log ist im Web-UI unter **Administration → Scan-Log** verfügbar.
Bei laufendem Scan aktualisiert sich die Anzeige automatisch alle 3 Sekunden;
Filter nach Log-Level (ERROR/WARNING/INFO/DEBUG) und Volltextsuche stehen zur
Verfügung. Über das Auswahlfeld „stdout/stderr-Mitschnitt" lässt sich
zusätzlich der ungefilterte Subprocess-Output (`scanner_output.log`)
einsehen – dort landen Crash-Meldungen, die *vor* dem Initialisieren des
Loggers auftreten.

## 8 Backup und Wiederherstellung

### 8.1 Sicherungsobjekt

```
config.json               Konfigurationsdatei (SECRET_KEY, scanner, teams, ldap …)
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
| Scanner läuft, aber keine neuen Funde | DB-Pfade unterschiedlich | In `config.json` unter `scanner.db_path` denselben Pfad wie `IDV_DB_PATH` eintragen |
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
und aktiviert automatisches Code-Reloading. Die Anwendung gibt in
diesem Fall eine klare Banner-Warnung beim Start aus und erlaubt als
einzige Ausnahme den Betrieb ohne gesetzten `SECRET_KEY`.

## 12 Abschaltung / Deinstallation

1. Letzte Sicherung anfertigen
2. Anwendungsprozess beenden
3. Gescheduled Tasks entfernen
4. Verzeichnis löschen
5. LDAP-Service-Account deaktivieren
6. SMTP-Postfach-Berechtigungen entziehen
