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
import threading
from typing import Any

# Stdout/Stderr explizit auf UTF-8 zwingen, *bevor* die erste NDJSON-Zeile
# rausgeht. Die Webapp liest den Subprozess-Stream mit
# ``encoding="utf-8", errors="replace"``; faellt der Scanner auf cp1252
# zurueck (Windows-Dienstkontext oder PyInstaller-Bundle, in denen die
# vom Webapp-Parent gesetzten ``PYTHONIOENCODING``/``PYTHONUTF8``-Env-Vars
# nicht greifen), landen Umlaute als Single-Byte (0xE4 = ä, 0xDC = Ü)
# in der Pipe. Diese Bytes sind keine gueltigen UTF-8-Startbytes – die
# Webapp ersetzt sie durch U+FFFD ("�"), und so landen Datei- bzw.
# Ordnernamen mit Umlauten verstuemmelt in der DB.
# ``reconfigure`` ist ab Python 3.7 verfuegbar; der Fallback schuetzt
# vor exotischen Stdout-Wrappern (z. B. wenn jemand ``sys.stdout``
# vorab durch ein eigenes Objekt ersetzt hat).
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            # Reconfigure ist Best-Effort fuer print()/logging.StreamHandler.
            # Fuer NDJSON ist es egal: ``emit()`` schreibt UTF-8-Bytes
            # direkt durch ``sys.stdout.buffer`` (siehe unten).
            pass

# ``emit`` kann aus mehreren Worker-Threads aufgerufen werden (Share-Level-
# Parallelisierung im Scanner). ``sys.stdout.write`` ist nicht atomar,
# daher serialisieren wir Zeile + Flush mit einem Lock – so koennen
# NDJSON-Zeilen nicht ineinanderlaufen.
_EMIT_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Op-Konstanten — muessen zwischen Scanner und Webapp-Handler uebereinstimmen.
# ---------------------------------------------------------------------------

OP_START_RUN     = "start_run"       # scan_runs: INSERT / UPDATE status='running'
OP_END_RUN       = "end_run"         # scan_runs: UPDATE status + stats
OP_UPSERT_FILE   = "upsert_file"     # idv_files: INSERT (neu) oder UPDATE (bekannt)
OP_MOVE_FILE     = "move_file"       # idv_files: UPDATE full_path; History 'moved'
OP_ARCHIVE_FILES = "archive_files"   # idv_files: batch status='archiviert'
OP_ARCHIVE_UNSEEN = "archive_unseen" # idv_files: archive all not-seen-this-run files
                                     # (Webapp-Writer fuehrt Query aus -> sieht eigene
                                     #  Upserts bereits committed, keine stale reads)
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

    Die Zeile wird als UTF-8-Bytes direkt durch ``sys.stdout.buffer``
    geschrieben — das umgeht das ``TextIOWrapper``-Encoding komplett.
    Damit bleibt der Scanner-Output auch dann korrekt, wenn das oben
    stehende ``reconfigure`` (aus welchen Gruenden auch immer) nicht
    greifen sollte: die Webapp liest die Pipe als UTF-8 und sieht so
    immer die richtigen Bytes. Fallback auf den Text-Pfad nur, wenn
    ``sys.stdout`` keinen ``buffer`` hat (z. B. wenn jemand ihn durch
    eine eigene Klasse ersetzt hat).
    """
    line = json.dumps({"op": op, **payload}, ensure_ascii=False, default=str)
    payload_bytes = (line + "\n").encode("utf-8")
    with _EMIT_LOCK:
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write(payload_bytes)
            buf.flush()
        else:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
