# 10 – Scanner

---

## 1 Überblick

idvscope umfasst zwei unabhängige Scanner-Komponenten zur Identifikation
von IDV-Kandidaten:

1. **Dateisystem-Scanner** (`scanner/network_scanner.py`) – für
   Netzlaufwerke, UNC-Pfade und lokale Verzeichnisse
2. **Teams-Scanner** (`scanner/teams_scanner.py`) – für Microsoft Teams /
   SharePoint über die Microsoft Graph API (optional)

Beide Scanner schreiben ihre Ergebnisse in dieselbe SQLite-Datenbank
(`idv_files`, `idv_file_history`, `scan_runs`).

## 2 Dateisystem-Scanner

### 2.1 Zweck

Der Scanner durchsucht konfigurierte Pfade nach typischen IDV-Dateien
(Excel-, Access-, Python-, SQL-, Power-BI-, R-Dateien) und erhebt
folgende Metadaten:

- Dateiname und vollständiger Pfad (UNC)
- Dateigröße (Byte)
- Letztes Änderungsdatum
- SHA-256-Hash des Dateiinhalts (optional `xxhash` für Performance)
- VBA-Makros vorhanden (Excel)
- Externe Verknüpfungen vorhanden (Excel)
- Blattschutz aktiv (Excel)
- Dateieigentümer (Windows, via `pywin32`)

### 2.2 Konfiguration (`config.json` → Abschnitt `scanner`)

Die Scanner-Einstellungen liegen im `"scanner"`-Abschnitt der gemeinsamen
`config.json` neben der EXE (bzw. im Projektverzeichnis):

```json
{
  "scanner": {
    "scan_paths": ["\\\\server01\\freigabe"],
    "db_path": "instance/idvscope.db"
  }
}
```

| Parameter | Typ | Standard | Beschreibung |
|---|---|---|---|
| `scan_paths` | Liste | `[]` | Zu scannende Pfade (UNC oder Laufwerksbuchstaben) |
| `extensions` | Liste | `.xlsx`, `.xlsm`, `.py`, `.sql`, … | Erfasste Dateierweiterungen |
| `blacklist_paths` | Liste | siehe unten | Pfad-/Dateinamen-Muster (Regex, case-insensitive), die Verzeichnis- **und** Dateiebene filtern |
| `exclude_paths` | Liste | `[]` | Legacy-Alias, für neue Installationen leer |

**Blacklist-Defaults** (wirken sowohl als Ordner- als auch als Dateinamen-Filter):

- `~\$` – Office-Lock-Dateien (`~$foo.xlsx`)
- `\.tmp(\b|$)` – Temp-Suffixe
- `\$RECYCLE\.BIN`, `System Volume Information`, `[\\/]Papierkorb[\\/]`, `[\\/]AppData[\\/]`, `[\\/]Temp[\\/]` – System-/Papierkorb-Pfade
- `[\\/]\.git[\\/]`, `[\\/]__pycache__[\\/]`, `[\\/]node_modules[\\/]`, `[\\/]\.venv[\\/]`, `[\\/]venv[\\/]` – Entwickler-/VCS-Ordner
- ` - Kopie[\s.(]`, ` - Copy[\s.(]`, `[\\/]Kopie von `, `[\\/]Copy of ` – Windows-Explorer-Dubletten
- `_alt\.`, `_backup\.`, `_bak\.`, `_old\.` – Alt-/Backup-Suffixe am Dateinamen

| `db_path` | String | `"instance/idvscope.db"` | Pfad zur SQLite-Datenbank |
| `log_path` | String | `"scanner/network_scanner.log"` | Pfad zur Logdatei |
| `hash_size_limit_mb` | Integer | `500` | Dateien größer als dieser Wert werden nicht gehasht |
| `max_workers` | Integer | `4` | Derzeit ohne Wirkung – Scanner läuft single-threaded (für künftige Parallelisierung reserviert) |
| `move_detection` | String | `"name_and_hash"` | Modus der Verschiebe-Erkennung |
| `scan_since` | String\|null | `null` | Nur Dateien mit mtime ≥ diesem Datum verarbeiten |
| `read_file_owner` | Boolean | `true` | Dateibesitzer via Windows-API lesen |

Die Scanner-Einstellungen können auch über die Web-Oberfläche bearbeitet
werden: Administration → Scanner-Einstellungen.

### 2.4 Datei-Stati

| Status | Bedeutung |
|---|---|
| `active` | Datei wurde beim letzten Scan gefunden |
| `archiviert` | Datei wurde beim letzten Scan **nicht** mehr gefunden (verschoben/umbenannt/gelöscht); Verknüpfungen zum IDV-Register bleiben gültig |

### 2.5 Bearbeitungsstatus

| Status | Bedeutung | Übergang |
|---|---|---|
| `Neu` | Vom Scanner entdeckt, noch nicht gesichtet | automatisch beim Scan |
| `Zur Registrierung` | Vorgemerkt für IDV-Erfassung | manuell "Zur Registrierung vormerken" |
| `Registriert` | Einem IDV-Register-Eintrag zugeordnet | automatisch beim Anlegen |
| `Ignoriert` | Bewusst ausgeschlossen | manuell "Ignorieren" |

### 2.6 Status-Übergänge

```
Erstfund
  → active  (change_type: new)

Scan: Datei wieder gefunden, Inhalt unverändert
  active → active  (change_type: unchanged)

Scan: Datei wieder gefunden, Inhalt geändert
  active → active  (change_type: changed)

Scan: Datei nicht mehr am bekannten Pfad gefunden
  active → archiviert  (change_type: archiviert)

Scan: Archivierte Datei am selben Pfad wieder vorhanden
  archiviert → active  (change_type: restored)

Scan: Verschobene Datei erkannt
  active (alter Pfad) → active (neuer Pfad)  (change_type: moved)
```

### 2.7 Move-Detection

#### `"name_and_hash"` (Standard)

Eine Datei gilt als verschoben, wenn unter dem neuen Pfad noch kein
Eintrag existiert **und** eine aktive Datei mit **gleichem SHA-256-Hash**
und **gleichem Dateinamen** gefunden wird.

#### `"hash_only"`

Eine Datei gilt als verschoben, wenn unter dem neuen Pfad noch kein
Eintrag existiert **und genau eine** aktive Datei mit gleichem SHA-256
gefunden wird (bei mehreren Treffern: als `new` behandelt).

#### `"disabled"`

Keine Move-Detection. Jede Datei, die nicht mehr am bekannten Pfad liegt,
wird archiviert.

### 2.8 Hash-Berechnung

- Algorithmus: **SHA-256** über den vollständigen Dateiinhalt
- Dateien ohne Leseberechtigung: Platzhalterwert `HASH_ERROR`
- Dateien über `hash_size_limit_mb`: ebenfalls `HASH_ERROR`, Änderungsstatus
  basiert dann nur auf mtime

### 2.9 Scan-Protokoll (`scan_runs`)

Je Scan-Lauf werden folgende Kennzahlen erfasst:

- `total_files` – gefundene Dateien
- `new_files` – erstmalig gefundene
- `changed_files` – Hash geändert
- `moved_files` – verschobene
- `restored_files` – aus Archiv wiederhergestellte
- `archived_files` – in diesem Lauf archivierte
- `errors` – Dateien/Pfade mit Verarbeitungsfehlern

### 2.10 Pause, Abbruch, Checkpoint

| Zustand | Buttons in UI |
|---|---|
| Scan läuft | Pause · Abbrechen |
| Scan pausiert | Fortsetzen · Abbrechen |
| Scan abgebrochen (Checkpoint) | Fortsetzen · Neu starten |
| Kein aktiver Scan | Scan starten |

Alternativ per Signaldatei im Scanner-Verzeichnis:

| Datei | Wirkung |
|---|---|
| `scanner_pause.signal` | Pause nach dem nächsten Verzeichnis |
| `scanner_cancel.signal` | Abbruch nach dem nächsten Verzeichnis |
| `scanner_checkpoint.json` | Fortschrittsstand (automatisch verwaltet) |

Der Checkpoint wird nach jedem vollständig abgeschlossenen
**Top-Level-Unterverzeichnis** geschrieben, sodass ein abgebrochener
Scan ohne erneutes Durchlaufen bereits fertiger Verzeichnisse
fortgesetzt werden kann.

### 2.11 Mehrere Teilscans

Große Verzeichnisse können auf mehrere Scan-Läufe aufgeteilt werden:

```json
// config_share1.json
{ "scan_paths": ["//srv/share1/Abteilung_A", "//srv/share1/Abteilung_B"] }

// config_share2.json
{ "scan_paths": ["//srv/share2"] }
```

Die Archivierungslogik (`mark_deleted_files`) berücksichtigt den
Geltungsbereich jedes Scan-Laufs. Dateien aus nicht gescannten
Verzeichnissen bleiben unberührt.

### 2.12 Multi-Scanner

#### Strategie 1 – Gemeinsame DB (sequentiell)

Alle Scanner schreiben in dieselbe `.db`-Datei auf dem Server.
Voraussetzung: Scans laufen zeitlich getrennt (SQLite WAL-Modus
verträgt sequentielle Schreiber gut, aber keine parallelen Schreiber
von verschiedenen Rechnern).

#### Strategie 2 – Separate DBs + Import (empfohlen für Parallelbetrieb)

Jeder Rechner scannt in eine eigene lokale `.db`. Ergebnisse werden
über die Webapp konsolidiert:

```
Rechner A: config_A.json → scan_A.db ─┐
Rechner B: config_B.json → scan_B.db ─┼→ idvscope.db (Import via Webapp)
Rechner C: config_C.json → scan_C.db ─┘
```

Import: `Admin → Scanner-Einstellungen → Scanner-Datenbank importieren`

### 2.13 Scheduled Task (Windows)

```
Aufgabenplanung → Neue Aufgabe
  Programm:  C:\idvscope\idvscope.exe
  Argumente: --scan --config C:\idvscope\config.json
  Trigger:   Wöchentlich, Montag 06:00
  Konto:     Dienstkonto mit Leserechten auf Shares
```

Alternativ mit Python:

```
python C:\IDV-Scanner\network_scanner.py --config C:\IDV-Scanner\config.json
```

### 2.14 Startdatum-Filter (`scan_since`)

Mit `scan_since` werden nur Dateien verarbeitet, die seit dem Datum
neu erstellt oder geändert wurden. Ältere Dateien werden übersprungen
**und nicht archiviert**.

```json
{ "scan_since": "2024-07-01" }
```

Typische Anwendungsfälle:

| Situation | Empfehlung |
|---|---|
| Ersteinrichtung mit großem Bestand, nur neuere Dateien relevant | `"scan_since": "2024-01-01"` |
| Quartals-Scan nur für aktuelle Änderungen | `"scan_since": "2025-01-01"` |
| Vollständige Erfassung aller Dateien | `"scan_since": null` (Standard) |

## 3 Teams-Scanner (optional)

### 3.1 Zweck

Der Teams-Scanner durchsucht über die Microsoft Graph API:

- Teams-Kanäle und deren SharePoint-Ordner
- Benutzer-OneDrives (optional)
- Erkennt IDV-relevante Dateitypen wie der Dateisystem-Scanner

### 3.2 Voraussetzungen

Es gibt zwei Modi. Welcher passt, hängt davon ab, wie der Tenant
betrieben wird:

#### 3.2.1 Standard-Modus (Default)

- Azure-AD-App-Registrierung mit folgenden Berechtigungen:
  - `Files.Read.All` – Dateien in SharePoint-Sites lesen (Inhalt + Delta)
  - `Sites.Read.All` – Site-Metadaten und Drive-Auflistung
- Client-Credentials-Flow (Application Permissions)
- Sowohl Microsoft-Teams-Team-IDs als auch SharePoint-Site-URLs als
  Quellen möglich

> Hinweis: Channel-Nachrichten oder Teams-Stammdaten werden nicht
> gescannt. `Team.ReadBasic.All` und `ChannelMessage.Read.All` sind
> daher **nicht** erforderlich und sollten aus
> Least-Privilege-Gründen auch nicht vergeben werden.

#### 3.2.2 Sites.Selected-Modus (für strikt verwaltete Tenants)

Empfohlen für rechenzentrumsbetriebene Tenants, in denen tenantweite
Lese-Permissions wie `Files.Read.All`/`Sites.Read.All` gesondert
bewertet werden. Der Tenant-Admin entscheidet pro Site, ob die App
zugreifen darf — standardmäßig hat sie keinen Zugriff.

**Aktivierung:** In `Administration → Teams-Einstellungen` den Schalter
„Sites.Selected-Modus" einschalten.

**Reicht `Sites.Selected` wirklich aus?** Ja. `Sites.Selected` ist
2021 von Microsoft genau deshalb eingeführt worden, um
`Files.Read.All` und `Sites.Read.All` für Drittanbieter-Apps
ablösbar zu machen. Die Permission gewährt von sich aus *null*
Zugriff — sie ist nur die Voraussetzung dafür, dass der Tenant-Admin
der App per Site explizit Rollen zuweisen kann. Die `read`-Rolle, die
über `POST /sites/{id}/permissions` gegrantet wird, umfasst auf der
betroffenen Site:

- Site-Metadaten (`GET /sites/{id}`) — vorher `Sites.Read.All`
- Drives/Lists auflisten (`GET /sites/{id}/drives`,
  `GET /sites/{id}/lists`) — vorher `Sites.Read.All`
- DriveItems inkl. Datei-Inhalt und Delta-Query
  (`GET /drives/{id}/root/delta`, `GET /drives/{id}/items/{id}/content`)
  — vorher `Files.Read.All`

Beide Tenant-Permissions entfallen damit vollständig.

**Azure-AD-App-Registrierung im Strict-Modus:**

- Application-Permission: **ausschließlich** `Sites.Selected`.
- **Bitte nicht zusätzlich vergeben:** `Files.Read.All`,
  `Sites.Read.All`, `Group.Read.All`. Sie sind nicht nur überflüssig,
  sondern würden den Sinn des Modus (kein tenantweiter Zugriff)
  aufheben — der Sicherheitsgewinn ginge verloren, weil die App dann
  doch wieder tenantweit lesen könnte.

**Was Sites.Selected nicht abdeckt (in der Regel irrelevant):**

- **Subsites** einer Site sind nicht im Grant der Parent-Site
  enthalten. Soll eine Subsite gescannt werden, braucht sie einen
  eigenen `POST /sites/{subsite-id}/permissions`-Aufruf.
- **User-OneDrive** (`/users/{id}/drive`, `/me/drive`) ist von
  `Sites.Selected` nicht abgedeckt. Für den Teams-Scanner ist das
  egal — gescannt werden Teams- und SharePoint-Sites, nicht
  persönliche OneDrives.
- Es muss exakt die `read`-Rolle sein. `write`/`fullcontrol` sind
  weder nötig noch sinnvoll (idvscope liest nur).

**Pro SharePoint-Site einmalig durch den Tenant-Admin:**

1. Site-ID nachschlagen (Graph-Explorer oder PowerShell):
   ```
   GET https://graph.microsoft.com/v1.0/sites/{hostname}:/sites/{site-name}
   ```
   Das Antwort-Feld `id` hat das Format
   `{hostname},{site-collection-guid},{site-guid}`.

2. Lese-Recht für die idvscope-App auf genau dieser Site granten:
   ```
   POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
   Content-Type: application/json

   {
     "roles": ["read"],
     "grantedToIdentities": [
       { "application": { "id": "<client-id>", "displayName": "idvscope" } }
     ]
   }
   ```

   Alternativ per PnP-PowerShell:
   ```powershell
   Grant-PnPAzureADAppSitePermission `
     -AppId      "<client-id>" `
     -DisplayName "idvscope" `
     -Site       "https://{tenant}.sharepoint.com/sites/{site-name}" `
     -Permissions Read
   ```

   Hinweis: Der Grant-Aufruf selbst benötigt einen Admin-Account oder
   ein Token mit `Sites.FullControl.All` — nur für diesen einmaligen
   Setup-Schritt; im laufenden Betrieb nicht.

3. In idvscope unter `Administration → Teams-Einstellungen` die
   Site-URL als Quelle eintragen. Team-IDs werden in diesem Modus
   übersprungen.

**Diagnose:** Fehlt der Site-Grant, antwortet Graph mit HTTP 403; der
Scanner protokolliert in dem Fall einen klar formulierten Hinweis auf
den fehlenden `POST /sites/{site-id}/permissions`-Aufruf.

### 3.3 Konfiguration

Administration → Teams-Einstellungen

| Feld | Inhalt |
|---|---|
| Tenant-ID | GUID der Azure-AD-Instanz |
| Client-ID | App-Registrierungs-ID |
| Client-Secret | Geheimschlüssel der App-Registrierung |
| Filter (optional) | Gruppen-DNs für Einschränkung |

### 3.4 Delta-Modus

Die Graph-API unterstützt Delta-Tokens, mit denen nur Änderungen seit
dem letzten Scan abgerufen werden. idvscope speichert das Delta-Token
und setzt bei jedem Scan genau dort auf.

## 4 Betriebsempfehlungen

### 4.1 Scan-Frequenz

| Umgebung | Frequenz |
|---|---|
| Großes Rechenzentrum mit vielen Änderungen | täglich (nachts) |
| Mittelgroße Bank | wöchentlich |
| Kleine Organisation | zweiwöchentlich |

### 4.2 Berechtigungen des Scanner-Kontos

- **Nur Leserechte** auf die zu scannenden Verzeichnisse
- Kein Schreib- oder Löschrecht erforderlich
- Kein Administrator-Zugang nötig

### 4.3 Netzwerk-Aspekte

- Scanner erzeugt je nach Shares hohen SMB-Traffic
- Empfehlung: Scanner zeitlich außerhalb der Geschäftszeiten betreiben
- Bei WAN-Verbindungen: `scan_since` zur Reduzierung nutzen

### 4.4 Ressourcenverbrauch (CPU / RAM) eingrenzen

Der Scanner ist aktuell single-threaded. Tritt auf dem Server hohe
CPU- oder RAM-Last auf, sind typische Ursachen blockierende
Netzwerk-API-Aufrufe oder speicherlastige Dateianalysen. Empfohlene
Stellschrauben — einzeln setzen und Wirkung messen:

| Symptom | Hebel | Wirkung |
|---|---|---|
| CPU dauerhaft bei ~100 %, Scan hängt auf Netzlaufwerken | `"read_file_owner": false` | Entfernt den blockierenden `GetFileSecurity`-Aufruf je Datei. Primärer Hebel. |
| Hohe RAM-Peaks bei großen Office-Dateien | `blacklist_paths` um die entsprechenden Pfade ergänzen | Die OOXML-Analyse liest pro Tabellenblatt den vollständigen XML-Inhalt (Makros, Formeln, Blattschutz); sie greift unabhängig von `hash_size_limit_mb`. |
| RAM wächst auf flachen Verzeichnissen (100 000+ Einträge je Ebene) | In Teilscans zerlegen (siehe 2.11) | Verzeichnislistings werden je Ebene vollständig ins RAM geladen. |
| SHA-256 auf sehr großen Dateien lastet die CPU aus | `"hash_size_limit_mb": 100` (oder kleiner) | Dateien über dem Limit erhalten `HASH_ERROR`; Änderungsstatus basiert dann nur auf mtime. |
| Scan wirkt DB-gebunden, viele Neuzugänge | `"move_detection": "disabled"` | Spart 1–2 DB-Queries je Neuzugang; verschobene Dateien werden jedoch als „archiviert + neu" behandelt. |
| Vollscan grundsätzlich zu lang | `"scan_since": "YYYY-MM-DD"` | Nur seit Stichtag geänderte Dateien werden verarbeitet. |

**Reihenfolge**: Bei akuten Ressourcenspitzen zuerst
`read_file_owner: false` setzen — das adressiert den häufigsten Fall
(blockierende Win32-API auf SMB-Shares). Weitere Hebel nur bei Bedarf
hinzunehmen, um die Wirkung klar zuordnen zu können.

**Diagnose auf dem Server**:
- Den Scanner-Subprozess im Taskmanager über die Spalte „Befehlszeile"
  identifizieren — er wird mit dem Argument `--scan` gestartet
  (Details-Tab → Spalte „Befehlszeile" einblenden).
- Die Scanner-Logdatei (`log_path`) auf Einträge wie
  „Hash-Berechnung unterbrochen" oder „Verzeichnis-Listing
  unterbrochen" prüfen; beide weisen auf Netzwerk-Blockaden hin.

### 4.5 Überwachung

- `scan_runs`-Tabelle anzeigen: Admin → Scanner → Scan-Läufe
- Log-Datei `network_scanner.log` prüfen
- Bei ungewöhnlicher Zunahme archivierter Dateien: manuelle Prüfung

## 5 Datenmodell des Scanners

Siehe [04 – Datenmodell](04-datenmodell.md) Abschnitt 4.

## 6 Sicherheitsaspekte

- Scanner benötigt weder Admin- noch Schreibrechte → minimale Angriffsfläche
- Das SHA-256-Hashing erfolgt vollständig lokal; keine Inhalte werden übertragen
- Metadaten (Dateiname, Pfad, Eigentümer) werden ins IDV-Register geschrieben und unterliegen dessen Datenklassifikation (siehe [04 – Datenmodell](04-datenmodell.md) Abschnitt 12)
- Teams-Scanner: Client-Secret bitte sicher verwalten (Umgebungsvariable oder Vault)
