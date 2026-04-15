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
import hashlib
import sqlite3
import zipfile
import logging
import argparse
import json
import traceback
import ctypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

# Windows-spezifische Imports (optional, graceful fallback)
try:
    import win32security
    import win32api
    import ntsecuritycon
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


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
    "exclude_paths": [
        "~$",          # Office-Sperrdateien
        ".tmp",
        "$RECYCLE.BIN",
        "System Volume Information",
        "AppData",
    ],
    "db_path": "idv_register.db",
    "log_path": "idv_scanner.log",
    "hash_size_limit_mb": 500,   # Dateien > X MB werden nicht gehasht (Performance)
    "max_workers": 4,
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
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    # Incremental migration: scan_status für bestehende Datenbanken
    col_names = {r[1] for r in conn.execute("PRAGMA table_info(scan_runs)").fetchall()}
    if "scan_status" not in col_names:
        conn.execute(
            "ALTER TABLE scan_runs ADD COLUMN scan_status TEXT NOT NULL DEFAULT 'completed'"
        )
        conn.commit()
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
            result["file_owner"] = f"{domain}\\{name}"
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

    return {
        "file_hash":          file_hash or "HASH_ERROR",
        "full_path":          path,
        "file_name":          Path(path).name,
        "extension":          ext,
        "share_root":         share_root,
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
    excludes   = config["exclude_paths"]

    for root, dirs, files in safe_walk(scan_path, followlinks=False, logger=logger):
        # Ausschlusspfade: dirs in-place filtern (verhindert Abstieg)
        dirs[:] = [
            d for d in dirs
            if not should_exclude(os.path.join(root, d), excludes)
        ]

        for fname in files:
            ext = Path(fname).suffix.lower()
            if ext not in extensions:
                continue

            full_path = os.path.join(root, fname)
            if should_exclude(full_path, excludes):
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
    excludes   = config["exclude_paths"]
    try:
        entries = os.listdir(scan_path)
    except OSError as e:
        logger.warning(f"Kann Verzeichnis nicht öffnen: {scan_path}: {e}")
        return
    for fname in entries:
        full_path = os.path.join(scan_path, fname)
        if not os.path.isfile(full_path):
            continue
        ext = Path(fname).suffix.lower()
        if ext not in extensions:
            continue
        if should_exclude(full_path, excludes):
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


def _get_toplevel_dirs(scan_path: str, excludes: list) -> list:
    """Gibt eine sortierte Liste der direkten Unterverzeichnisse zurück."""
    try:
        return sorted([
            os.path.join(scan_path, d)
            for d in os.listdir(scan_path)
            if os.path.isdir(os.path.join(scan_path, d))
            and not should_exclude(os.path.join(scan_path, d), excludes)
        ])
    except OSError:
        return []


_EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xlsb", ".xls", ".xltm", ".xltx"}


def _process_chunk(chunk_gen, conn: sqlite3.Connection, scan_run_id: int,
                   now: str, logger: logging.Logger, move_mode: str,
                   stats: dict, signal_dir: Optional[str],
                   auto_ignore: bool = False,
                   discard_no_formula: bool = False) -> None:
    """Verarbeitet alle Dateien eines Generators, prüft alle 10 Dateien die Signale.

    auto_ignore:       Neue Excel-Dateien ohne Formeln/Makros sofort als 'Ignoriert' markieren.
    discard_no_formula: Neue Excel-Dateien ohne Formeln/Makros komplett überspringen (nicht in DB).
    """
    # Signal-Check zu Beginn jedes Chunks (wichtig bei Verzeichnissen mit < 10 Dateien)
    _check_and_handle_signals(signal_dir, logger)
    file_counter = 0
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
                    stats["discarded"] = stats.get("discarded", 0) + 1
                    logger.debug(f"Verworfen (kein Formel): {current_path}")
                    continue

            change = upsert_file(conn, data, scan_run_id, now, logger, move_mode)
            stats["total"]  += 1
            stats[change]   += 1
            file_counter    += 1

            # ── Auto-Ignore: neue Excel-Dateien ohne Formeln sofort ignorieren ──
            if auto_ignore and change in ("new", "restored") and is_excel and no_formula:
                conn.execute(
                    "UPDATE idv_files SET bearbeitungsstatus = 'Ignoriert' "
                    "WHERE full_path = ? AND status = 'active' "
                    "  AND bearbeitungsstatus = 'Neu' "
                    "  AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = idv_files.id)"
                    "  AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = idv_files.id)",
                    (data["full_path"],)
                )

            if file_counter % 10 == 0:
                _check_and_handle_signals(signal_dir, logger)
                _flush_log(logger)

            if stats["total"] % 100 == 0:
                conn.commit()
                logger.info(f"  … {stats['total']} Dateien verarbeitet")
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
                conn.execute("""
                    UPDATE idv_files SET
                        full_path = :full_path, share_root = :share_root,
                        relative_path = :relative_path,
                        last_seen_at = :now, last_scan_run_id = :run_id
                    WHERE id = :id
                """, {**data, "now": now, "run_id": scan_run_id, "id": moved_from["id"]})
                conn.execute("""
                    INSERT INTO idv_file_history
                        (file_id, scan_run_id, change_type, old_hash, new_hash, changed_at, details)
                    VALUES (?, ?, 'moved', ?, ?, ?, ?)
                """, (moved_from["id"], scan_run_id,
                      data["file_hash"], data["file_hash"], now,
                      json.dumps({"old_path": moved_from["full_path"],
                                  "new_path": data["full_path"]})))
                logger.debug(f"Verschoben: {moved_from['full_path']} → {data['full_path']}")
                return "moved"

        # ── Echter Neuzugang ────────────────────────────────────────────
        insert_data = {
            **data,
            "first_seen_at":    now,
            "last_seen_at":     now,
            "last_scan_run_id": scan_run_id,
        }
        cur_ins = conn.execute("""
            INSERT INTO idv_files (
                file_hash, full_path, file_name, extension, share_root,
                relative_path, size_bytes, created_at, modified_at, file_owner,
                office_author, office_last_author, office_created, office_modified,
                has_macros, has_external_links, sheet_count, named_ranges_count,
                formula_count,
                has_sheet_protection, protected_sheets_count,
                sheet_protection_has_pw, workbook_protected,
                ist_cognos_report, cognos_report_name, cognos_paket_pfad,
                cognos_abfragen_anzahl, cognos_datenpunkte_anzahl, cognos_filter_anzahl,
                cognos_seiten_anzahl, cognos_parameter_anzahl, cognos_namespace_version,
                first_seen_at, last_seen_at, last_scan_run_id, status
            ) VALUES (
                :file_hash, :full_path, :file_name, :extension, :share_root,
                :relative_path, :size_bytes, :created_at, :modified_at, :file_owner,
                :office_author, :office_last_author, :office_created, :office_modified,
                :has_macros, :has_external_links, :sheet_count, :named_ranges_count,
                :formula_count,
                :has_sheet_protection, :protected_sheets_count,
                :sheet_protection_has_pw, :workbook_protected,
                :ist_cognos_report, :cognos_report_name, :cognos_paket_pfad,
                :cognos_abfragen_anzahl, :cognos_datenpunkte_anzahl, :cognos_filter_anzahl,
                :cognos_seiten_anzahl, :cognos_parameter_anzahl, :cognos_namespace_version,
                :first_seen_at, :last_seen_at, :last_scan_run_id, 'active'
            )
        """, insert_data)
        file_id = cur_ins.lastrowid
        conn.execute("""
            INSERT INTO idv_file_history (file_id, scan_run_id, change_type, new_hash, changed_at)
            VALUES (?, ?, 'new', ?, ?)
        """, (file_id, scan_run_id, data["file_hash"], now))
        return "new"

    else:
        # ── Bekannte Datei: Update ──────────────────────────────────────
        file_id      = existing["id"]
        old_hash     = existing["file_hash"]
        new_hash     = data["file_hash"]
        was_archived = existing["status"] == "archiviert"

        conn.execute("""
            UPDATE idv_files SET
                file_hash = :file_hash, size_bytes = :size_bytes,
                modified_at = :modified_at, file_owner = :file_owner,
                office_author = :office_author, office_last_author = :office_last_author,
                office_modified = :office_modified,
                has_macros = :has_macros, has_external_links = :has_external_links,
                sheet_count = :sheet_count, named_ranges_count = :named_ranges_count,
                formula_count = :formula_count,
                has_sheet_protection = :has_sheet_protection,
                protected_sheets_count = :protected_sheets_count,
                sheet_protection_has_pw = :sheet_protection_has_pw,
                workbook_protected = :workbook_protected,
                ist_cognos_report = :ist_cognos_report,
                cognos_report_name = :cognos_report_name,
                cognos_paket_pfad = :cognos_paket_pfad,
                cognos_abfragen_anzahl = :cognos_abfragen_anzahl,
                cognos_datenpunkte_anzahl = :cognos_datenpunkte_anzahl,
                cognos_filter_anzahl = :cognos_filter_anzahl,
                cognos_seiten_anzahl = :cognos_seiten_anzahl,
                cognos_parameter_anzahl = :cognos_parameter_anzahl,
                cognos_namespace_version = :cognos_namespace_version,
                last_seen_at = :now, last_scan_run_id = :run_id, status = 'active'
            WHERE full_path = :full_path
        """, {**data, "now": now, "run_id": scan_run_id})

        if was_archived:
            change_type = "restored"
        else:
            change_type = "changed" if old_hash != new_hash else "unchanged"

        conn.execute("""
            INSERT INTO idv_file_history
                (file_id, scan_run_id, change_type, old_hash, new_hash, changed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (file_id, scan_run_id, change_type, old_hash, new_hash, now))
        return change_type


def mark_deleted_files(conn: sqlite3.Connection, scan_run_id: int, now: str,
                       scan_since: Optional[str] = None,
                       scan_paths: Optional[list] = None) -> int:
    """Überführt aktive Dateien, die im aktuellen Scan nicht gesehen wurden, ins Archiv.

    Nutzt last_scan_run_id statt eines Python-Sets – skaliert auch bei 100k+ Dateien.
    Dateien werden nicht gelöscht, sondern auf status='archiviert' gesetzt.

    scan_since:  ISO-Datumsstring (z.B. '2024-07-01'). Wenn gesetzt, werden nur
                 Dateien archiviert, deren modified_at >= scan_since liegt.

    scan_paths:  Liste der in diesem Lauf tatsächlich gescannten Pfade. Wenn gesetzt,
                 werden nur Dateien archiviert, deren full_path unter einem dieser
                 Pfade liegt. Dateien außerhalb des Geltungsbereichs bleiben unberührt
                 — so können mehrere Teilscans auf verschiedene Verzeichnisse korrekt
                 akkumuliert werden.
    """
    conditions = ["status = 'active'", "last_scan_run_id != ?"]
    params: list = [scan_run_id]

    if scan_since:
        conditions.append("modified_at >= ?")
        params.append(scan_since)

    if scan_paths:
        # Nur Dateien im Geltungsbereich der gescannten Pfade archivieren
        path_conds = " OR ".join("full_path LIKE ?" for _ in scan_paths)
        conditions.append(f"({path_conds})")
        for sp in scan_paths:
            # Normalisierung: Trennzeichen am Ende entfernen, dann % anhängen
            params.append(sp.rstrip("/\\") + "%")

    rows = conn.execute(
        f"SELECT id FROM idv_files WHERE {' AND '.join(conditions)}",
        params
    ).fetchall()

    if not rows:
        return 0

    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE idv_files SET status = 'archiviert', last_seen_at = ? WHERE id IN ({placeholders})",
        [now] + ids
    )
    conn.executemany("""
        INSERT INTO idv_file_history (file_id, scan_run_id, change_type, changed_at)
        VALUES (?, ?, 'archiviert', ?)
    """, [(fid, scan_run_id, now) for fid in ids])

    return len(ids)


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

    conn = init_db(config["db_path"])
    now  = datetime.now(timezone.utc).isoformat()

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

    if runtime_auto_ignore:
        logger.info("Laufzeit-Auto-Ignore aktiv: neue Excel-Dateien ohne Formeln werden sofort ignoriert")
    if runtime_discard:
        logger.info("Verwerfen aktiv: neue Excel-Dateien ohne Formeln werden nicht in die DB aufgenommen")

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
    if checkpoint_run_id:
        scan_run_id = checkpoint_run_id
        conn.execute(
            "UPDATE scan_runs SET scan_status = 'running' WHERE id = ?",
            (scan_run_id,)
        )
        conn.commit()
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
        cur = conn.execute(
            "INSERT INTO scan_runs (started_at, scan_paths, scan_status) VALUES (?, ?, 'running')",
            (now, json.dumps(scan_paths))
        )
        conn.commit()
        scan_run_id = cur.lastrowid
        stats = {"total": 0, "new": 0, "changed": 0, "unchanged": 0,
                 "moved": 0, "restored": 0, "errors": 0}

    logger.info(f"Scan-Run #{scan_run_id} gestartet | Pfade: {scan_paths}")

    # ── Abbruch-Signale aus vorherigem Lauf bereinigen ────────────────────
    if signal_dir:
        clean_signals(signal_dir)

    # ── Hauptschleife über Scan-Pfade ─────────────────────────────────────
    try:
        excludes = config["exclude_paths"]

        for scan_path in scan_paths:
            if not os.path.exists(scan_path):
                logger.warning(f"Pfad nicht erreichbar: {scan_path}")
                stats["errors"] += 1
                continue

            logger.info(f"Scanne: {scan_path}")

            # Dateien direkt im Wurzelverzeichnis (kein Subdir-Abstieg)
            root_chunk = f"__ROOT__{scan_path}"
            if root_chunk not in completed_dirs:
                _check_and_handle_signals(signal_dir, logger)
                _process_chunk(
                    walk_root_files(scan_path, config, scan_paths, logger, scan_since_ts),
                    conn, scan_run_id, now, logger, move_mode, stats, signal_dir,
                    auto_ignore=runtime_auto_ignore,
                    discard_no_formula=runtime_discard,
                )
                conn.commit()
                completed_dirs.append(root_chunk)
                if signal_dir:
                    write_checkpoint(signal_dir, scan_run_id, scan_paths,
                                     completed_dirs, stats)

            # Top-Level-Unterverzeichnisse als einzelne Checkpunkt-Einheiten
            for subdir in _get_toplevel_dirs(scan_path, excludes):
                if subdir in completed_dirs:
                    logger.info(f"  Überspringe (bereits abgeschlossen): {subdir}")
                    continue

                _check_and_handle_signals(signal_dir, logger)
                logger.info(f"  Unterverzeichnis: {subdir}")
                _process_chunk(
                    walk_and_scan(subdir, config, scan_paths, logger, scan_since_ts),
                    conn, scan_run_id, now, logger, move_mode, stats, signal_dir,
                    auto_ignore=runtime_auto_ignore,
                    discard_no_formula=runtime_discard,
                )
                conn.commit()
                completed_dirs.append(subdir)
                if signal_dir:
                    write_checkpoint(signal_dir, scan_run_id, scan_paths,
                                     completed_dirs, stats)

        # ── Erfolgreich abgeschlossen ──────────────────────────────────────
        deleted = mark_deleted_files(conn, scan_run_id, now, scan_since, scan_paths)
        conn.commit()

        # ── Auto-Ignorieren am Scan-Ende: verbleibende Dateien ohne Formeln ─
        # (deckt Dateien ab, die bereits vor diesem Scan existierten und noch 'Neu' sind)
        auto_ignored = 0
        try:
            if runtime_auto_ignore:
                cur_ai = conn.execute("""
                    UPDATE idv_files
                    SET bearbeitungsstatus = 'Ignoriert'
                    WHERE status = 'active'
                      AND bearbeitungsstatus = 'Neu'
                      AND (formula_count IS NULL OR formula_count = 0)
                      AND (has_macros IS NULL OR has_macros = 0)
                      AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = idv_files.id)
                      AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = idv_files.id)
                """)
                auto_ignored = cur_ai.rowcount
                conn.commit()
                if auto_ignored:
                    logger.info(f"  Auto-Ignoriert  : {auto_ignored} Dateien (ohne Formeln/Makros)")
        except Exception as e:
            logger.warning(f"Auto-Ignorieren fehlgeschlagen: {e}")

        if stats.get("discarded"):
            logger.info(f"  Verworfen       : {stats['discarded']} Dateien (kein Formel/Makro)")

        finished = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE scan_runs SET
                finished_at = ?, total_files = ?, new_files = ?,
                changed_files = ?, moved_files = ?, restored_files = ?,
                archived_files = ?, errors = ?, scan_status = 'completed'
            WHERE id = ?
        """, (finished, stats["total"], stats["new"], stats["changed"],
              stats["moved"], stats["restored"], deleted, stats["errors"], scan_run_id))
        conn.commit()
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
        logger.info(f"  Archiviert      : {deleted}")
        logger.info(f"  Fehler          : {stats['errors']}")
        logger.info("=" * 60)

    except ScanCancelledError:
        # ── Scan abgebrochen – Zwischenstand sichern ──────────────────────
        conn.commit()
        finished = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE scan_runs SET
                finished_at = ?, total_files = ?, new_files = ?,
                changed_files = ?, moved_files = ?, restored_files = ?,
                archived_files = 0, errors = ?, scan_status = 'cancelled'
            WHERE id = ?
        """, (finished, stats["total"], stats["new"], stats["changed"],
              stats["moved"], stats["restored"], stats["errors"], scan_run_id))
        conn.commit()
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

        # DB-Zustand sichern, soweit möglich
        try:
            conn.commit()
            finished = datetime.now(timezone.utc).isoformat()
            conn.execute("""
                UPDATE scan_runs SET
                    finished_at = ?, total_files = ?, new_files = ?,
                    changed_files = ?, moved_files = ?, restored_files = ?,
                    archived_files = 0, errors = ?, scan_status = 'crashed'
                WHERE id = ?
            """, (finished, stats["total"], stats["new"], stats["changed"],
                  stats["moved"], stats["restored"], stats["errors"] + 1, scan_run_id))
            conn.commit()
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

def main():
    parser = argparse.ArgumentParser(description="IDV-Scanner – Netzlaufwerk-Discovery")
    parser.add_argument("--config", default="config.json",
                        help="Pfad zur Konfigurationsdatei (default: config.json)")
    parser.add_argument("--signal-dir", default=None,
                        help="Verzeichnis für Signal-Dateien (Pause/Abbruch/Checkpoint). "
                             "Standard: Verzeichnis der config.json")
    parser.add_argument("--init-config", action="store_true",
                        help="Erstellt eine Beispiel-config.json und beendet sich")
    parser.add_argument("--resume", action="store_true",
                        help="Setzt einen unterbrochenen Scan (Checkpoint) fort")
    args = parser.parse_args()

    if args.init_config:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump({"scanner": DEFAULT_CONFIG}, f, indent=2, ensure_ascii=False)
        print("config.json erstellt. Bitte Scan-Pfade unter 'scanner' anpassen.")
        sys.exit(0)

    # Konfiguration laden
    # Unterstützt zusammengeführte config.json mit "scanner"-Abschnitt
    # sowie die frühere flache Scanner-Konfiguration (rückwärtskompatibel).
    config = dict(DEFAULT_CONFIG)
    if os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            raw = json.load(f)
        if "scanner" in raw:
            config.update(raw["scanner"])
        else:
            config.update(raw)
    else:
        print(f"Keine config.json gefunden. Starte mit: python idv_scanner.py --init-config")
        sys.exit(1)

    signal_dir = args.signal_dir if args.signal_dir else os.path.dirname(os.path.abspath(args.config))
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
