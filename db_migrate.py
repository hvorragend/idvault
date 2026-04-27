"""
Sidecar-DB-Migration: Bereinigt idv_files-Zeilen mit U+FFFD im full_path
=========================================================================

Hintergrund
-----------
Vor dem Umlaute-Fix in scanner_protocol.py ist Scanner-stdout in cp1252
statt UTF-8 in die Webapp-Pipe geflossen, weil sys.stdout in der
Subprozess-Umgebung (Windows-Dienst, PyInstaller-Bundle) nicht zuverlaessig
auf UTF-8 stand. Die Webapp dekodierte den Stream als UTF-8 mit
``errors='replace'``, sodass Umlaute (cp1252-Single-Bytes wie 0xE4 / 0xDC)
als U+FFFD ('\\ufffd', dargestellt als '?') in idv_files.full_path
landeten.

Solange diese kaputt-pfadigen Zeilen im DB liegen, kollidieren sie bei
Folge-Scans mit der nun korrekt-pfadigen Version derselben Datei:
    - UNIQUE constraint failed: idv_files.full_path  (INSERT)
    - Move-Detection-Storm (jede Datei wird als 'moved' erkannt)
    - OP_ARCHIVE_UNSEEN archiviert die kaputt-pfadigen Zeilen, sodass
      sie aus 'Eingang' / 'Alle Funde' verschwinden

Dieses Skript loescht die kaputt-pfadigen Zeilen kontrolliert. Beim
naechsten Vollscan werden die Dateien mit korrekten Pfaden neu eingespielt.

Self-Heal: Scanner-Top-Level-Module flach legen
-----------------------------------------------
Der ``_SidecarFinder`` in run.py findet Top-Level-Module nur direkt
unter ``updates/<modul>.py`` – nicht unter ``updates/scanner/<modul>.py``.
Die Whitelist in ``webapp/routes/admin/__init__.py`` umzumappen reicht
nicht, weil Package-``__init__.py``s explizit nicht aus dem Sidecar
geladen werden (run.py:145-148): die alte gebundle Whitelist verarbeitet
jedes Sidecar-ZIP, sodass ``scanner_protocol.py`` weiterhin unter
``updates/scanner/`` landet und nie geladen wird.

Damit der Umlaute-Fix in ``scanner_protocol.py`` ohne EXE-Neubau wirksam
wird, kopieren wir hier zu Beginn des Webapp-Starts die relevanten
Top-Level-Module flach in ``updates/``. Das passiert vor dem Blueprint-
Import, der ``from scanner_protocol import ...`` ausloest, und auch vor
dem Spawn des Scanner-Subprozesses.

Sicherheitsnetz
---------------
Zeilen, die mit einem manuell registrierten IDV verknuepft sind
(``idv_register.file_id`` oder ``idv_file_links.file_id``), werden NICHT
geloescht – sondern auf ``status='archiviert'`` gesetzt und in den
WARNING-Log geschrieben, damit der Admin sie haendisch nachpflegen kann.

Idempotenz
----------
Beim zweiten Aufruf gibt es keine Treffer mehr und das Skript ist ein
No-Op. Es kann gefahrlos im Sidecar-Ordner liegen bleiben.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3

# U+FFFD = REPLACEMENT CHARACTER ('?'). Keine personenbezogenen
# Daten: nur das Sentinel-Zeichen, das die kaputten Zeilen markiert.
_FFFD = "�"

# Tabellen mit nullable FK auf idv_files(id), die wir vor dem DELETE
# auf NULL setzen, damit der DELETE nicht an einem FK-Constraint
# scheitert (PRAGMA foreign_keys=ON ist in db_pragmas.py aktiviert).
# Tabellen mit ON DELETE CASCADE werden hier bewusst nicht aufgefuehrt –
# die kuemmert SQLite selbst.
_NULLABLE_FK_TARGETS = (
    ("cognos_reports",     "idv_file_id"),
    ("self_service_audit", "file_id"),
)

# Scanner-Top-Level-Module, die im PyInstaller-Bundle ueber
# ``pathex=['.', 'scanner']`` flach importiert werden. Wenn sie im
# Sidecar-ZIP unter scanner/<modul>.py liegen (alte Whitelist im
# laufenden Bundle), wird hier die flache Kopie nachgezogen.
_SCANNER_TOPLEVEL_MODULES = (
    "scanner_protocol",
    "path_utils",
    "network_scanner",
    "excel_export",
    "teams_scanner",
)


def _flatten_scanner_modules(logger: logging.Logger) -> None:
    """Kopiert ``updates/scanner/<modul>.py`` nach ``updates/<modul>.py``,
    wenn die flache Variante fehlt oder aelter ist. Sonst wuerde der
    SidecarFinder die geupdatete Datei nicht finden.
    """
    # __file__ ist updates/db_migrate.py (das ruft webapp/__init__.py auf).
    updates_dir = os.path.dirname(os.path.abspath(__file__))
    nested_dir = os.path.join(updates_dir, "scanner")
    if not os.path.isdir(nested_dir):
        return  # Keine geschachtelte Variante => nichts zu tun

    copied = 0
    for mod in _SCANNER_TOPLEVEL_MODULES:
        src = os.path.join(nested_dir, mod + ".py")
        dst = os.path.join(updates_dir, mod + ".py")
        if not os.path.isfile(src):
            continue
        # Nur kopieren wenn dst fehlt oder src neuer ist – verhindert,
        # dass ein neueres flaches Update von einer alten geschachtelten
        # Datei ueberschrieben wird.
        if os.path.isfile(dst):
            try:
                if os.path.getmtime(src) <= os.path.getmtime(dst):
                    continue
            except OSError:
                continue
        try:
            shutil.copy2(src, dst)
            copied += 1
            logger.warning(
                "Sidecar-Self-Heal: %s aus updates/scanner/ flach kopiert "
                "nach updates/%s.py", mod, mod,
            )
        except OSError as exc:
            logger.warning(
                "Sidecar-Self-Heal: Kopie %s -> %s fehlgeschlagen: %s",
                src, dst, exc,
            )

    if copied:
        logger.warning(
            "Sidecar-Self-Heal: %d Scanner-Modul(e) flach gelegt. "
            "Beim naechsten Scan-Subprozess- bzw. Blueprint-Import wird die "
            "neue Version aus updates/ geladen.",
            copied,
        )


def run(db_path: str) -> None:
    logger = logging.getLogger("idv_migrate.umlaut_cleanup")

    # 1. Self-Heal: Scanner-Module flach legen, bevor der Webapp sie
    #    via Blueprint-Import zieht.
    try:
        _flatten_scanner_modules(logger)
    except Exception as exc:  # niemals den Start blockieren
        logger.warning("Sidecar-Self-Heal fehlgeschlagen: %s", exc)

    # 2. DB-Cleanup

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        # FK-Enforcement an, damit wir blinde Verwaisungen sofort sehen.
        conn.execute("PRAGMA foreign_keys = ON")

        rows = conn.execute(
            "SELECT id, full_path, file_hash FROM idv_files "
            "WHERE full_path LIKE ? "
            "  AND status != 'archiviert'",
            (f"%{_FFFD}%",),
        ).fetchall()

        if not rows:
            return  # idempotent: nichts zu tun

        logger.warning(
            "Umlaut-Cleanup: %d idv_files-Zeile(n) mit U+FFFD im full_path "
            "gefunden – Detail-Log folgt.",
            len(rows),
        )

        ids_to_delete: list[int] = []
        ids_to_archive: list[int] = []

        for r in rows:
            file_id = r["id"]
            in_register = conn.execute(
                "SELECT COUNT(*) FROM idv_register WHERE file_id = ?",
                (file_id,),
            ).fetchone()[0]
            in_links = conn.execute(
                "SELECT COUNT(*) FROM idv_file_links WHERE file_id = ?",
                (file_id,),
            ).fetchone()[0]

            if in_register or in_links:
                ids_to_archive.append(file_id)
                logger.warning(
                    "  id=%d: archiviere statt zu loeschen "
                    "(idv_register=%d, idv_file_links=%d) – manuell nachpflegen",
                    file_id, in_register, in_links,
                )
            else:
                ids_to_delete.append(file_id)

        with conn:
            if ids_to_delete:
                placeholders = ",".join("?" * len(ids_to_delete))

                # 1. NOT-NULL-FK ohne CASCADE: idv_file_history
                conn.execute(
                    f"DELETE FROM idv_file_history "
                    f"WHERE file_id IN ({placeholders})",
                    ids_to_delete,
                )

                # 2. Nullable-FKs auf NULL setzen (Audit-Trail bleibt erhalten,
                #    nur der File-Bezug wird gekappt).
                for tbl, col in _NULLABLE_FK_TARGETS:
                    try:
                        conn.execute(
                            f"UPDATE {tbl} SET {col} = NULL "
                            f"WHERE {col} IN ({placeholders})",
                            ids_to_delete,
                        )
                    except sqlite3.OperationalError:
                        # Tabelle existiert in dieser Installation nicht.
                        pass

                # 3. Schlussendlich die idv_files-Zeilen.
                conn.execute(
                    f"DELETE FROM idv_files WHERE id IN ({placeholders})",
                    ids_to_delete,
                )
                logger.warning(
                    "Umlaut-Cleanup: %d idv_files-Zeile(n) geloescht.",
                    len(ids_to_delete),
                )

            if ids_to_archive:
                placeholders = ",".join("?" * len(ids_to_archive))
                conn.execute(
                    f"UPDATE idv_files SET status='archiviert' "
                    f"WHERE id IN ({placeholders})",
                    ids_to_archive,
                )
                logger.warning(
                    "Umlaut-Cleanup: %d idv_files-Zeile(n) archiviert "
                    "(manuell verknuepft, Pflege durch Admin noetig).",
                    len(ids_to_archive),
                )
    finally:
        conn.close()
