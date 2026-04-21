"""
Scanner-Protokoll: NDJSON-stdout-Events vom Scanner-Subprozess zur Webapp
========================================================================

Der Scanner-Subprozess schreibt ausschliesslich lesend auf die SQLite-
Datenbank (Move-Detection, Resume-Checkpoints usw.). Jeder schreibende
Vorgang wird stattdessen als eine Zeile JSON ueber stdout emittiert.

Die Webapp startet den Scanner als Subprocess mit ``stdout=PIPE`` und
liest jede Zeile in einem Reader-Thread ein, der sie ueber
``db_writer.get_writer().submit(...)`` auf die eine, prozesslokale
Writer-Connection der Webapp anwendet. Damit bleibt die SQLite-Datei
aus Sicht beider Prozesse konfliktfrei (kein ``database is locked``).

Format pro Zeile:
    {"op": "<op-name>", ...payload}\n

Nicht-JSON-Zeilen, die der Scanner auf stdout schreibt (z. B. ueber
print-Aufrufe in Third-Party-Code), landen im stdout-Reader als
"unbekannte Zeile" und werden nach ``instance/logs/scanner_output.log``
weitergereicht.
"""

from __future__ import annotations

import json
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Op-Konstanten — muessen zwischen Scanner und Webapp-Handler uebereinstimmen.
# ---------------------------------------------------------------------------

OP_START_RUN     = "start_run"       # scan_runs: INSERT / UPDATE status='running'
OP_END_RUN       = "end_run"         # scan_runs: UPDATE status + stats
OP_UPSERT_FILE   = "upsert_file"     # idv_files: INSERT (neu) oder UPDATE (bekannt)
OP_MOVE_FILE     = "move_file"       # idv_files: UPDATE full_path; History 'moved'
OP_ARCHIVE_FILES = "archive_files"   # idv_files: batch status='archiviert'
OP_UPDATE_STATUS = "update_status"   # idv_files.bearbeitungsstatus
OP_FILE_HISTORY  = "file_history"    # idv_file_history: INSERT (Einzelzeile)
OP_LOG              = "log"              # reiner Textlog-Eintrag (Webapp persistiert nicht)
OP_PROGRESS         = "progress"         # Fortschrittsmeldung fuer UI (nicht persistiert)
OP_SAVE_DELTA_TOKEN = "save_delta_token" # teams_delta_tokens: UPSERT (inkrementeller Scan)


def emit(op: str, **payload: Any) -> None:
    """Gibt genau eine NDJSON-Zeile auf stdout aus und flusht.

    ``payload`` muss JSON-serialisierbar sein. Zusaetzliche Keys werden
    als zusammengefuehrte Datenstruktur gesendet; ``op`` wird stets
    als erstes Feld geschrieben.
    """
    line = json.dumps({"op": op, **payload}, ensure_ascii=False, default=str)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
