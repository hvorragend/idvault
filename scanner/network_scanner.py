"""
IDV-Scanner – Individuelle Datenverarbeitung Discovery Tool
===========================================================
Scannt Netzlaufwerke rekursiv nach IDV-Eigenentwicklungen,
erhebt Metadaten, berechnet SHA-256-Hashes und speichert
Ergebnisse in einer SQLite-Datenbank.

Autor:  IDV-Register Projekt
Lizenz: intern
"""

import os
import sys
import time
import getpass
import hashlib
import sqlite3
import zipfile
import logging
import argparse
import json
import traceback
import ctypes
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple
from xml.etree import ElementTree as ET

# Windows-spezifische Imports (optional, graceful fallback)
try:
    import win32security
    import win32api
    import ntsecuritycon
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

try:
    from path_utils import apply_path_mappings, should_pass_filters
except ImportError:
    from scanner.path_utils import apply_path_mappings, should_pass_filters

try:
    from scanner_protocol import (
        emit,
        OP_START_RUN, OP_END_RUN, OP_UPSERT_FILE, OP_MOVE_FILE,
        OP_ARCHIVE_FILES, OP_ARCHIVE_UNSEEN,
        OP_UPDATE_STATUS, OP_FILE_HISTORY,
        OP_LOG, OP_PROGRESS,
    )
except ImportError:
    from scanner.scanner_protocol import (
        emit,
        OP_START_RUN, OP_END_RUN, OP_UPSERT_FILE, OP_MOVE_FILE,
        OP_ARCHIVE_FILES, OP_ARCHIVE_UNSEEN,
        OP_UPDATE_STATUS, OP_FILE_HISTORY,
        OP_LOG, OP_PROGRESS,
    )


def _set_keep_awake(active: bool) -> None:
    """Verhindert Bildschirmschoner und Systemschlaf während des Scans (nur Windows).

    Ruft SetThreadExecutionState aus kernel32.dll auf:
    - active=True:  ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    - active=False: ES_CONTINUOUS  (Reset auf normales Verhalten)
    """
    if os.name != 'nt':
        return
    try:
        ES_CONTINUOUS       = 0x80000000
        ES_SYSTEM_REQUIRED  = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        state = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if active else ES_CONTINUOUS
        ctypes.windll.kernel32.SetThreadExecutionState(state)
    except Exception:
        pass  # Nicht-kritisch; Scan läuft weiter

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "scan_paths": [
        # Beispiel: "//server01/freigabe",
        # Beispiel: "Z:\\"
    ],
    "extensions": [
        ".xls", ".xlsx", ".xlsm", ".xlsb", ".xltm", ".xltx",
        ".accdb", ".mdb", ".accde", ".accdr",
        ".ida", ".idv",
        ".bas", ".cls", ".frm",
        ".pbix", ".pbit",
        ".dotm", ".pptm",
        ".py", ".r", ".rmd",
        ".sql"
    ],
    "exclude_paths": [],
    "blacklist_paths": [
        "~$",
        ".tmp",
        r"\$RECYCLE\.BIN",
        "System Volume Information",
        "AppData",
    ],
    "whitelist_paths": [],
    "path_mappings": [],
    "hash_size_limit_mb": 500,   # Dateien > X MB werden nicht gehasht (Performance)
    "max_workers": 4,
    # Parallelisierung auf Freigaben-/Top-Verzeichnis-Ebene. Wert 1 = seriell.
    # Werte > 1 starten pro Top-Level-Unterverzeichnis einen Thread (mit
    # eigener read-only SQLite-Connection). Sinnvoll bei mehreren langsamen
    # Netzlaufwerken; lokal zumeist ohne Nutzen.
    "parallel_shares": 1,
    # Dateibesitzer via Windows-API lesen (pywin32 erforderlich).
    # Auf Netzlaufwerken kann dies den Scan stark verlangsamen oder
    # mit einem KeyboardInterrupt abstürzen → bei Problemen auf false setzen.
    "read_file_owner": True,
    # Move-Detection-Modus:
    #   "name_and_hash" (Standard) – gleicher Hash UND gleicher Dateiname
    #   "hash_only"                – gleicher Hash, Dateiname darf sich geändert haben;
    #                                nur wenn genau ein aktiver Treffer (Eindeutigkeit)
    #   "disabled"                 – keine Move-Detection; verschobene Dateien werden
    #                                archiviert und als neue Datei neu angelegt
    "move_detection": "hash_only",
    # Startdatum für den Scan (ISO-Format: "YYYY-MM-DD" oder null für alle Dateien).
    # Nur Dateien, deren Dateisystem-Änderungsdatum >= scan_since liegt, werden
    # verarbeitet. Ältere Dateien werden übersprungen und NICHT archiviert.
    # Beispiel: "2024-07-01" erfasst nur Dateien, die ab dem 01.07.2024 neu
    # erstellt oder geändert wurden.
    "scan_since": None
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_path: str) -> logging.Logger:
    logger = logging.getLogger("IDVScanner")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def _flush_log(logger: logging.Logger) -> None:
    """Flusht alle Handler, damit Log-Einträge auch bei Crash auf der Platte landen."""
    for handler in logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Datenbank
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    scan_paths      TEXT,
    total_files     INTEGER DEFAULT 0,
    new_files       INTEGER DEFAULT 0,
    changed_files   INTEGER DEFAULT 0,
    moved_files     INTEGER DEFAULT 0,
    restored_files  INTEGER DEFAULT 0,
    archived_files  INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    scan_status     TEXT NOT NULL DEFAULT 'completed'
);

CREATE TABLE IF NOT EXISTS idv_files (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash               TEXT NOT NULL,          -- SHA-256, eindeutiger Fingerabdruck
    full_path               TEXT NOT NULL,
    file_name               TEXT NOT NULL,
    extension               TEXT NOT NULL,
    share_root              TEXT,                   -- UNC-Root / Laufwerksbuchstabe
    relative_path           TEXT,                   -- Pfad relativ zum Share-Root
    size_bytes              INTEGER,
    created_at              TEXT,                   -- Dateisystem-Erstelldatum (UTC)
    modified_at             TEXT,                   -- Dateisystem-Änderungsdatum (UTC)
    file_owner              TEXT,                   -- Windows-Eigentümer (SID/Name)
    office_author           TEXT,                   -- dc:creator aus OOXML
    office_last_author      TEXT,                   -- cp:lastModifiedBy aus OOXML
    office_created          TEXT,                   -- dcterms:created aus OOXML
    office_modified         TEXT,                   -- dcterms:modified aus OOXML
    has_macros              INTEGER DEFAULT 0,       -- 1 = VBA-Projekt vorhanden
    has_external_links      INTEGER DEFAULT 0,       -- 1 = externe Verknüpfungen
    sheet_count             INTEGER,                -- Anzahl Tabellenblätter (Excel)
    named_ranges_count      INTEGER,                -- Anzahl benannter Bereiche
    formula_count           INTEGER DEFAULT 0,       -- Anzahl Formelzellen (Excel)
    has_sheet_protection    INTEGER DEFAULT 0,       -- 1 = mind. 1 Blatt ist geschützt
    protected_sheets_count  INTEGER DEFAULT 0,       -- Anzahl geschützter Blätter
    sheet_protection_has_pw INTEGER DEFAULT 0,       -- 1 = mind. 1 Passwort-Hash gesetzt
    workbook_protected      INTEGER DEFAULT 0,       -- 1 = Arbeitsmappenschutz aktiv
    first_seen_at           TEXT NOT NULL,          -- Erster Fund (UTC)
    last_seen_at            TEXT NOT NULL,          -- Letzter Scan (UTC)
    last_scan_run_id        INTEGER,
    status                  TEXT DEFAULT 'active',  -- active | deleted | moved
    UNIQUE(full_path)
);

CREATE TABLE IF NOT EXISTS idv_file_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id         INTEGER NOT NULL,
    scan_run_id     INTEGER NOT NULL,
    change_type     TEXT NOT NULL,              -- new | changed | deleted | unchanged
    old_hash        TEXT,
    new_hash        TEXT,
    changed_at      TEXT NOT NULL,
    details         TEXT,                       -- JSON mit geänderten Feldern
    FOREIGN KEY(file_id) REFERENCES idv_files(id)
);

CREATE INDEX IF NOT EXISTS idx_files_hash      ON idv_files(file_hash);
CREATE INDEX IF NOT EXISTS idx_files_path      ON idv_files(full_path);
CREATE INDEX IF NOT EXISTS idx_files_ext       ON idv_files(extension);
CREATE INDEX IF NOT EXISTS idx_files_modified  ON idv_files(modified_at);
CREATE INDEX IF NOT EXISTS idx_history_file    ON idv_file_history(file_id);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """Oeffnet eine rein lesende Connection fuer den Scanner-Subprozess.

    Der Scanner schreibt seit Einfuehrung des db_writer-Patterns nicht mehr
    direkt in die SQLite-Datei; alle Schreibvorgaenge werden als NDJSON
    auf stdout emittiert und vom Webapp-Writer-Thread appliziert. Die
    Schema-Initialisierung liegt daher ausschliesslich bei der Webapp
    (``db.init_register_db``). Diese Funktion gibt lediglich eine
    Reader-Connection mit den gemeinsamen PRAGMAs zurueck.
    """
    try:
        from db_pragmas import apply_pragmas
    except ImportError:  # pragma: no cover — defensiv, falls Sidecar
        import sys as _sys
        import os as _os
        _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        from db_pragmas import apply_pragmas

    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    apply_pragmas(conn, role="reader")
    return conn


# ---------------------------------------------------------------------------
# Hash-Berechnung
# ---------------------------------------------------------------------------

def sha256_file(path: str, chunk_size: int = 65536,
                logger: logging.Logger = None) -> Optional[str]:
    """SHA-256-Hash einer Datei. Gibt None zurück bei Fehler."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                h.update(chunk)
        return h.hexdigest()
    except (PermissionError, OSError) as e:
        if logger:
            logger.debug(f"Hash-Fehler (übersprungen): {path} – {e}")
        return None
    except BaseException as e:
        # Windows kann auf Netzlaufwerken ein Control-Signal senden
        # (KeyboardInterrupt). Hash dann einfach überspringen.
        if logger:
            logger.warning(f"Hash-Berechnung unterbrochen: {path} – {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# Betriebssystem-Metadaten
# ---------------------------------------------------------------------------

def get_fs_metadata(path: str, config: dict = None) -> dict:
    """Dateisystem-Metadaten einer Datei."""
    if config is None:
        config = {}
    result = {
        "size_bytes": None,
        "created_at": None,
        "modified_at": None,
        "file_owner": None,
    }
    try:
        stat = os.stat(path)
        result["size_bytes"] = stat.st_size
        result["created_at"]  = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()
        result["modified_at"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        pass

    if HAS_WIN32 and config.get("read_file_owner", True):
        try:
            sd = win32security.GetFileSecurity(
                path, win32security.OWNER_SECURITY_INFORMATION
            )
            owner_sid = sd.GetSecurityDescriptorOwner()
            name, domain, _ = win32security.LookupAccountSid(None, owner_sid)
            result["file_owner"] = name
        except BaseException:
            # GetFileSecurity blockiert auf Netzwerkpfaden und kann von Windows
            # mit einem Control-Signal abgebrochen werden, das Python als
            # KeyboardInterrupt darstellt. Besitzer dann einfach leer lassen.
            pass

    return result


# ---------------------------------------------------------------------------
# OOXML-Analyse (Excel, Word, PowerPoint)
# ---------------------------------------------------------------------------

OOXML_NS = {
    "dc":       "http://purl.org/dc/elements/1.1/",
    "cp":       "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dcterms":  "http://purl.org/dc/terms/",
}

def analyze_ooxml(path: str, ext: str) -> dict:
    """Analysiert OOXML-Dateien (xlsx, xlsm, docm, pptm …) via ZIP-Inspektion."""
    result = {
        "office_author":          None,
        "office_last_author":     None,
        "office_created":         None,
        "office_modified":        None,
        "has_macros":             0,
        "has_external_links":     0,
        "sheet_count":            None,
        "named_ranges_count":     None,
        "formula_count":          0,
        "has_sheet_protection":   0,
        "protected_sheets_count": 0,
        "sheet_protection_has_pw":0,
        "workbook_protected":     0,
    }

    try:
        if not zipfile.is_zipfile(path):
            return result

        with zipfile.ZipFile(path, "r") as z:
            names = z.namelist()

            # --- Core Properties (Autor, Datum) ---
            if "docProps/core.xml" in names:
                try:
                    with z.open("docProps/core.xml") as f:
                        tree = ET.parse(f)
                        root = tree.getroot()
                        def _find(tag, ns_key):
                            ns = OOXML_NS[ns_key]
                            el = root.find(f".//{{{ns}}}{tag}")
                            return el.text.strip() if el is not None and el.text else None

                        result["office_author"]      = _find("creator",          "dc")
                        result["office_last_author"] = _find("lastModifiedBy",   "cp")
                        result["office_created"]     = _find("created",          "dcterms")
                        result["office_modified"]    = _find("modified",         "dcterms")
                except Exception:
                    pass

            # --- Makros (VBA-Projekt) ---
            vba_paths = [n for n in names if "vbaProject" in n]
            result["has_macros"] = 1 if vba_paths else 0

            # --- Externe Verknüpfungen (Excel) ---
            ext_links = [n for n in names if "externalLink" in n.lower()]
            result["has_external_links"] = 1 if ext_links else 0

            # --- Tabellenblätter zählen (Excel) ---
            if ext.lower() in (".xlsx", ".xlsm", ".xlsb", ".xls", ".xltm", ".xltx"):
                sheets = [n for n in names if n.startswith("xl/worksheets/sheet")]
                result["sheet_count"] = len(sheets)

                # --- Benannte Bereiche + Arbeitsmappenschutz (workbook.xml) ---
                if "xl/workbook.xml" in names:
                    try:
                        with z.open("xl/workbook.xml") as f:
                            wb_tree = ET.parse(f)
                            wb_root = wb_tree.getroot()
                            ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                            defined = wb_root.findall(f".//{{{ns}}}definedName")
                            result["named_ranges_count"] = len(defined)
                            # Arbeitsmappenschutz: <workbookProtection> vorhanden?
                            wb_prot = wb_root.find(f"{{{ns}}}workbookProtection")
                            if wb_prot is not None:
                                # lockStructure="1" oder lockWindows="1" → aktiv
                                lock_structure = wb_prot.get("lockStructure", "0")
                                lock_windows   = wb_prot.get("lockWindows",   "0")
                                if lock_structure == "1" or lock_windows == "1":
                                    result["workbook_protected"] = 1
                    except Exception:
                        pass

                # --- Blattschutz + Formelzählung: jedes Sheet-XML prüfen ---
                sheet_files = [n for n in names
                               if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
                protected     = 0
                has_pw        = 0
                formula_count = 0
                ns_ss = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                for sheet_file in sheet_files:
                    try:
                        with z.open(sheet_file) as sf:
                            sh_tree = ET.parse(sf)
                            sh_root = sh_tree.getroot()
                            # Blattschutz: <sheetProtection> vorhanden UND
                            # entweder sheet="1" explizit gesetzt oder Passwort-Hash hinterlegt
                            prot_el = sh_root.find(f"{{{ns_ss}}}sheetProtection")
                            if prot_el is not None:
                                has_hash = prot_el.get("hashValue") or prot_el.get("password")
                                sheet_on = prot_el.get("sheet", "0") == "1"
                                if sheet_on or has_hash:
                                    protected += 1
                                    if has_hash:
                                        has_pw = 1
                            # Formeln: jede Zelle mit einem <f>-Kindelement ist eine Formelzelle
                            formula_count += len(sh_root.findall(f".//{{{ns_ss}}}f"))
                    except Exception:
                        pass

                result["formula_count"] = formula_count
                if protected > 0:
                    result["has_sheet_protection"]   = 1
                    result["protected_sheets_count"]  = protected
                    result["sheet_protection_has_pw"] = has_pw

    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Cognos IDA-Report-Analyse (*.ida)
# ---------------------------------------------------------------------------

import re as _re

_COGNOS_NS_RE = _re.compile(
    r'\{http://developer\.cognos\.com/schemas/report/(\d+\.\d+)/\}'
)


def analyze_cognos_xml(path: str) -> dict:
    """Parst eine Cognos Report-Spezifikation (*.ida).

    Gibt immer ein dict zurück. 'ist_cognos_report' == 0, wenn die Datei
    kein gültiger Cognos-Report ist oder das Parsen fehlschlug.
    """
    result = {
        "ist_cognos_report":          0,
        "cognos_report_name":         None,
        "cognos_paket_pfad":          None,
        "cognos_abfragen_anzahl":     None,
        "cognos_datenpunkte_anzahl":  None,
        "cognos_filter_anzahl":       None,
        "cognos_seiten_anzahl":       None,
        "cognos_parameter_anzahl":    None,
        "cognos_namespace_version":   None,
    }
    try:
        tree = ET.parse(path)
        root = tree.getroot()

        m = _COGNOS_NS_RE.match(root.tag)
        if not m:
            return result   # kein Cognos-Namespace → kein Cognos-Report

        ns  = m.group(0)[1:-1]   # URI ohne geschweifte Klammern
        pfx = f"{{{ns}}}"

        def find_all(tag):
            return root.findall(f".//{pfx}{tag}")

        result["ist_cognos_report"]         = 1
        result["cognos_namespace_version"]  = m.group(1)
        result["cognos_report_name"]        = root.get("name")

        mp = root.find(f"{pfx}modelPath") or root.find(f".//{pfx}modelPath")
        result["cognos_paket_pfad"] = (
            mp.text.strip() if mp is not None and mp.text else None
        )

        result["cognos_abfragen_anzahl"]    = len(find_all("query"))
        result["cognos_datenpunkte_anzahl"] = len(find_all("dataItem"))
        result["cognos_filter_anzahl"]      = (
            len(find_all("detailFilter")) + len(find_all("summaryFilter"))
        )
        result["cognos_seiten_anzahl"]      = len(find_all("page"))
        result["cognos_parameter_anzahl"]   = len(find_all("parameter"))

    except Exception:
        pass  # Kein gültiger Cognos-Report – result['ist_cognos_report'] bleibt 0
    return result


# ---------------------------------------------------------------------------
# Pfad-Hilfsfunktionen
# ---------------------------------------------------------------------------

def should_exclude(path: str, excludes: list) -> bool:
    path_lower = path.lower()
    return any(ex.lower() in path_lower for ex in excludes)


def get_share_root(path: str, scan_paths: list) -> tuple:
    """Gibt (share_root, relative_path) zurück."""
    p = Path(path)
    for sp in scan_paths:
        try:
            rel = p.relative_to(sp)
            return sp, str(rel)
        except ValueError:
            continue
    return str(p.anchor), str(p)


# ---------------------------------------------------------------------------
# Pause / Abbrechen / Checkpoint
# ---------------------------------------------------------------------------

class ScanCancelledError(Exception):
    """Wird ausgelöst, wenn der Benutzer den Scan abbricht."""


def check_signals(signal_dir: Optional[str]) -> str:
    """Gibt 'cancel', 'pause' oder 'ok' zurück."""
    if not signal_dir:
        return "ok"
    if os.path.exists(os.path.join(signal_dir, "scanner_cancel.signal")):
        return "cancel"
    if os.path.exists(os.path.join(signal_dir, "scanner_pause.signal")):
        return "pause"
    return "ok"


def write_checkpoint(signal_dir: str, scan_run_id: int, scan_paths: list,
                     completed_dirs: list, stats: dict) -> None:
    """Schreibt den Fortschrittsstand in eine JSON-Datei."""
    data = {
        "scan_run_id":     scan_run_id,
        "scan_paths":      scan_paths,
        "completed_dirs":  completed_dirs,
        "stats":           stats,
        "checkpointed_at": datetime.now(timezone.utc).isoformat(),
    }
    path = os.path.join(signal_dir, "scanner_checkpoint.json")
    tmp  = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def read_checkpoint(signal_dir: str) -> Optional[dict]:
    """Liest den letzten Checkpoint. Gibt None zurück, wenn keiner vorhanden."""
    path = os.path.join(signal_dir, "scanner_checkpoint.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def remove_checkpoint(signal_dir: str) -> None:
    """Löscht die Checkpoint-Datei (nach erfolgreichem Scan)."""
    try:
        os.remove(os.path.join(signal_dir, "scanner_checkpoint.json"))
    except FileNotFoundError:
        pass


def clean_signals(signal_dir: str) -> None:
    """Löscht Pause- und Abbruch-Signaldateien."""
    for name in ("scanner_pause.signal", "scanner_cancel.signal"):
        try:
            os.remove(os.path.join(signal_dir, name))
        except FileNotFoundError:
            pass


def _check_and_handle_signals(signal_dir: Optional[str], logger: logging.Logger) -> None:
    """Blockiert bei Pause, löst ScanCancelledError bei Abbruch aus."""
    sig = check_signals(signal_dir)
    if sig == "cancel":
        logger.info("Abbruch-Signal empfangen.")
        raise ScanCancelledError()
    elif sig == "pause":
        logger.info("Pause-Signal empfangen – Scan unterbrochen. Warte auf Fortsetzung …")
        while True:
            time.sleep(2)
            sig = check_signals(signal_dir)
            if sig == "cancel":
                logger.info("Abbruch-Signal während Pause empfangen.")
                raise ScanCancelledError()
            if sig != "pause":
                logger.info("Pause aufgehoben – Scan wird fortgesetzt.")
                break


# ---------------------------------------------------------------------------
# Scanner-Kern
# ---------------------------------------------------------------------------

def scan_file(path: str, config: dict, scan_paths: list,
              logger: logging.Logger = None) -> Optional[dict]:
    """Analysiert eine einzelne Datei und gibt ein Metadaten-Dict zurück."""
    ext = Path(path).suffix.lower()
    fs  = get_fs_metadata(path, config)

    # Hash nur bei vertretbarer Dateigröße
    size_limit = config.get("hash_size_limit_mb", 500) * 1024 * 1024
    file_hash  = None
    if fs["size_bytes"] is not None and fs["size_bytes"] <= size_limit:
        file_hash = sha256_file(path, logger=logger)

    # OOXML-Analyse für Office-Dateien
    ooxml_exts = {".xlsx", ".xlsm", ".xlsb", ".xltm", ".xltx",
                  ".docm", ".dotm", ".pptm", ".pptx", ".docx"}
    ooxml = {}
    if ext in ooxml_exts:
        ooxml = analyze_ooxml(path, ext)

    # Cognos IDA-Report-Analyse
    cognos = {}
    if ext == ".ida":
        cognos = analyze_cognos_xml(path)

    share_root, rel_path = get_share_root(path, scan_paths)

    mappings = config.get("path_mappings", [])
    stored_full_path = apply_path_mappings(path, mappings)
    stored_share_root = apply_path_mappings(share_root, mappings)

    return {
        "file_hash":          file_hash or "HASH_ERROR",
        "full_path":          stored_full_path,
        "file_name":          Path(path).name,
        "extension":          ext,
        "share_root":         stored_share_root,
        "relative_path":      rel_path,
        "size_bytes":         fs.get("size_bytes"),
        "created_at":         fs.get("created_at"),
        "modified_at":        fs.get("modified_at"),
        "file_owner":         fs.get("file_owner"),
        "office_author":      ooxml.get("office_author"),
        "office_last_author": ooxml.get("office_last_author"),
        "office_created":     ooxml.get("office_created"),
        "office_modified":    ooxml.get("office_modified"),
        "has_macros":             ooxml.get("has_macros", 0),
        "has_external_links":     ooxml.get("has_external_links", 0),
        "sheet_count":            ooxml.get("sheet_count"),
        "named_ranges_count":     ooxml.get("named_ranges_count"),
        "formula_count":          ooxml.get("formula_count", 0),
        "has_sheet_protection":   ooxml.get("has_sheet_protection", 0),
        "protected_sheets_count": ooxml.get("protected_sheets_count", 0),
        "sheet_protection_has_pw":ooxml.get("sheet_protection_has_pw", 0),
        "workbook_protected":     ooxml.get("workbook_protected", 0),
        # Cognos IDA-Report-Felder
        "ist_cognos_report":          cognos.get("ist_cognos_report", 0),
        "cognos_report_name":         cognos.get("cognos_report_name"),
        "cognos_paket_pfad":          cognos.get("cognos_paket_pfad"),
        "cognos_abfragen_anzahl":     cognos.get("cognos_abfragen_anzahl"),
        "cognos_datenpunkte_anzahl":  cognos.get("cognos_datenpunkte_anzahl"),
        "cognos_filter_anzahl":       cognos.get("cognos_filter_anzahl"),
        "cognos_seiten_anzahl":       cognos.get("cognos_seiten_anzahl"),
        "cognos_parameter_anzahl":    cognos.get("cognos_parameter_anzahl"),
        "cognos_namespace_version":   cognos.get("cognos_namespace_version"),
    }


def _to_extended_path(path: str) -> str:
    r"""Windows: wandelt UNC/lokale Pfade in Extended-Length-Form um (max. 32767 Zeichen).

    \\server\share\...  →  \\?\UNC\server\share\...
    C:\...              →  \\?\C:\...
    Hebt das MAX_PATH-Limit (260 Zeichen) für os.scandir/os.stat auf.
    """
    if os.name != 'nt' or path.startswith('\\\\?\\'):
        return path
    if path.startswith('\\\\'):
        return '\\\\?\\UNC\\' + path[2:]
    return '\\\\?\\' + path


def safe_walk(top: str, followlinks: bool = False, logger: logging.Logger = None):
    """os.walk-Ersatz mit robuster Fehlerbehandlung für Netzlaufwerke.

    Fängt PermissionError, OSError und Windows-Control-Signale (die Python als
    KeyboardInterrupt darstellt) beim Verzeichnis-Listing ab. Unzugängliche
    Verzeichnisse werden übersprungen und als Warnung geloggt.

    Wie os.walk unterstützt diese Funktion das in-place Filtern von dirs durch
    den Aufrufer, um den Abstieg in bestimmte Unterverzeichnisse zu verhindern.
    """
    try:
        with os.scandir(top) as it:
            entries = list(it)
    except PermissionError as e:
        if logger:
            logger.warning(f"Kein Zugriff auf Verzeichnis (übersprungen): {top} – {e.strerror}")
        return
    except OSError as e:
        # WinError 3 (Pfad nicht gefunden) und WinError 206 (Dateiname zu lang)
        # treten auf, wenn der Pfad das MAX_PATH-Limit (260 Zeichen) überschreitet.
        # Retry mit Extended-Length-Präfix hebt dieses Limit auf.
        if os.name == 'nt' and getattr(e, 'winerror', None) in (3, 206):
            try:
                with os.scandir(_to_extended_path(top)) as it:
                    entries = list(it)
            except OSError as e2:
                if logger:
                    logger.warning(f"Lesefehler Verzeichnis (übersprungen): {top} – {e2.strerror}")
                return
        else:
            if logger:
                logger.warning(f"Lesefehler Verzeichnis (übersprungen): {top} – {e.strerror}")
            return
    except BaseException as e:
        # Auf Netzlaufwerken kann Windows ein Control-Signal senden,
        # das Python als KeyboardInterrupt darstellt – analog zum bekannten
        # Verhalten von GetFileSecurity auf geschützten Freigaben.
        if logger:
            logger.warning(
                f"Verzeichnis-Listing unterbrochen (übersprungen): {top} – {type(e).__name__}"
            )
        return

    dirs = []
    nondirs = []
    for entry in entries:
        try:
            is_dir = entry.is_dir(follow_symlinks=followlinks)
        except OSError:
            is_dir = False
        if is_dir:
            dirs.append(entry.name)
        else:
            nondirs.append(entry.name)

    yield top, dirs, nondirs

    # dirs kann vom Aufrufer in-place gefiltert worden sein (wie bei os.walk)
    for dirname in dirs:
        yield from safe_walk(os.path.join(top, dirname), followlinks=followlinks, logger=logger)


def walk_and_scan(scan_path: str, config: dict, all_scan_paths: list,
                  logger: logging.Logger, scan_since_ts: Optional[float] = None):
    """Generator: liefert Metadaten-Dicts für alle gefundenen Dateien.

    scan_since_ts: Unix-Timestamp (float). Dateien, die vor diesem Zeitpunkt
    zuletzt geändert wurden, werden übersprungen.
    """
    extensions = set(e.lower() for e in config["extensions"])
    blacklist  = config.get("blacklist_paths", [])
    whitelist  = config.get("whitelist_paths", [])

    for root, dirs, files in safe_walk(scan_path, followlinks=False, logger=logger):
        dirs[:] = [
            d for d in dirs
            if should_pass_filters(os.path.join(root, d), blacklist, whitelist)
        ]

        for fname in files:
            ext = Path(fname).suffix.lower()
            if ext not in extensions:
                continue

            full_path = os.path.join(root, fname)
            if not should_pass_filters(full_path, blacklist, whitelist):
                continue

            # Startdatum-Filter: Dateien vor scan_since überspringen
            if scan_since_ts is not None:
                try:
                    if os.stat(full_path).st_mtime < scan_since_ts:
                        continue
                except OSError:
                    pass  # bei Lesefehler: Datei trotzdem verarbeiten

            try:
                data = scan_file(full_path, config, all_scan_paths, logger=logger)
                if data:
                    yield data
            except Exception as e:
                logger.warning(f"Fehler bei {full_path}: {type(e).__name__}: {e}")


def walk_root_files(scan_path: str, config: dict, all_scan_paths: list,
                    logger: logging.Logger, scan_since_ts: Optional[float] = None):
    """Generator: liefert Metadaten-Dicts für Dateien direkt im scan_path (keine Rekursion)."""
    extensions = set(e.lower() for e in config["extensions"])
    blacklist  = config.get("blacklist_paths", [])
    whitelist  = config.get("whitelist_paths", [])
    try:
        entries = os.listdir(scan_path)
    except OSError as e:
        if os.name == 'nt' and getattr(e, 'winerror', None) in (3, 206):
            try:
                entries = os.listdir(_to_extended_path(scan_path))
            except OSError as e2:
                logger.warning(f"Kann Verzeichnis nicht öffnen: {scan_path}: {e2}")
                return
        else:
            logger.warning(f"Kann Verzeichnis nicht öffnen: {scan_path}: {e}")
            return
    for fname in entries:
        full_path = os.path.join(scan_path, fname)
        if not os.path.isfile(full_path):
            continue
        ext = Path(fname).suffix.lower()
        if ext not in extensions:
            continue
        if not should_pass_filters(full_path, blacklist, whitelist):
            continue
        if scan_since_ts is not None:
            try:
                if os.stat(full_path).st_mtime < scan_since_ts:
                    continue
            except OSError:
                pass
        try:
            data = scan_file(full_path, config, all_scan_paths, logger=logger)
            if data:
                yield data
        except Exception as e:
            logger.warning(f"Fehler bei {full_path}: {type(e).__name__}: {e}")


def _get_toplevel_dirs(scan_path: str, blacklist: list, whitelist: list) -> list:
    """Gibt eine sortierte Liste der direkten Unterverzeichnisse zurück."""
    try:
        return sorted([
            os.path.join(scan_path, d)
            for d in os.listdir(scan_path)
            if os.path.isdir(os.path.join(scan_path, d))
            and should_pass_filters(os.path.join(scan_path, d), blacklist, whitelist)
        ])
    except OSError:
        return []


_EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xlsb", ".xls", ".xltm", ".xltx"}


def _classify_by_filename(file_name: str) -> Optional[str]:
    """Gibt bearbeitungsstatus anhand von Dateinamen-Präfix/-Suffix zurück oder None.

    Dateiname enthält 'IDV' (Präfix/Suffix) → 'Zur Registrierung' (wesentliche Eigenentwicklung)
    Dateiname enthält 'AH'  (Präfix/Suffix) → 'Nicht wesentlich'  (unwesentliche Eigenentwicklung/Arbeitshilfe)
    IDV hat Vorrang gegenüber AH.
    """
    stem = Path(file_name).stem.upper()
    if stem.startswith("IDV") or stem.endswith("IDV"):
        return "Zur Registrierung"
    if stem.startswith("AH") or stem.endswith("AH"):
        return "Nicht wesentlich"
    return None


def _process_chunk(chunk_gen, conn: sqlite3.Connection, scan_run_id: int,
                   now: str, logger: logging.Logger, move_mode: str,
                   stats: dict, signal_dir: Optional[str],
                   auto_ignore: bool = False,
                   discard_no_formula: bool = False,
                   auto_classify_filename: bool = True,
                   progress_lock: Optional[threading.Lock] = None) -> None:
    """Verarbeitet alle Dateien eines Generators, prüft alle 10 Dateien die Signale.

    auto_ignore:            Neue Excel-Dateien ohne Formeln/Makros sofort als 'Ignoriert' markieren.
    discard_no_formula:     Neue Excel-Dateien ohne Formeln/Makros komplett überspringen (nicht in DB).
    auto_classify_filename: Neue Dateien mit Präfix/Suffix 'AH' oder 'IDV' automatisch klassifizieren.
    progress_lock:          Optionaler Lock, der ``stats``-Mutationen serialisiert, wenn
                            mehrere Worker-Threads gleichzeitig Chunks verarbeiten.
    """
    # Signal-Check zu Beginn jedes Chunks (wichtig bei Verzeichnissen mit < 10 Dateien)
    _check_and_handle_signals(signal_dir, logger)
    file_counter = 0

    # No-op-Kontext, wenn keine Parallelisierung aktiv ist – spart Lock-Overhead.
    class _NullLock:
        def __enter__(self):  # pragma: no cover — trivial
            return self
        def __exit__(self, *a):  # pragma: no cover — trivial
            return False
    lock = progress_lock if progress_lock is not None else _NullLock()

    for data in chunk_gen:
        current_path = data.get("full_path", "?")
        try:
            logger.debug(f"Verarbeite: {current_path}")

            is_excel = data.get("extension", "").lower() in _EXCEL_EXTENSIONS
            no_formula = not data.get("formula_count") and not data.get("has_macros")

            # ── Discard: neue Excel-Dateien ohne Formeln komplett überspringen ──
            if discard_no_formula and is_excel and no_formula:
                exists = conn.execute(
                    "SELECT 1 FROM idv_files WHERE full_path = ?", (data["full_path"],)
                ).fetchone()
                if not exists:
                    with lock:
                        stats["discarded"] = stats.get("discarded", 0) + 1
                    logger.debug(f"Verworfen (kein Formel): {current_path}")
                    continue

            change = upsert_file(conn, data, scan_run_id, now, logger, move_mode)
            with lock:
                stats["total"]  += 1
                stats[change]   += 1
                total_snapshot  = stats["total"]
            file_counter    += 1

            # ── Auto-Ignore: neue Excel-Dateien ohne Formeln sofort ignorieren ──
            if auto_ignore and change in ("new", "restored") and is_excel and no_formula:
                emit(
                    OP_UPDATE_STATUS,
                    kind="auto_ignore_single",
                    full_path=data["full_path"],
                )

            # ── Auto-Klassifizierung nach Dateiname (AH / IDV) ──
            if auto_classify_filename and change in ("new", "restored"):
                fn_status = _classify_by_filename(data.get("file_name", ""))
                if fn_status:
                    emit(
                        OP_UPDATE_STATUS,
                        kind="auto_classify_single",
                        full_path=data["full_path"],
                        new_status=fn_status,
                    )

            if file_counter % 10 == 0:
                _check_and_handle_signals(signal_dir, logger)
                _flush_log(logger)

            if total_snapshot % 20 == 0:
                logger.info(f"  … {total_snapshot} Dateien verarbeitet")
        except ScanCancelledError:
            raise
        except BaseException as e:
            # BaseException fängt auch KeyboardInterrupt ab, den Windows bei
            # Netzwerk-Problemen als Control-Signal senden kann.
            logger.error(
                f"Fehler bei {current_path}: {type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}"
            )
            _flush_log(logger)
            with lock:
                stats["errors"] += 1
            # Bei echtem KeyboardInterrupt (Ctrl+C vom Benutzer) abbrechen;
            # bei Netzwerk-Signalen weiter scannen.
            if isinstance(e, KeyboardInterrupt):
                # Prüfen ob ein Abbruch-Signal vorliegt (bewusster Abbruch)
                sig = check_signals(signal_dir) if signal_dir else "ok"
                if sig == "cancel":
                    raise ScanCancelledError()
                # Kein Signal → vermutlich Windows-Netzwerk-Signal, weiter scannen
                logger.warning("KeyboardInterrupt ohne Abbruch-Signal – setze Scan fort")


# ---------------------------------------------------------------------------
# Datenbank-Upsert & Delta-Erkennung
# ---------------------------------------------------------------------------

def upsert_file(conn: sqlite3.Connection, data: dict,
                scan_run_id: int, now: str, logger: logging.Logger,
                move_mode: str = "name_and_hash") -> str:
    """
    Fügt eine Datei ein oder aktualisiert sie.
    Gibt change_type zurück: 'new' | 'changed' | 'unchanged' | 'moved' | 'restored'

    Logik:
    1. Eintrag für full_path vorhanden (aktiv oder archiviert)?
       → Update; war archiviert → 'restored', sonst 'changed'/'unchanged'
    2. Kein Eintrag für full_path → Move-Detection gemäß move_mode:
       "name_and_hash": gleicher Hash + gleicher Dateiname → moved
       "hash_only":     gleicher Hash, genau ein aktiver Treffer → moved
                        (mehrere Treffer = Mehrdeutigkeit → new)
       "disabled":      keine Move-Detection
    3. Sonst: echter Neuzugang → 'new'
    """
    existing = conn.execute(
        "SELECT id, file_hash, status FROM idv_files WHERE full_path = ?",
        (data["full_path"],)
    ).fetchone()

    if existing is None:
        # ── Move-Detection ──────────────────────────────────────────────
        if data["file_hash"] != "HASH_ERROR" and move_mode != "disabled":
            moved_from = None

            # Stufe 1: gleicher Hash + gleicher Dateiname (immer aktiv)
            moved_from = conn.execute(
                "SELECT id, full_path, file_name FROM idv_files "
                "WHERE file_hash = ? AND file_name = ? AND status = 'active'",
                (data["file_hash"], data["file_name"])
            ).fetchone()

            # Stufe 2 (hash_only): gleicher Hash, Dateiname egal, nur bei Eindeutigkeit
            if not moved_from and move_mode == "hash_only":
                candidates = conn.execute(
                    "SELECT id, full_path, file_name FROM idv_files "
                    "WHERE file_hash = ? AND status = 'active'",
                    (data["file_hash"],)
                ).fetchall()
                if len(candidates) == 1:
                    moved_from = candidates[0]
                    logger.debug(
                        f"Move (hash_only): '{moved_from['file_name']}' → "
                        f"'{data['file_name']}' | {moved_from['full_path']} → {data['full_path']}"
                    )
                elif len(candidates) > 1:
                    logger.debug(
                        f"Move-Detection: {len(candidates)} Treffer für Hash "
                        f"{data['file_hash'][:12]}… – zu mehrdeutig, behandle als neu"
                    )

            if moved_from:
                # Prüfen ob die Quelldatei in diesem Lauf bereits gesehen wurde
                # (last_scan_run_id == scan_run_id → Originaldatei existiert noch).
                # In diesem Fall handelt es sich um eine KOPIE, nicht um eine
                # Verschiebung → als Neuanlage behandeln.
                source_still_active = conn.execute(
                    "SELECT 1 FROM idv_files WHERE id = ? AND last_scan_run_id = ?",
                    (moved_from["id"], scan_run_id)
                ).fetchone()
                if source_still_active:
                    logger.debug(
                        f"Kopie erkannt (Quelle noch aktiv): '{moved_from['full_path']}' "
                        f"→ '{data['full_path']}' – behandle als Neuanlage"
                    )
                    moved_from = None   # Kopie → Neuanlage weiter unten

            if moved_from:
                emit(
                    OP_UPSERT_FILE,
                    action="move",
                    scan_run_id=scan_run_id,
                    now=now,
                    change_type="moved",
                    file_id=moved_from["id"],
                    old_hash=data["file_hash"],
                    data=data,
                    details=json.dumps({
                        "old_path": moved_from["full_path"],
                        "new_path": data["full_path"],
                    }),
                )
                logger.debug(f"Verschoben: {moved_from['full_path']} → {data['full_path']}")
                return "moved"

        # ── Echter Neuzugang ────────────────────────────────────────────
        emit(
            OP_UPSERT_FILE,
            action="insert",
            scan_run_id=scan_run_id,
            now=now,
            change_type="new",
            data=data,
        )
        return "new"

    else:
        # ── Bekannte Datei: Update ──────────────────────────────────────
        file_id      = existing["id"]
        old_hash     = existing["file_hash"]
        new_hash     = data["file_hash"]
        was_archived = existing["status"] == "archiviert"

        if was_archived:
            change_type = "restored"
        else:
            change_type = "changed" if old_hash != new_hash else "unchanged"

        emit(
            OP_UPSERT_FILE,
            action="update",
            scan_run_id=scan_run_id,
            now=now,
            change_type=change_type,
            file_id=file_id,
            old_hash=old_hash,
            data=data,
        )
        return change_type


def mark_deleted_files(conn: sqlite3.Connection, scan_run_id: int, now: str,
                       scan_since: Optional[str] = None,
                       scan_paths: Optional[list] = None) -> int:
    """Überführt aktive Dateien, die im aktuellen Scan nicht gesehen wurden, ins Archiv.

    Die Auswahl erfolgt nicht mehr hier, sondern im Webapp-Writer (Event
    ``OP_ARCHIVE_UNSEEN``). Hintergrund: Der Scanner nutzt eine eigene
    Reader-Connection. Upsert-Events werden asynchron an den Webapp-Writer
    gereicht, der sie in Batches anwendet. Zum Zeitpunkt, zu dem der Scanner
    hier fragen würde, „welche Dateien wurden in diesem Lauf nicht gesehen?",
    sind die letzten Upserts häufig noch nicht committet – die Scanner-
    Connection sähe dann einen veralteten ``last_scan_run_id`` und würde
    gerade aktualisierte Dateien fälschlicherweise archivieren. Mit dem
    Event-basierten Ansatz läuft das SELECT auf der Writer-Connection und
    berücksichtigt alle zuvor in der Queue stehenden Upserts.

    scan_since:  ISO-Datumsstring (z.B. '2024-07-01'). Wenn gesetzt, werden nur
                 Dateien archiviert, deren modified_at >= scan_since liegt.

    scan_paths:  Liste der in diesem Lauf tatsächlich gescannten Pfade. Wenn gesetzt,
                 werden nur Dateien archiviert, deren full_path unter einem dieser
                 Pfade liegt. Dateien außerhalb des Geltungsbereichs bleiben unberührt
                 — so können mehrere Teilscans auf verschiedene Verzeichnisse korrekt
                 akkumuliert werden.

    Rueckgabe: immer 0. Die tatsaechliche Anzahl ermittelt der Webapp-Handler
    und schreibt sie in ``scan_runs.archived_files``; ``apply_scan_run_end``
    merged den Wert per ``MAX``, damit OP_END_RUN ihn nicht ueberschreibt.
    """
    emit(
        OP_ARCHIVE_UNSEEN,
        scan_run_id=scan_run_id,
        now=now,
        scan_since=scan_since,
        scan_paths=list(scan_paths) if scan_paths else [],
    )
    return 0


# ---------------------------------------------------------------------------
# Identitäts- und Pfad-Diagnostik
# ---------------------------------------------------------------------------

def _log_scanner_identity(logger: logging.Logger) -> None:
    """Loggt, unter welcher Identität der Scanner läuft.

    Hintergrund: Der Scanner läuft im Kontext des idvault-Dienstes; die
    UNC-Credentials eines konfigurierten AD-Benutzers werden vor dem
    Start via WNetAddConnection2 in der Session registriert. Diese Log-
    Zeile hilft bei der Fehlersuche, wenn UNC-Zugriffe scheitern
    (z. B. weil der Dienst unerwartet als LOCAL SERVICE statt
    LOCAL SYSTEM läuft oder eine Credential-Registrierung fehlt).
    """
    try:
        user = getpass.getuser()
    except Exception:
        user = "?"

    domain_user = user
    session_id = None
    if HAS_WIN32:
        try:
            # Voll qualifizierter Name (DOMAIN\user) bzw. UPN
            # NameSamCompatible = 2
            domain_user = win32api.GetUserNameEx(2)
        except Exception:
            try:
                domain_user = win32api.GetUserName()
            except Exception:
                pass
        try:
            # Session 0 → Service-Session ohne Desktop;
            # Session > 0 → interaktive Anmeldung.
            session_id = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
        except Exception:
            session_id = None

    msg = f"Scanner-Identität: {domain_user}"
    if session_id is not None:
        msg += f" (Konsolen-Session-ID: {session_id})"
    logger.info(msg)


def _check_path_accessible(path: str, logger: logging.Logger,
                           retries: int = 2,
                           retry_delay: float = 2.0) -> Tuple[bool, str]:
    """Prüft, ob ein (Netzwerk-)Pfad erreichbar und lesbar ist.

    Im Gegensatz zu ``os.path.exists()`` (das jede Art von Fehler still
    in ``False`` umwandelt) versucht diese Funktion tatsächlich, das
    Verzeichnis zu öffnen, und liefert die zugrundeliegende Fehlermeldung
    samt Windows-Fehlercode zurück. Bei transienten Netzwerkfehlern wird
    bis zu ``retries`` Mal mit Wartezeit erneut versucht.

    Gibt ``(True, "")`` bei Erfolg zurück oder ``(False, error_msg)``.
    """
    last_err = ""
    for attempt in range(retries + 1):
        try:
            with os.scandir(path) as it:
                # Mindestens einen Eintrag anfordern, um sicherzustellen,
                # dass die SMB-Verbindung tatsächlich aufgebaut wird.
                next(iter(it), None)
            return True, ""
        except FileNotFoundError as e:
            last_err = f"FileNotFoundError [WinError {getattr(e, 'winerror', '?')}]: {e.strerror or e}"
        except PermissionError as e:
            last_err = f"PermissionError [WinError {getattr(e, 'winerror', '?')}]: {e.strerror or e}"
            # Bei Permission Denied bringt ein Retry nichts.
            break
        except OSError as e:
            last_err = (
                f"{type(e).__name__} [errno {e.errno} / WinError "
                f"{getattr(e, 'winerror', '?')}]: {e.strerror or e}"
            )
        except BaseException as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < retries:
            logger.info(
                f"  Pfad-Prüfung fehlgeschlagen (Versuch {attempt + 1}/{retries + 1}) "
                f"für {path}: {last_err} – erneuter Versuch in {retry_delay}s …"
            )
            time.sleep(retry_delay)

    return False, last_err


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def run_scan(config: dict, logger: logging.Logger,
             signal_dir: Optional[str] = None, resume: bool = False):
    """Führt den Scan durch. Unterstützt Pause/Abbrechen/Checkpoint/Resume.

    signal_dir: Verzeichnis für Signaldateien (scanner_pause.signal etc.).
                Üblicherweise das Verzeichnis der config.json.
    resume:     Wenn True und ein Checkpoint existiert, wird dort fortgesetzt.
    """
    scan_paths = config["scan_paths"]
    if not scan_paths:
        logger.error("Keine Scan-Pfade konfiguriert. Bitte config.json anpassen.")
        sys.exit(1)

    path_mappings = config.get("path_mappings", [])
    # Gemappte Scan-Pfade für mark_deleted_files (DB enthält gemappte Pfade)
    mapped_scan_paths = [apply_path_mappings(sp, path_mappings) for sp in scan_paths]

    conn = init_db(config["db_path"])
    now  = datetime.now(timezone.utc).isoformat()

    # Identität des Scanner-Prozesses loggen – wichtig für die Fehlersuche bei
    # Zugriffsproblemen auf Netzwerkfreigaben (z. B. wenn der Scanner aus einem
    # Windows-Dienst heraus als technischer AD-Benutzer gestartet wird).
    _log_scanner_identity(logger)

    move_mode = config.get("move_detection", "hash_only")
    logger.info(f"Move-Detection-Modus: {move_mode}")

    # ── Laufzeit-Einstellungen aus DB laden ───────────────────────────────────
    try:
        _ai_row = conn.execute(
            "SELECT value FROM app_settings WHERE key='auto_ignore_no_formula'"
        ).fetchone()
        runtime_auto_ignore = (_ai_row and _ai_row["value"] == "1")
    except Exception:
        runtime_auto_ignore = False

    try:
        _dc_row = conn.execute(
            "SELECT value FROM app_settings WHERE key='discard_no_formula'"
        ).fetchone()
        runtime_discard = (_dc_row and _dc_row["value"] == "1")
    except Exception:
        runtime_discard = False

    try:
        _cf_row = conn.execute(
            "SELECT value FROM app_settings WHERE key='auto_classify_by_filename'"
        ).fetchone()
        # Default: deaktiviert (kein Eintrag = "0")
        runtime_classify_filename = (_cf_row["value"] == "1") if _cf_row else False
    except Exception:
        runtime_classify_filename = False

    if runtime_auto_ignore:
        logger.info("Laufzeit-Auto-Ignore aktiv: neue Excel-Dateien ohne Formeln werden sofort ignoriert")
    if runtime_discard:
        logger.info("Verwerfen aktiv: neue Excel-Dateien ohne Formeln werden nicht in die DB aufgenommen")
    if runtime_classify_filename:
        logger.info("Auto-Klassifizierung nach Dateiname aktiv: AH (Arbeitshilfe) → 'Nicht wesentlich', IDV → 'Zur Registrierung'")

    # Startdatum-Filter auswerten
    scan_since    = config.get("scan_since") or None
    scan_since_ts = None
    if scan_since:
        try:
            dt = datetime.fromisoformat(scan_since)
            scan_since_ts = dt.timestamp()
            logger.info(f"Startdatum-Filter aktiv: nur Dateien >= {scan_since}")
        except ValueError:
            logger.warning(f"Ungültiges scan_since-Format '{scan_since}' – Filter deaktiviert")
            scan_since = None

    # ── Checkpoint laden (Resume) ──────────────────────────────────────────
    completed_dirs: list = []
    checkpoint_run_id: Optional[int] = None

    if resume and signal_dir:
        cp = read_checkpoint(signal_dir)
        if cp:
            completed_dirs    = cp.get("completed_dirs", [])
            checkpoint_run_id = cp.get("scan_run_id")
            cp_stats          = cp.get("stats", {})
            logger.info(
                f"Setze Scan #{checkpoint_run_id} fort. "
                f"{len(completed_dirs)} Verzeichnisse bereits abgeschlossen."
            )
        else:
            logger.warning("--resume angegeben, aber kein Checkpoint gefunden – starte neu.")

    # ── Scan-Run anlegen oder fortsetzen ───────────────────────────────────
    # Schreiben in ``scan_runs`` geschieht ausschliesslich ueber den Webapp-
    # Writer-Thread. Der Scanner emittiert per stdout, die Webapp wendet
    # es an. Die scan_run_id wird vom Scanner selbst vergeben (MAX(id)+1
    # auf seiner Reader-Connection) — solange kein zweiter Scanner parallel
    # laeuft (durch Admin-Orchestrierung garantiert), ist die Vergabe
    # kollisionsfrei.
    if checkpoint_run_id:
        scan_run_id = checkpoint_run_id
        emit(OP_START_RUN, scan_run_id=scan_run_id, resume=True)
        stats = {
            "total":     cp_stats.get("total",     0),
            "new":       cp_stats.get("new",       0),
            "changed":   cp_stats.get("changed",   0),
            "unchanged": cp_stats.get("unchanged", 0),
            "moved":     cp_stats.get("moved",     0),
            "restored":  cp_stats.get("restored",  0),
            "errors":    cp_stats.get("errors",    0),
        }
    else:
        _next = conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM scan_runs"
        ).fetchone()
        scan_run_id = int(_next[0])
        emit(
            OP_START_RUN,
            scan_run_id=scan_run_id,
            started_at=now,
            scan_paths=scan_paths,
            resume=False,
        )
        stats = {"total": 0, "new": 0, "changed": 0, "unchanged": 0,
                 "moved": 0, "restored": 0, "errors": 0}

    logger.info(f"Scan-Run #{scan_run_id} gestartet | Pfade: {scan_paths}")

    # ── Abbruch-Signale aus vorherigem Lauf bereinigen ────────────────────
    if signal_dir:
        clean_signals(signal_dir)

    # ── Hauptschleife über Scan-Pfade ─────────────────────────────────────
    try:
        blacklist = config.get("blacklist_paths", [])
        whitelist = config.get("whitelist_paths", [])

        try:
            parallel_shares = max(1, min(8, int(config.get("parallel_shares", 1))))
        except (TypeError, ValueError):
            parallel_shares = 1

        # Lock serialisiert Zugriffe auf ``stats``, ``completed_dirs`` und
        # ``write_checkpoint`` zwischen den Worker-Threads. ``emit`` bringt
        # seinen eigenen Lock mit (scanner_protocol._EMIT_LOCK).
        progress_lock = threading.Lock()

        def _run_share(scan_path: str) -> None:
            accessible, err_msg = _check_path_accessible(scan_path, logger)
            if not accessible:
                logger.warning(
                    f"Pfad nicht erreichbar: {scan_path} – {err_msg}"
                )
                logger.warning(
                    "  Hinweis: Bitte prüfen, ob der oben geloggte Scanner-"
                    "Benutzer Lesezugriff auf den UNC-Pfad besitzt und ob die "
                    "Freigabe vom Server aus aufgelöst werden kann (DNS, "
                    "Firewall, SMB-Version)."
                )
                with progress_lock:
                    stats["errors"] += 1
                return

            # Jeder Worker-Thread braucht eine eigene SQLite-Connection —
            # sqlite3.Connection ist nicht thread-safe. Lesemodus reicht
            # (alle Schreibvorgaenge gehen via ``emit`` nach stdout).
            local_conn = init_db(config["db_path"]) if parallel_shares > 1 else conn
            try:
                logger.info(f"Scanne: {scan_path}")

                # Dateien direkt im Wurzelverzeichnis (kein Subdir-Abstieg)
                root_chunk = f"__ROOT__{scan_path}"
                if root_chunk not in completed_dirs:
                    _check_and_handle_signals(signal_dir, logger)
                    _process_chunk(
                        walk_root_files(scan_path, config, scan_paths, logger, scan_since_ts),
                        local_conn, scan_run_id, now, logger, move_mode, stats, signal_dir,
                        auto_ignore=runtime_auto_ignore,
                        discard_no_formula=runtime_discard,
                        auto_classify_filename=runtime_classify_filename,
                        progress_lock=progress_lock,
                    )
                    with progress_lock:
                        completed_dirs.append(root_chunk)
                        if signal_dir:
                            write_checkpoint(signal_dir, scan_run_id, scan_paths,
                                             completed_dirs, stats)

                # Top-Level-Unterverzeichnisse als einzelne Checkpunkt-Einheiten
                for subdir in _get_toplevel_dirs(scan_path, blacklist, whitelist):
                    if subdir in completed_dirs:
                        logger.info(f"  Überspringe (bereits abgeschlossen): {subdir}")
                        continue

                    _check_and_handle_signals(signal_dir, logger)
                    logger.info(f"  Unterverzeichnis: {subdir}")
                    _process_chunk(
                        walk_and_scan(subdir, config, scan_paths, logger, scan_since_ts),
                        local_conn, scan_run_id, now, logger, move_mode, stats, signal_dir,
                        auto_ignore=runtime_auto_ignore,
                        discard_no_formula=runtime_discard,
                        auto_classify_filename=runtime_classify_filename,
                        progress_lock=progress_lock,
                    )
                    with progress_lock:
                        completed_dirs.append(subdir)
                        if signal_dir:
                            write_checkpoint(signal_dir, scan_run_id, scan_paths,
                                             completed_dirs, stats)
            finally:
                if parallel_shares > 1:
                    try:
                        local_conn.close()
                    except Exception:
                        pass

        if parallel_shares > 1 and len(scan_paths) > 1:
            workers = min(parallel_shares, len(scan_paths))
            logger.info(f"Parallelisierung aktiv: {workers} Freigaben gleichzeitig")
            cancel_exc: list = []
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan-share") as ex:
                futures = [ex.submit(_run_share, sp) for sp in scan_paths]
                for fut in as_completed(futures):
                    exc = fut.exception()
                    if exc is not None:
                        if isinstance(exc, ScanCancelledError):
                            cancel_exc.append(exc)
                        else:
                            raise exc
            if cancel_exc:
                raise cancel_exc[0]
        else:
            for scan_path in scan_paths:
                _run_share(scan_path)

        # ── Erfolgreich abgeschlossen ──────────────────────────────────────
        # mark_deleted_files emittiert jetzt ein OP_ARCHIVE_UNSEEN-Event;
        # die tatsaechliche Anzahl archivierter Dateien setzt der Webapp-
        # Handler in scan_runs.archived_files (siehe apply_scanner_archive_unseen).
        mark_deleted_files(conn, scan_run_id, now, scan_since, mapped_scan_paths)

        # ── Auto-Ignorieren am Scan-Ende: verbleibende Excel-Dateien ohne Formeln ─
        # (deckt Dateien ab, die bereits vor diesem Scan existierten und noch 'Neu' sind)
        # WICHTIG: Nur Excel-Dateien – andere Dateitypen (Access, Cognos, Skripte …)
        # dürfen nicht pauschal ignoriert werden, nur weil sie keine Formeln haben.
        if runtime_auto_ignore:
            emit(
                OP_UPDATE_STATUS,
                kind="auto_ignore_bulk_excel",
                extensions=sorted(_EXCEL_EXTENSIONS),
            )
            logger.info("  Auto-Ignorieren (Excel ohne Formeln) emittiert")

        # ── Auto-Klassifizierung nach Dateiname am Scan-Ende ──────────────────
        # (deckt Dateien ab, die bereits vor diesem Scan existierten und noch 'Neu' sind)
        if runtime_classify_filename:
            emit(OP_UPDATE_STATUS, kind="auto_classify_bulk_ah")
            emit(OP_UPDATE_STATUS, kind="auto_classify_bulk_idv")
            logger.info("  Auto-Klassifizierung (AH/IDV) emittiert")

        if stats.get("discarded"):
            logger.info(f"  Verworfen       : {stats['discarded']} Excel-Dateien (kein Formel/Makro)")

        finished = datetime.now(timezone.utc).isoformat()
        emit(
            OP_END_RUN,
            scan_run_id=scan_run_id,
            finished_at=finished,
            status="completed",
            total=stats["total"], new=stats["new"], changed=stats["changed"],
            moved=stats["moved"], restored=stats["restored"],
            archived=0, errors=stats["errors"],
        )
        conn.close()

        if signal_dir:
            remove_checkpoint(signal_dir)

        logger.info("=" * 60)
        logger.info(f"Scan abgeschlossen in Run #{scan_run_id}")
        logger.info(f"  Gesamt gefunden : {stats['total']}")
        logger.info(f"  Neu             : {stats['new']}")
        logger.info(f"  Geändert        : {stats['changed']}")
        logger.info(f"  Verschoben      : {stats['moved']}")
        logger.info(f"  Wiederhergest.  : {stats['restored']}")
        logger.info(f"  Archiviert      : (siehe scan_runs.archived_files – wird vom Webapp-Writer gesetzt)")
        logger.info(f"  Fehler          : {stats['errors']}")
        logger.info("=" * 60)

    except ScanCancelledError:
        # ── Scan abgebrochen – Zwischenstand sichern ──────────────────────
        # Scanner-Connection ist read-only; Schreibvorgaenge liegen bereits
        # als NDJSON auf stdout und werden von der Webapp appliziert.
        finished = datetime.now(timezone.utc).isoformat()
        emit(
            OP_END_RUN,
            scan_run_id=scan_run_id,
            finished_at=finished,
            status="cancelled",
            total=stats["total"], new=stats["new"], changed=stats["changed"],
            moved=stats["moved"], restored=stats["restored"],
            archived=0, errors=stats["errors"],
        )
        conn.close()

        if signal_dir:
            clean_signals(signal_dir)
            # Checkpoint-Datei bleibt erhalten, damit Resume möglich ist

        logger.warning("=" * 60)
        logger.warning(f"Scan #{scan_run_id} ABGEBROCHEN durch Benutzer.")
        logger.warning(f"  Bisher verarbeitet: {stats['total']} Dateien "
                       f"({len(completed_dirs)} Verzeichnisse abgeschlossen)")
        logger.warning("  Resume mit: --resume (oder über die Webapp)")
        logger.warning("=" * 60)
        _flush_log(logger)

    except BaseException as e:
        # ── Unerwarteter Crash – so viel wie möglich sichern ─────────────
        logger.critical("=" * 60)
        logger.critical(f"UNERWARTETER FEHLER in Scan #{scan_run_id}")
        logger.critical(f"  Typ:     {type(e).__name__}")
        logger.critical(f"  Meldung: {e}")
        logger.critical(f"  Bisher verarbeitet: {stats['total']} Dateien "
                        f"({len(completed_dirs)} Verzeichnisse)")
        logger.critical(f"  Traceback:\n{traceback.format_exc()}")
        logger.critical("=" * 60)
        _flush_log(logger)

        # Scanner-Connection ist read-only; Schreibvorgaenge liegen bereits
        # als NDJSON auf stdout und werden von der Webapp appliziert.
        try:
            finished = datetime.now(timezone.utc).isoformat()
            emit(
                OP_END_RUN,
                scan_run_id=scan_run_id,
                finished_at=finished,
                status="crashed",
                total=stats["total"], new=stats["new"], changed=stats["changed"],
                moved=stats["moved"], restored=stats["restored"],
                archived=0, errors=stats["errors"] + 1,
            )
        except Exception as db_err:
            logger.critical(f"DB-Sicherung fehlgeschlagen: {db_err}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # Checkpoint schreiben für Resume
        if signal_dir:
            try:
                write_checkpoint(signal_dir, scan_run_id, scan_paths,
                                 completed_dirs, stats)
            except Exception as cp_err:
                logger.critical(f"Checkpoint-Sicherung fehlgeschlagen: {cp_err}")
            clean_signals(signal_dir)

        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_config_from_db(db_path: str) -> dict:
    """Liest scanner_config + path_mappings aus app_settings und verschmilzt
    mit DEFAULT_CONFIG. db_path ist absolut und wird in config übernommen."""
    import sqlite3
    cfg = dict(DEFAULT_CONFIG)
    cfg["db_path"] = db_path
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        def _read_json(key: str, default):
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key=?", (key,)
            ).fetchone()
            if not row or not row["value"]:
                return default
            try:
                return json.loads(row["value"])
            except (TypeError, ValueError):
                return default

        scanner_cfg = _read_json("scanner_config", {})
        if isinstance(scanner_cfg, dict):
            cfg.update(scanner_cfg)
        path_mappings = _read_json("path_mappings", [])
        if isinstance(path_mappings, list):
            cfg["path_mappings"] = path_mappings
    finally:
        conn.close()
    return cfg


def main():
    parser = argparse.ArgumentParser(description="IDV-Scanner – Netzlaufwerk-Discovery")
    parser.add_argument("--db-path", default=None,
                        help="Pfad zur SQLite-DB. Scanner-Config + path_mappings werden "
                             "aus app_settings gelesen; der Log-Pfad wird aus <db_parent>/logs "
                             "abgeleitet.")
    parser.add_argument("--config", default=None,
                        help="Fallback: Pfad zu einer JSON-Config (nur für Ad-hoc-CLI-Tests "
                             "ohne Webapp-DB).")
    parser.add_argument("--signal-dir", default=None,
                        help="Verzeichnis für Signal-Dateien (Pause/Abbruch/Checkpoint). "
                             "Standard: Verzeichnis der DB bzw. der config.json")
    parser.add_argument("--resume", action="store_true",
                        help="Setzt einen unterbrochenen Scan (Checkpoint) fort")
    args = parser.parse_args()

    if not args.db_path and not args.config:
        print("Fehler: --db-path oder --config erforderlich.", file=sys.stderr)
        sys.exit(2)

    if args.db_path:
        db_path = os.path.abspath(args.db_path)
        if not os.path.isfile(db_path):
            print(f"DB nicht gefunden: {db_path}", file=sys.stderr)
            sys.exit(1)
        config = _load_config_from_db(db_path)
        log_path = os.path.join(os.path.dirname(db_path), "logs", "network_scanner.log")
        config["log_path"] = log_path
        signal_dir = args.signal_dir if args.signal_dir else os.path.dirname(db_path)
    else:
        # Fallback: JSON-Config für CLI-Standalone-Läufe ohne Webapp-DB.
        config = dict(DEFAULT_CONFIG)
        if not os.path.isfile(args.config):
            print(f"Konfiguration nicht gefunden: {args.config}", file=sys.stderr)
            sys.exit(1)
        with open(args.config, encoding="utf-8") as f:
            raw = json.load(f)
        scanner_data = raw.get("scanner", raw)
        config.update({k: v for k, v in scanner_data.items() if k in DEFAULT_CONFIG})
        if "path_mappings" in raw:
            config["path_mappings"] = raw["path_mappings"]
        _config_dir = os.path.dirname(os.path.abspath(args.config))
        for _key in ("db_path", "log_path"):
            _val = raw.get(_key) or scanner_data.get(_key)
            if _val:
                config[_key] = _val if os.path.isabs(_val) \
                               else os.path.normpath(os.path.join(_config_dir, _val))
        if not config.get("db_path"):
            print("Fehler: 'db_path' muss in der JSON-Config gesetzt sein.", file=sys.stderr)
            sys.exit(2)
        if not config.get("log_path"):
            config["log_path"] = os.path.join(
                os.path.dirname(config["db_path"]), "logs", "network_scanner.log"
            )
        signal_dir = args.signal_dir if args.signal_dir else _config_dir

    # Sicherstellen, dass das Log-Verzeichnis existiert (sonst bricht
    # FileHandler beim ersten Schreibversuch ab).
    try:
        os.makedirs(os.path.dirname(config["log_path"]), exist_ok=True)
    except (OSError, TypeError):
        pass

    logger = setup_logging(config["log_path"])

    _set_keep_awake(True)
    try:
        run_scan(config, logger, signal_dir=signal_dir, resume=args.resume)
    except BaseException as e:
        # Letzter Rettungsanker: Crash-Details in Log UND stderr schreiben,
        # damit die Ursache auch ohne Zugriff auf die Konsole erkennbar ist.
        tb = traceback.format_exc()
        try:
            logger.critical(f"Scanner abgestürzt: {type(e).__name__}: {e}\n{tb}")
            _flush_log(logger)
        except Exception:
            pass
        # Auch in stderr schreiben (wird von der Webapp nach scanner_output.log umgeleitet)
        print(f"FATAL: {type(e).__name__}: {e}\n{tb}", file=sys.stderr)
        sys.exit(2)
    finally:
        _set_keep_awake(False)


if __name__ == "__main__":
    main()
