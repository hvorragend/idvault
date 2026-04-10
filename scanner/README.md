# IDV-Scanner – Installationsanleitung

## Voraussetzungen
- Python 3.10 oder neuer
- Windows (empfohlen für vollständige Metadaten inkl. Eigentümer)
- Netzwerkzugriff auf die zu scannenden Freigaben

## Installation

```cmd
pip install -r requirements.txt

REM Optional (Windows-Dateieigentümer):
pip install pywin32
```

## Konfiguration

```cmd
python idv_scanner.py --init-config
```
Öffnet `config.json` und trägt die Scan-Pfade ein:
```json
{
  "scan_paths": ["\\\\server01\\freigabe", "Z:\\"],
  ...
}
```

## Erster Scan

```cmd
python idv_scanner.py --config config.json
```

## Excel-Export

```cmd
python idv_export.py --db idv_register.db --output IDV_Export_2025.xlsx
```

## Als Scheduled Task (Windows)

1. Aufgabenplanung öffnen
2. Neue Aufgabe: `python C:\IDV-Scanner\idv_scanner.py --config C:\IDV-Scanner\config.json`
3. Trigger: wöchentlich (z.B. Montag 06:00 Uhr)
4. Ausführen als: Dienstkonto mit Lesezugriff auf alle Shares

## Datenbankstruktur

| Tabelle              | Inhalt                                          |
|----------------------|-------------------------------------------------|
| `idv_files`          | Aktuelle Dateiliste mit allen Metadaten         |
| `idv_file_history`   | Änderungshistorie (neu/geändert/gelöscht)       |
| `scan_runs`          | Protokoll aller Scan-Durchläufe                 |

## Nächste Schritte: IDV-Register

Die `idv_files`-Tabelle ist die **Grundgesamtheit**.  
Darauf aufbauend wird die Tabelle `idv_register` ergänzt:
- Steuerungsrelevanz (ja/nein)
- Rechnungslegungsrelevanz (ja/nein)
- GDA-Bewertung (1–4)
- Fachverantwortlicher
- Prüfstatus / Freigabe
- Nächste Prüfung (Wiedervorlage)
