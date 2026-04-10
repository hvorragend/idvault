## Was der Scanner macht

**`idv_scanner.py`** – Der Kern:
- Scannt alle konfigurierten UNC-Pfade/Laufwerke rekursiv
- Erhebt: SHA-256-Hash, Dateigröße, Erstelldatum, Änderungsdatum, Windows-Eigentümer
- Analysiert OOXML-Dateien (Excel, Word, PowerPoint) direkt als ZIP – ohne Office zu öffnen: Autor, letzter Bearbeiter, VBA-Makros, externe Verknüpfungen, Tabellenblattanzahl, benannte Bereiche
- Erkennt **Delta** automatisch: neue / geänderte / gelöschte Dateien zwischen Scan-Runs
- Schreibt alles in SQLite mit vollständiger Änderungshistorie

**`idv_export.py`** – Der Excel-Report mit 4 Sheets:
- **IDV-Grundgesamtheit** – alle Dateien als formatierte Tabelle, Makro-Dateien gelb markiert
- **Scan-Übersicht** – alle bisherigen Scan-Runs mit Statistik
- **Änderungen** – Delta-Report (neu/geändert/gelöscht) farblich kodiert
- **Statistik** – Verteilung nach Dateityp, Makro-Anzahl, externe Links

---

## Sofort-Start

```cmd
pip install -r requirements.txt
pip install pywin32          # für Windows-Eigentümer

python idv_scanner.py --init-config   # erzeugt config.json
# → scan_paths in config.json eintragen
python idv_scanner.py                 # erster Scan
python idv_export.py                  # Excel-Export
```

