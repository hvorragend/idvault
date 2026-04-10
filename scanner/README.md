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
python idv_scanner.py --init-config

REM 2. config.json anpassen (Scan-Pfade, db_path)

REM 3. Scan starten
python idv_scanner.py --config config.json
```

---

## Konfiguration (`config.json`)

| Parameter | Typ | Standard | Beschreibung |
|---|---|---|---|
| `scan_paths` | Liste | `[]` | Zu scannende Pfade (UNC oder Laufwerksbuchstaben) |
| `extensions` | Liste | `.xlsx`, `.xlsm`, `.py` … | Erfasste Dateierweiterungen |
| `exclude_paths` | Liste | `["~$", ".tmp", …]` | Pfadmuster, die ausgeschlossen werden |
| `db_path` | String | `"idv_register.db"` | Pfad zur SQLite-Datenbank |
| `log_path` | String | `"idv_scanner.log"` | Pfad zur Logdatei |
| `hash_size_limit_mb` | Integer | `500` | Dateien größer als dieser Wert werden nicht gehasht |
| `max_workers` | Integer | `4` | Reserviert (zukünftige Parallelisierung) |
| `move_detection` | String | `"name_and_hash"` | Modus der Verschiebe-Erkennung (s.u.) |
| `scan_since` | String\|null | `null` | Startdatum im Format `"YYYY-MM-DD"`. Nur Dateien, deren Änderungsdatum ≥ diesem Datum liegt, werden verarbeitet. `null` = alle Dateien. |

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
python idv_scanner.py --config config_share1.json
python idv_scanner.py --config config_share2.json
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
python idv_export.py --db idv_register.db --output IDV_Export.xlsx
```

---

## Als Scheduled Task (Windows)

1. Aufgabenplanung öffnen
2. Neue Aufgabe: `python C:\IDV-Scanner\idv_scanner.py --config C:\IDV-Scanner\config.json`
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
