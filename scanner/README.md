# IDV-Scanner

Scannt Netzlaufwerke und lokale Verzeichnisse nach IDV-Eigenentwicklungen
(Excel, Access, Python, SQL, Power BI u.a.), erhebt Metadaten und speichert
Ergebnisse in einer SQLite-Datenbank. Integriert sich mit der idvault-Webapp.

---

## Installation

```cmd
pip install -r requirements.txt

REM Optional (Windows-Dateieigentümer via pywin32):
pip install pywin32
```

---

## Schnellstart

```cmd
REM 1. Beispiel-Konfiguration erzeugen
python network_scanner.py --init-config

REM 2. config.json anpassen (Scan-Pfade, db_path)

REM 3. Scan starten
python network_scanner.py --config config.json
```

---

## Konfiguration (`config.json`)

| Parameter | Typ | Standard | Beschreibung |
|---|---|---|---|
| `scan_paths` | Liste | `[]` | Zu scannende Pfade (UNC oder Laufwerksbuchstaben) |
| `extensions` | Liste | `.xlsx`, `.xlsm`, `.py` … | Erfasste Dateierweiterungen |
| `exclude_paths` | Liste | `["~$", ".tmp", …]` | Pfadmuster, die ausgeschlossen werden |
| `db_path` | String | `"idv_register.db"` | Pfad zur SQLite-Datenbank |
| `log_path` | String | `"network_scanner.log"` | Pfad zur Logdatei |
| `hash_size_limit_mb` | Integer | `500` | Dateien größer als dieser Wert werden nicht gehasht |
| `max_workers` | Integer | `4` | Derzeit ohne Wirkung – der Scanner läuft single-threaded. Der Parameter ist für eine künftige Parallelisierung reserviert. |
| `move_detection` | String | `"name_and_hash"` | Modus der Verschiebe-Erkennung (s.u.) |
| `scan_since` | String\|null | `null` | Startdatum im Format `"YYYY-MM-DD"`. Nur Dateien, deren Änderungsdatum ≥ diesem Datum liegt, werden verarbeitet. `null` = alle Dateien. |
| `read_file_owner` | Boolean | `true` | Dateibesitzer über die Windows-API (`pywin32`) auslesen. Auf Netzlaufwerken kann dieser API-Aufruf den Scan stark verlangsamen oder mit einem Fehler abbrechen — in dem Fall auf `false` setzen. |

### Mehrere Teilscans (große Verzeichnisse aufteilen)

Sind die zu scannenden Verzeichnisse sehr groß, können sie auf mehrere Scan-Läufe
aufgeteilt werden. Dazu einfach pro Lauf einen eigenen `scan_paths`-Eintrag verwenden:

```json
// config_share1.json
{ "scan_paths": ["//srv/share1/Abteilung_A", "//srv/share1/Abteilung_B"] }

// config_share2.json
{ "scan_paths": ["//srv/share2"] }
```

```cmd
python network_scanner.py --config config_share1.json
python network_scanner.py --config config_share2.json
```

**Warum das funktioniert:**

Die Archivierungslogik (`mark_deleted_files`) berücksichtigt den Geltungsbereich
jedes Scan-Laufs. Nur Dateien, deren Pfad unter einem der gescannten Verzeichnisse
liegt **und** die in diesem Lauf nicht gefunden wurden, werden archiviert. Dateien
aus anderen Verzeichnissen bleiben unberührt.

```
Scan 1: //srv/share1/        → archiviert nur fehlende Dateien aus share1
Scan 2: //srv/share2/        → archiviert nur fehlende Dateien aus share2
                               Dateien aus share1 werden nicht angetastet ✅
```

**Hinweis:** Alle Teilscans sollten auf dieselbe Datenbank (`db_path`) zeigen,
damit die Ergebnisse akkumuliert werden.

---

### Startdatum-Filter (`scan_since`)

Mit `scan_since` werden nur Dateien verarbeitet, die seit einem bestimmten Datum
neu erstellt oder geändert wurden. Ältere Dateien werden **übersprungen** —
und, entscheidend, auch **nicht archiviert**: Die Archivierungslogik berücksichtigt
den Filter und markiert nur Dateien als archiviert, die tatsächlich im Datumsbereich
lagen und nicht mehr gefunden wurden.

```json
{
  "scan_since": "2024-07-01"
}
```

Verglichen wird das **Dateisystem-Änderungsdatum** (`mtime`) der Datei mit dem
konfigurierten Datum. Ist die Datei älter, wird sie in diesem Scan ignoriert.

**Typische Anwendungsfälle:**

| Situation | Empfehlung |
|---|---|
| Ersteinrichtung mit großem Bestand, nur neuere Dateien relevant | `"scan_since": "2024-01-01"` |
| Quartals-Scan nur für aktuelle Änderungen | `"scan_since": "2025-01-01"` |
| Vollständige Erfassung aller Dateien | `"scan_since": null` (Standard) |

**Hinweis:** `scan_since` filtert nur nach dem Dateisystem-Datum — nicht nach dem
Datum des ersten Fundes in der Datenbank. Eine Datei, die schon länger existiert
aber nie geändert wurde, wird bei `scan_since` übersprungen, auch wenn sie noch
nie gescannt wurde.

---

### Ressourcenverbrauch (CPU / RAM) eingrenzen

Der Scanner läuft single-threaded; hohe CPU- oder RAM-Last in einem Lauf
weisen typischerweise auf blockierende Netzwerk-API-Aufrufe oder auf
speicherlastige Analysen hin. Die wichtigsten Stellschrauben:

| Symptom | Hebel | Wirkung |
|---|---|---|
| CPU durchgehend bei ~100 %, Scan hängt an Netzwerk-Shares | `"read_file_owner": false` | Entfernt den blockierenden Windows-API-Aufruf für Datei-Eigentümer |
| Hoher RAM-Peak bei einzelnen Office-Dateien | `blacklist_paths` um Pfade mit sehr großen Excel-/PowerPoint-Dateien ergänzen | Die OOXML-Analyse (Makros, Formeln, Blattschutz) liest pro Tabellenblatt den vollständigen XML-Inhalt in den Speicher — bei Dateien mit dutzenden großen Blättern summiert sich das. Die OOXML-Analyse greift unabhängig von `hash_size_limit_mb`. |
| RAM wächst bei sehr flachen Verzeichnissen (100 000+ Einträgen auf einer Ebene) | In Teilscans zerlegen (s.o.) oder `blacklist_paths` präzisieren | Verzeichnislistings werden je Ebene vollständig ins RAM gezogen; kleinere Teilbäume halten den Peak klein. |
| Hoher CPU-Anteil durch SHA-256 auf riesige Dateien | `"hash_size_limit_mb": 100` (oder kleiner) | Dateien über dem Limit erhalten `HASH_ERROR` und werden nicht gehasht. |
| Viele Neuzugänge, Scan wirkt DB-gebunden | `"move_detection": "disabled"` | Spart 1–2 DB-Queries pro Neuzugang. Nur verwenden, wenn Move-Tracking fachlich nicht gebraucht wird — verschobene Dateien werden sonst als „archiviert + neu" behandelt. |
| Vollscan dauert grundsätzlich zu lange | `"scan_since": "YYYY-MM-DD"` | Nur seit dem Stichtag geänderte Dateien werden verarbeitet. |

**Reihenfolge bei akuten Problemen**: zuerst `read_file_owner: false`
setzen und die Wirkung messen, dann weitere Hebel einzeln aktivieren.

**Zur Diagnose auf dem Server** helfen:
- `tasklist /V | findstr idvault` bzw. Taskmanager → Details → Spalte
  „Befehlszeile" zeigt den Scanner-Subprozess (Argument `--scan`).
- Die Log-Datei (`log_path`, Standard `network_scanner.log`) auf Einträge
  wie „Hash-Berechnung unterbrochen" oder „Verzeichnis-Listing
  unterbrochen" prüfen — typische Hinweise auf Netzwerk-Blockaden.

---

### Integration mit der idvault-Webapp

Damit Scanner und Webapp dieselbe Datenbank nutzen, `db_path` auf die
Instanz-Datenbank der Webapp zeigen lassen:

```json
{
  "db_path": "../instance/idvault.db",
  "scan_paths": ["\\\\server01\\freigabe"]
}
```

---

## Datei-Stati

Jede erfasste Datei hat einen Status in `idv_files.status`:

| Status | Bedeutung |
|---|---|
| `active` | Datei wurde beim letzten Scan gefunden. |
| `archiviert` | Datei wurde beim letzten Scan **nicht** mehr gefunden – sie wurde möglicherweise verschoben, umbenannt oder gelöscht. Die Zeile bleibt in der Datenbank erhalten; Verknüpfungen zum IDV-Register (über `file_id`) bleiben gültig. |

### Status-Übergänge

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

Scan: Verschobene Datei erkannt (Move-Detection aktiv)
  active (alter Pfad) → active (neuer Pfad)  (change_type: moved)
  ↳ Kein neuer Datensatz; DB-ID und IDV-Register-Verknüpfung bleiben erhalten.
```

---

## Delta-Erkennung und Historie

Jeder Scan schreibt Einträge in `idv_file_history`:

| `change_type` | Wann |
|---|---|
| `new` | Datei zum ersten Mal gefunden |
| `unchanged` | Datei gefunden, Hash identisch zum letzten Scan |
| `changed` | Datei gefunden, Hash hat sich geändert (Inhalt wurde geändert) |
| `moved` | Datei an neuem Pfad gefunden, gleicher Hash erkannt |
| `archiviert` | Datei nicht mehr gefunden → ins Archiv überführt |
| `restored` | Archivierte Datei am gleichen Pfad wieder aufgetaucht |

Für `moved`-Einträge enthält die Spalte `details` ein JSON-Objekt:

```json
{"old_path": "//srv/alt/bericht.xlsm", "new_path": "//srv/neu/bericht.xlsm"}
```

### Hash-Berechnung

- Algorithmus: **SHA-256** über den vollständigen Dateiinhalt
- Dateien, die nicht gelesen werden können (Berechtigungsfehler), erhalten den
  Platzhalterwert `HASH_ERROR` und werden in der Move-Detection ignoriert.
- Dateien, die `hash_size_limit_mb` überschreiten, werden ebenfalls nicht
  gehasht (`HASH_ERROR`). Ihr Änderungsstatus basiert dann nur auf dem
  Dateisystem-Änderungsdatum.

---

## Move-Detection

Konfiguriert über den Parameter `move_detection` in `config.json`.

### `"name_and_hash"` (Standard, empfohlen)

Eine Datei gilt als verschoben, wenn unter dem neuen Pfad noch kein Eintrag
existiert **und** eine aktive Datei mit **gleichem SHA-256-Hash** und
**gleichem Dateinamen** gefunden wird.

```
Szenario                                               Ergebnis
────────────────────────────────────────────────────── ─────────────────
//srv/alt/bericht.xlsm → //srv/neu/bericht.xlsm        ✅ moved
//srv/alt/bericht.xlsm → //srv/neu/bericht_v2.xlsm     ❌ archiviert + new
//srv/alt/bericht.xlsm → //srv/neu/bericht.xlsm         ❌ archiviert + new
  (Inhalt geändert)
```

**Wann geeignet:** Standardfall – Dateien werden verschoben, behalten aber
ihren Dateinamen.

---

### `"hash_only"`

Eine Datei gilt als verschoben, wenn unter dem neuen Pfad noch kein Eintrag
existiert **und** genau **eine** aktive Datei mit gleichem SHA-256-Hash gefunden
wird (Eindeutigkeitsprüfung).

Bei **mehreren** Treffern (z.B. mehrere Kopien einer Vorlage mit identischem
Inhalt) ist keine eindeutige Zuordnung möglich → die neue Datei wird als `new`
behandelt.

```
Szenario                                               Ergebnis
────────────────────────────────────────────────────── ─────────────────
//srv/alt/bericht.xlsm → //srv/neu/bericht.xlsm        ✅ moved
//srv/alt/bericht.xlsm → //srv/neu/neuer_name.xlsm     ✅ moved
  (Name darf sich ändern)
vorlage.xlsm (2 Kopien, gleicher Hash) umbenannt       ❌ new (mehrdeutig)
Inhalt geändert + verschoben                           ❌ archiviert + new
```

**Wann geeignet:** Wenn Dateien häufig umbenannt und verschoben werden und
trotzdem wiedererkannt werden sollen. Vorsicht bei Vorlagen-Dateien mit
identischem Inhalt in mehreren Exemplaren.

---

### `"disabled"`

Keine Move-Detection. Jede Datei, die nicht mehr am bekannten Pfad liegt,
wird archiviert. Dateien an neuen Pfaden werden immer als `new` angelegt.

**Wann geeignet:** Wenn Fehlzuordnungen ausgeschlossen werden sollen oder das
Dateisystem viele Dateien mit identischem Inhalt enthält.

---

## Archivierte Dateien

Archivierte Dateien werden **nicht gelöscht**. Sie bleiben mit
`status = 'archiviert'` in `idv_files` erhalten.

**Warum das wichtig ist:**
Ist eine Datei über `idv_register.file_id` mit einem IDV-Eintrag verknüpft,
bleibt diese Verknüpfung auch nach der Archivierung gültig. Im idvault-Interface
unter *Scanner → Scanner-Funde → Archiv* sind alle archivierten Dateien einsehbar,
inklusive des Datums des letzten Fundes.

**Automatische Wiederherstellung:**
Taucht eine archivierte Datei beim nächsten Scan am gleichen Pfad wieder auf
(z.B. nach Netzwerkunterbrechung), wird sie automatisch reaktiviert
(`change_type = 'restored'`).

---

## Scan-Statistik (`scan_runs`)

Nach jedem Scan wird ein Protokoll-Eintrag in `scan_runs` gespeichert:

| Spalte | Bedeutung |
|---|---|
| `total_files` | Anzahl gefundener Dateien in diesem Scan |
| `new_files` | Erstmalig gefundene Dateien |
| `changed_files` | Dateien mit geändertem Hash (Inhalt geändert) |
| `moved_files` | Als verschoben erkannte Dateien |
| `restored_files` | Aus dem Archiv wiederhergestellte Dateien |
| `archived_files` | In diesem Scan archivierte Dateien |
| `errors` | Dateien/Pfade mit Verarbeitungsfehlern |

---

## Excel-Export

```cmd
python excel_export.py --db idv_register.db --output IDV_Export.xlsx
```

---

## Pause, Abbrechen und Checkpoint

Bei sehr großen Netzwerkstrukturen kann der Scan **pausiert**, **abgebrochen** und
später **fortgesetzt** werden.

### Steuerung über die Webapp

Die Scan-Schaltfläche in allen Scanner-Ansichten zeigt je nach Zustand:

| Zustand | Angezeigte Buttons |
|---|---|
| Scan läuft | **Pause** · **Abbrechen** |
| Scan pausiert | **Fortsetzen** · **Abbrechen** |
| Scan abgebrochen, Checkpoint vorhanden | **Scan fortsetzen** · **Neu starten** |
| Kein aktiver Scan | **Scan starten** |

Ein Abbruch hält den Fortschritt in einer Checkpoint-Datei fest. Beim nächsten
Start über **„Scan fortsetzen"** (oder `--resume`) werden bereits abgeschlossene
Verzeichnisse übersprungen.

### Steuerung über Signaldateien (CLI / Taskplanung)

Im Scanner-Verzeichnis (Ordner der `config.json`) werden folgende Dateien ausgewertet:

| Datei | Bedeutung |
|---|---|
| `scanner_pause.signal` | Existiert = Pause nach dem nächsten Verzeichnis |
| `scanner_cancel.signal` | Existiert = Abbruch nach dem nächsten Verzeichnis |
| `scanner_checkpoint.json` | Fortschrittsstand (automatisch geschrieben/gelöscht) |

**Pause anlegen:**
```cmd
echo. > C:\idvault\scanner\scanner_pause.signal
```

**Pause aufheben (Scan fortsetzen):**
```cmd
del C:\idvault\scanner\scanner_pause.signal
```

**Abbrechen:**
```cmd
echo. > C:\idvault\scanner\scanner_cancel.signal
```

**Nach Abbruch fortsetzen:**
```cmd
idvault.exe --scan --config C:\idvault\scanner\config.json --resume
REM oder:
python network_scanner.py --config config.json --resume
```

### Granularität des Checkpoints

Der Checkpoint wird nach jedem vollständig abgeschlossenen **Top-Level-Unterverzeichnis**
geschrieben. Bei einem 10-TB-Share mit 20 Abteilungsordnern kann ein abgebrochener
Scan z.B. bei Abteilung 14 wiederaufgenommen werden – ohne die ersten 13 erneut zu scannen.

---

## Multi-Scanner – Mehrere Rechner parallel

### Strategie 1: Gemeinsame Datenbank (sequentieller Betrieb)

Alle Scanner schreiben in dieselbe `.db`-Datei auf dem Server:

```json
{
  "db_path": "\\\\server\\idvault\\instance\\idvault.db",
  "scan_paths": ["\\\\server\\freigabe\\Abteilung_A"]
}
```

SQLite im WAL-Modus (bereits aktiv) verträgt mehrere **sequentielle** Schreiber
gut. **Gleichzeitige** Schreiber von verschiedenen Rechnern können die Datenbank
beschädigen.

→ **Empfehlung:** Nur wenn die Scans zeitlich getrennt laufen (z.B. per Scheduled
  Task zu verschiedenen Uhrzeiten).

---

### Strategie 2: Separate Datenbanken + Import (empfohlen für Parallelbetrieb)

Jeder Rechner scannt in eine eigene lokale `.db`-Datei. Anschließend werden die
Ergebnisse über die Webapp zusammengeführt.

```
Rechner A: config_A.json  →  scan_A.db  ─┐
Rechner B: config_B.json  →  scan_B.db  ─┼→  idvault.db  (Import via Webapp)
Rechner C: config_C.json  →  scan_C.db  ─┘
```

**Konfiguration (Beispiel Rechner B):**
```json
{
  "db_path": "C:\\Scans\\scan_B.db",
  "scan_paths": ["\\\\server\\freigabe\\Abteilung_B"]
}
```

**Import in die zentrale Datenbank:**

1. `scan_B.db` auf den Server kopieren
2. In idvault: *Admin → Scanner-Einstellungen → Scanner-Datenbank importieren*
3. Pfad zur kopierten Datei angeben und **Importieren** klicken

**Ergebnis:** Alle Dateien aus `scan_B.db` erscheinen in Scanner-Funde.
Vorhandene Pfade werden nicht dupliziert – neuere Scan-Daten überschreiben ältere.

**Vorteile:**
- Scans laufen parallel ohne Locking-Probleme
- Netzwerkunterbrechungen beeinflussen andere Rechner nicht
- Jeder Scanner hat einen eigenen Checkpoint

---

## Als Scheduled Task (Windows)

**Option A – Standalone-Executable (idvault.exe)**

Wenn idvault als Standalone-Executable betrieben wird, übernimmt dieselbe
Datei auch den Scanner-Modus:

1. Aufgabenplanung öffnen
2. Neue Aufgabe:
   ```
   C:\idvault\idvault.exe --scan --config C:\idvault\scanner\config.json
   ```
3. Trigger: wöchentlich (z.B. Montag 06:00 Uhr)
4. Ausführen als: Dienstkonto mit Lesezugriff auf alle Shares

**Option B – Python-Skript (Quellinstallation)**

1. Aufgabenplanung öffnen
2. Neue Aufgabe: `python C:\IDV-Scanner\network_scanner.py --config C:\IDV-Scanner\config.json`
3. Trigger: wöchentlich (z.B. Montag 06:00 Uhr)
4. Ausführen als: Dienstkonto mit Lesezugriff auf alle Shares

---

## Datenbankschema (Überblick)

```
scan_runs        – ein Eintrag pro Scan-Lauf mit Statistik
idv_files        – eine Zeile pro bekannter Datei (aktiv oder archiviert)
idv_file_history – lückenlose Änderungshistorie pro Datei und Scan-Lauf
```

Die `id`-Spalte in `idv_files` wird **niemals geändert** – auch nicht bei
Verschiebung oder Umbenennung. So bleibt die Verknüpfung
`idv_register.file_id → idv_files.id` dauerhaft konsistent.
