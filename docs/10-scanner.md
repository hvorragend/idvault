# 10 – Scanner

---

## 1 Überblick

idvault umfasst zwei unabhängige Scanner-Komponenten zur Identifikation
von IDV-Kandidaten:

1. **Dateisystem-Scanner** (`scanner/eigenentwicklung_scanner.py`) – für
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
    "db_path": "instance/idvault.db"
  }
}
```

| Parameter | Typ | Standard | Beschreibung |
|---|---|---|---|
| `scan_paths` | Liste | `[]` | Zu scannende Pfade (UNC oder Laufwerksbuchstaben) |
| `extensions` | Liste | `.xlsx`, `.xlsm`, `.py`, `.sql`, … | Erfasste Dateierweiterungen |
| `exclude_paths` | Liste | `["~$", ".tmp", …]` | Pfadmuster, die ausgeschlossen werden |
| `db_path` | String | `"instance/idvault.db"` | Pfad zur SQLite-Datenbank |
| `log_path` | String | `"scanner/eigenentwicklung_scanner.log"` | Pfad zur Logdatei |
| `hash_size_limit_mb` | Integer | `500` | Dateien größer als dieser Wert werden nicht gehasht |
| `max_workers` | Integer | `4` | Reserviert (zukünftige Parallelisierung) |
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
Rechner B: config_B.json → scan_B.db ─┼→ idvault.db (Import via Webapp)
Rechner C: config_C.json → scan_C.db ─┘
```

Import: `Admin → Scanner-Einstellungen → Scanner-Datenbank importieren`

### 2.13 Scheduled Task (Windows)

```
Aufgabenplanung → Neue Aufgabe
  Programm:  C:\idvault\idvault.exe
  Argumente: --scan --config C:\idvault\config.json
  Trigger:   Wöchentlich, Montag 06:00
  Konto:     Dienstkonto mit Leserechten auf Shares
```

Alternativ mit Python:

```
python C:\IDV-Scanner\eigenentwicklung_scanner.py --config C:\IDV-Scanner\config.json
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

- Azure-AD-App-Registrierung mit folgenden Berechtigungen:
  - `Files.Read.All`
  - `Sites.Read.All`
  - `Team.ReadBasic.All`
  - `ChannelMessage.Read.All` (optional)
- Client-Credentials-Flow (Application Permissions)

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
dem letzten Scan abgerufen werden. idvault speichert das Delta-Token
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

### 4.4 Überwachung

- `scan_runs`-Tabelle anzeigen: Admin → Scanner → Scan-Läufe
- Log-Datei `eigenentwicklung_scanner.log` prüfen
- Bei ungewöhnlicher Zunahme archivierter Dateien: manuelle Prüfung

## 5 Datenmodell des Scanners

Siehe [04 – Datenmodell](04-datenmodell.md) Abschnitt 4.

## 6 Sicherheitsaspekte

- Scanner benötigt weder Admin- noch Schreibrechte → minimale Angriffsfläche
- Das SHA-256-Hashing erfolgt vollständig lokal; keine Inhalte werden übertragen
- Metadaten (Dateiname, Pfad, Eigentümer) werden ins IDV-Register geschrieben und unterliegen dessen Datenklassifikation (siehe [04 – Datenmodell](04-datenmodell.md) Abschnitt 12)
- Teams-Scanner: Client-Secret bitte sicher verwalten (Umgebungsvariable oder Vault)
