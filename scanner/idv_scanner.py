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
import hashlib
import sqlite3
import zipfile
import logging
import argparse
import json
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
        "\\$RECYCLE.BIN\\",
        "\\System Volume Information\\",
        "\\AppData\\",
    ],
    "db_path": "idv_register.db",
    "log_path": "idv_scanner.log",
    "hash_size_limit_mb": 500,   # Dateien > X MB werden nicht gehasht (Performance)
    "max_workers": 4
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_path: str) -> logging.Logger:
    logger = logging.getLogger("IDVScanner")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


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
    deleted_files   INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS idv_files (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash           TEXT NOT NULL,          -- SHA-256, eindeutiger Fingerabdruck
    full_path           TEXT NOT NULL,
    file_name           TEXT NOT NULL,
    extension           TEXT NOT NULL,
    share_root          TEXT,                   -- UNC-Root / Laufwerksbuchstabe
    relative_path       TEXT,                   -- Pfad relativ zum Share-Root
    size_bytes          INTEGER,
    created_at          TEXT,                   -- Dateisystem-Erstelldatum (UTC)
    modified_at         TEXT,                   -- Dateisystem-Änderungsdatum (UTC)
    file_owner          TEXT,                   -- Windows-Eigentümer (SID/Name)
    office_author       TEXT,                   -- dc:creator aus OOXML
    office_last_author  TEXT,                   -- cp:lastModifiedBy aus OOXML
    office_created      TEXT,                   -- dcterms:created aus OOXML
    office_modified     TEXT,                   -- dcterms:modified aus OOXML
    has_macros          INTEGER DEFAULT 0,       -- 1 = VBA-Projekt vorhanden
    has_external_links  INTEGER DEFAULT 0,       -- 1 = externe Verknüpfungen
    sheet_count         INTEGER,                -- Anzahl Tabellenblätter (Excel)
    named_ranges_count  INTEGER,                -- Anzahl benannter Bereiche
    first_seen_at       TEXT NOT NULL,          -- Erster Fund (UTC)
    last_seen_at        TEXT NOT NULL,          -- Letzter Scan (UTC)
    last_scan_run_id    INTEGER,
    status              TEXT DEFAULT 'active',  -- active | deleted | moved
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
    return conn


# ---------------------------------------------------------------------------
# Hash-Berechnung
# ---------------------------------------------------------------------------

def sha256_file(path: str, chunk_size: int = 65536) -> Optional[str]:
    """SHA-256-Hash einer Datei. Gibt None zurück bei Fehler."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                h.update(chunk)
        return h.hexdigest()
    except (PermissionError, OSError):
        return None


# ---------------------------------------------------------------------------
# Betriebssystem-Metadaten
# ---------------------------------------------------------------------------

def get_fs_metadata(path: str) -> dict:
    """Dateisystem-Metadaten einer Datei."""
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

    if HAS_WIN32:
        try:
            sd = win32security.GetFileSecurity(
                path, win32security.OWNER_SECURITY_INFORMATION
            )
            owner_sid = sd.GetSecurityDescriptorOwner()
            name, domain, _ = win32security.LookupAccountSid(None, owner_sid)
            result["file_owner"] = f"{domain}\\{name}"
        except Exception:
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
        "office_author":       None,
        "office_last_author":  None,
        "office_created":      None,
        "office_modified":     None,
        "has_macros":          0,
        "has_external_links":  0,
        "sheet_count":         None,
        "named_ranges_count":  None,
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

                # --- Benannte Bereiche (workbook.xml) ---
                if "xl/workbook.xml" in names:
                    try:
                        with z.open("xl/workbook.xml") as f:
                            wb_tree = ET.parse(f)
                            wb_root = wb_tree.getroot()
                            ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                            defined = wb_root.findall(f".//{{{ns}}}definedName")
                            result["named_ranges_count"] = len(defined)
                    except Exception:
                        pass

    except Exception:
        pass

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
# Scanner-Kern
# ---------------------------------------------------------------------------

def scan_file(path: str, config: dict, scan_paths: list) -> Optional[dict]:
    """Analysiert eine einzelne Datei und gibt ein Metadaten-Dict zurück."""
    ext = Path(path).suffix.lower()
    fs  = get_fs_metadata(path)

    # Hash nur bei vertretbarer Dateigröße
    size_limit = config.get("hash_size_limit_mb", 500) * 1024 * 1024
    file_hash  = None
    if fs["size_bytes"] is not None and fs["size_bytes"] <= size_limit:
        file_hash = sha256_file(path)

    # OOXML-Analyse für Office-Dateien
    ooxml_exts = {".xlsx", ".xlsm", ".xlsb", ".xltm", ".xltx",
                  ".docm", ".dotm", ".pptm", ".pptx", ".docx"}
    ooxml = {}
    if ext in ooxml_exts:
        ooxml = analyze_ooxml(path, ext)

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
        "has_macros":         ooxml.get("has_macros", 0),
        "has_external_links": ooxml.get("has_external_links", 0),
        "sheet_count":        ooxml.get("sheet_count"),
        "named_ranges_count": ooxml.get("named_ranges_count"),
    }


def walk_and_scan(scan_path: str, config: dict, all_scan_paths: list,
                  logger: logging.Logger):
    """Generator: liefert Metadaten-Dicts für alle gefundenen Dateien."""
    extensions = set(e.lower() for e in config["extensions"])
    excludes   = config["exclude_paths"]

    for root, dirs, files in os.walk(scan_path, followlinks=False):
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

            try:
                data = scan_file(full_path, config, all_scan_paths)
                if data:
                    yield data
            except Exception as e:
                logger.warning(f"Fehler bei {full_path}: {e}")


# ---------------------------------------------------------------------------
# Datenbank-Upsert & Delta-Erkennung
# ---------------------------------------------------------------------------

def upsert_file(conn: sqlite3.Connection, data: dict,
                scan_run_id: int, now: str, logger: logging.Logger) -> str:
    """
    Fügt eine Datei ein oder aktualisiert sie.
    Gibt change_type zurück: 'new' | 'changed' | 'unchanged'
    """
    cur = conn.execute(
        "SELECT id, file_hash, modified_at FROM idv_files WHERE full_path = ?",
        (data["full_path"],)
    )
    existing = cur.fetchone()

    if existing is None:
        # Neue Datei

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
                first_seen_at, last_seen_at, last_scan_run_id, status
            ) VALUES (
                :file_hash, :full_path, :file_name, :extension, :share_root,
                :relative_path, :size_bytes, :created_at, :modified_at, :file_owner,
                :office_author, :office_last_author, :office_created, :office_modified,
                :has_macros, :has_external_links, :sheet_count, :named_ranges_count,
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
        file_id   = existing["id"]
        old_hash  = existing["file_hash"]
        new_hash  = data["file_hash"]
        changed   = old_hash != new_hash

        conn.execute("""
            UPDATE idv_files SET
                file_hash = :file_hash, size_bytes = :size_bytes,
                modified_at = :modified_at, file_owner = :file_owner,
                office_author = :office_author, office_last_author = :office_last_author,
                office_modified = :office_modified,
                has_macros = :has_macros, has_external_links = :has_external_links,
                sheet_count = :sheet_count, named_ranges_count = :named_ranges_count,
                last_seen_at = :now, last_scan_run_id = :run_id, status = 'active'
            WHERE full_path = :full_path
        """, {**data, "now": now, "run_id": scan_run_id})

        change_type = "changed" if changed else "unchanged"
        conn.execute("""
            INSERT INTO idv_file_history
                (file_id, scan_run_id, change_type, old_hash, new_hash, changed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (file_id, scan_run_id, change_type, old_hash, new_hash, now))

        return change_type


def mark_deleted_files(conn: sqlite3.Connection, scan_run_id: int, now: str) -> int:
    """Markiert Dateien als gelöscht, die im letzten Scan nicht mehr gefunden wurden.
 
    Nutzt last_scan_run_id statt eines Python-Sets – skaliert auch bei 100k+ Dateien.
    """
    rows = conn.execute(
        "SELECT id FROM idv_files WHERE status = 'active' AND last_scan_run_id != ?",
        (scan_run_id,)
    ).fetchall()
 
    if not rows:
        return 0
 
    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE idv_files SET status = 'deleted', last_seen_at = ? WHERE id IN ({placeholders})",
        [now] + ids
    )
    conn.executemany("""
        INSERT INTO idv_file_history (file_id, scan_run_id, change_type, changed_at)
        VALUES (?, ?, 'deleted', ?)
    """, [(fid, scan_run_id, now) for fid in ids])
 
    return len(ids)


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def run_scan(config: dict, logger: logging.Logger):
    scan_paths = config["scan_paths"]
    if not scan_paths:
        logger.error("Keine Scan-Pfade konfiguriert. Bitte config.json anpassen.")
        sys.exit(1)

    conn = init_db(config["db_path"])
    now  = datetime.now(timezone.utc).isoformat()

    # Scan-Run starten
    cur = conn.execute(
        "INSERT INTO scan_runs (started_at, scan_paths) VALUES (?, ?)",
        (now, json.dumps(scan_paths))
    )
    conn.commit()
    scan_run_id = cur.lastrowid
    logger.info(f"Scan-Run #{scan_run_id} gestartet | Pfade: {scan_paths}")

    stats = {"total": 0, "new": 0, "changed": 0, "unchanged": 0, "errors": 0}

    for scan_path in scan_paths:
        if not os.path.exists(scan_path):
            logger.warning(f"Pfad nicht erreichbar: {scan_path}")
            stats["errors"] += 1
            continue

        logger.info(f"Scanne: {scan_path}")
        for data in walk_and_scan(scan_path, config, scan_paths, logger):
            try:
                change = upsert_file(conn, data, scan_run_id, now, logger)
                stats["total"]   += 1
                stats[change]    += 1

                if stats["total"] % 100 == 0:
                    conn.commit()
                    logger.info(f"  … {stats['total']} Dateien verarbeitet")

            except Exception as e:
                logger.error(f"DB-Fehler bei {data.get('full_path')}: {e}")
                stats["errors"] += 1

    conn.commit()

    # Gelöschte Dateien markieren (SQL-basiert über last_scan_run_id)
    deleted = mark_deleted_files(conn, scan_run_id, now)
    conn.commit()

    finished = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        UPDATE scan_runs SET
            finished_at = ?, total_files = ?, new_files = ?,
            changed_files = ?, deleted_files = ?, errors = ?
        WHERE id = ?
    """, (finished, stats["total"], stats["new"],
          stats["changed"], deleted, stats["errors"], scan_run_id))
    conn.commit()
    conn.close()

    logger.info("=" * 60)
    logger.info(f"Scan abgeschlossen in Run #{scan_run_id}")
    logger.info(f"  Gesamt gefunden : {stats['total']}")
    logger.info(f"  Neu             : {stats['new']}")
    logger.info(f"  Geändert        : {stats['changed']}")
    logger.info(f"  Unverändert     : {stats['unchanged']}")
    logger.info(f"  Gelöscht        : {deleted}")
    logger.info(f"  Fehler          : {stats['errors']}")
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="IDV-Scanner – Netzlaufwerk-Discovery")
    parser.add_argument("--config", default="config.json",
                        help="Pfad zur Konfigurationsdatei (default: config.json)")
    parser.add_argument("--init-config", action="store_true",
                        help="Erstellt eine Beispiel-config.json und beendet sich")
    args = parser.parse_args()

    if args.init_config:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print("config.json erstellt. Bitte Scan-Pfade anpassen.")
        sys.exit(0)

    # Konfiguration laden
    config = dict(DEFAULT_CONFIG)
    if os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            config.update(json.load(f))
    else:
        print(f"Keine config.json gefunden. Starte mit: python idv_scanner.py --init-config")
        sys.exit(1)

    logger = setup_logging(config["log_path"])
    run_scan(config, logger)


if __name__ == "__main__":
    main()
