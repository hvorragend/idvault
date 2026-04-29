"""Admin-Sub-Modul: Datensicherung der Admin-Konfiguration (Issue #445).

Sichert die administrativ pflegbaren Stammdaten als ZIP-Datei (eine
JSON-Datei pro Tabelle + ``meta.json``). ``persons`` und alle
operationalen Daten (IDV-Register, Prüfungen, Maßnahmen, Scanner-Funde
etc.) sind ausgeschlossen.

LDAP-Server-Konfiguration und Gruppen-Rollen-Mapping sind enthalten.

Restore-Strategie
-----------------
* Atomar in einer einzigen Schreibtransaktion über den Writer-Thread.
* ``PRAGMA foreign_keys = OFF`` um die Tabellen leeren zu können, ohne
  Cascades aus operationalen Tabellen auszulösen. Nach dem Commit wird
  ``PRAGMA foreign_keys = ON`` wieder gesetzt.
* Original-IDs werden beibehalten — operationale Daten, die per FK auf
  ``org_units``, ``geschaeftsprozesse``, ``klassifizierungen`` etc.
  zeigen, bleiben damit konsistent (Voraussetzung: Restore in dieselbe
  Installation oder in eine leere Instanz).
* ``fund_pfad_profile`` enthält FKs auf ``persons``. Da Mitarbeiter
  nicht im Backup sind, werden diese FKs beim Restore auf NULL
  gesetzt, falls die referenzierte Person in der Zielinstanz fehlt.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any

from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from db_write_tx import write_tx

from ... import limiter
from ...db_writer import get_writer
from .. import admin_required, get_db, login_required
from . import bp, _upload_rate_limit


@bp.route("/datensicherung")
@login_required
def datensicherung():
    """Eigener Reiter fuer Datensicherung & Wiederherstellung."""
    return render_template("admin/datensicherung.html")


# Reihenfolge ist relevant: Eltern vor Kindern. Beim Restore werden die
# Tabellen rückwärts geleert (Kinder zuerst) und vorwärts neu befüllt.
BACKUP_TABLES: list[str] = [
    "org_units",
    "plattformen",
    "geschaeftsprozesse",
    "klassifizierungen",
    "wesentlichkeitskriterien",
    "wesentlichkeitskriterium_details",
    "freigabe_pools",
    "ldap_config",
    "ldap_group_role_mapping",
    "testfall_vorlagen",
    "testfall_vorlage_scope",
    "glossar_eintraege",
    "fund_pfad_profile",
    "app_settings",
]

# Spalten in fund_pfad_profile, die auf persons(id) verweisen. Werden
# beim Restore validiert und ggf. auf NULL gesetzt.
_FUND_PFAD_PROFILE_PERSON_FKS = (
    "fachverantwortlicher_id",
    "idv_koordinator_id",
    "created_by_id",
)

BACKUP_FORMAT_VERSION = 1


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in rows]


def _serialize_row(row: sqlite3.Row, columns: list[str]) -> dict[str, Any]:
    return {col: row[col] for col in columns}


def build_backup_bytes(conn: sqlite3.Connection) -> bytes:
    """Erzeugt das Backup-ZIP als Bytes-Objekt."""
    bundled_version = current_app.config.get("BUNDLED_VERSION", "")
    active_version = current_app.config.get("APP_VERSION", bundled_version)

    meta = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app_version": active_version,
        "tables": list(BACKUP_TABLES),
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "meta.json",
            json.dumps(meta, ensure_ascii=False, indent=2),
        )
        for table in BACKUP_TABLES:
            columns = _table_columns(conn, table)
            if not columns:
                continue
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            payload = {
                "columns": columns,
                "rows": [_serialize_row(r, columns) for r in rows],
            }
            zf.writestr(
                f"tables/{table}.json",
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
    return buf.getvalue()


@bp.route("/backup/export")
@admin_required
def backup_export():
    """Lädt die Admin-Konfiguration als ZIP herunter."""
    payload = build_backup_bytes(get_db())
    fname = f"idvault-config-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    return send_file(
        io.BytesIO(payload),
        mimetype="application/zip",
        as_attachment=True,
        download_name=fname,
    )


def _read_backup(zip_path: str) -> tuple[dict, dict[str, dict]]:
    """Liest meta.json und alle tables/*.json aus dem ZIP.

    Wirft ``ValueError`` bei Format-Problemen.
    """
    tables: dict[str, dict] = {}
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        if "meta.json" not in names:
            raise ValueError("meta.json fehlt im Backup-ZIP.")
        try:
            meta = json.loads(zf.read("meta.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"meta.json unlesbar: {exc}") from exc

        if meta.get("format_version") != BACKUP_FORMAT_VERSION:
            raise ValueError(
                f"Unbekannte Backup-Format-Version: "
                f"{meta.get('format_version')!r}"
            )

        for table in BACKUP_TABLES:
            entry = f"tables/{table}.json"
            if entry not in names:
                continue
            try:
                data = json.loads(zf.read(entry).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"{entry} unlesbar: {exc}") from exc
            if not isinstance(data, dict) or "rows" not in data:
                raise ValueError(f"{entry}: ungültiges Format.")
            tables[table] = data

    return meta, tables


def _scrub_fund_pfad_profile(
    rows: list[dict], existing_person_ids: set[int]
) -> tuple[list[dict], int]:
    """Setzt Personen-FKs auf NULL, wenn die Person nicht (mehr) existiert.

    Gibt (bereinigte_rows, anzahl_genullte_FK_werte) zurück.
    """
    scrubbed_count = 0
    for row in rows:
        for col in _FUND_PFAD_PROFILE_PERSON_FKS:
            pid = row.get(col)
            if pid is not None and pid not in existing_person_ids:
                row[col] = None
                scrubbed_count += 1
    return rows, scrubbed_count


def restore_backup(conn: sqlite3.Connection, tables: dict[str, dict]) -> dict:
    """Spielt die Tabellen-Inhalte zurück. Muss aus dem Writer-Thread
    aufgerufen werden.

    Erwartet eine bereits validierte ``tables``-Struktur (Output von
    ``_read_backup``). Setzt ``foreign_keys=OFF`` um die Konfig-Tabellen
    leeren und neu befüllen zu können, ohne dass operationale Daten
    (idv_register, …) durch CASCADE-Regeln gelöscht werden.

    Gibt eine Statistik mit Anzahl eingespielter Zeilen pro Tabelle
    zurück.
    """
    stats: dict[str, int] = {}
    person_ids: set[int] = {
        r[0] for r in conn.execute("SELECT id FROM persons").fetchall()
    }

    # foreign_keys-Pragma kann nur außerhalb einer Transaktion umgeschaltet
    # werden. Die Connection des Writer-Threads ist zwischen Jobs im
    # Autocommit-Modus, daher hier sicher.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with write_tx(conn):
            # Erst die Kinder leeren (umgekehrte Reihenfolge)
            for table in reversed(BACKUP_TABLES):
                if table in tables:
                    conn.execute(f"DELETE FROM {table}")

            # Dann in Reihenfolge wieder befüllen
            for table in BACKUP_TABLES:
                if table not in tables:
                    continue
                payload = tables[table]
                rows = list(payload.get("rows", []))
                if table == "fund_pfad_profile":
                    rows, _ = _scrub_fund_pfad_profile(rows, person_ids)

                if not rows:
                    stats[table] = 0
                    continue

                schema_cols = set(_table_columns(conn, table))
                # Spalten bestimmen, die sowohl im Backup als auch im
                # Schema existieren — robust gegen Schema-Drift.
                first_row = rows[0]
                cols = [c for c in first_row.keys() if c in schema_cols]
                if not cols:
                    stats[table] = 0
                    continue

                placeholders = ",".join("?" * len(cols))
                col_list = ",".join(cols)
                sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
                conn.executemany(
                    sql, [tuple(r.get(c) for c in cols) for r in rows]
                )
                stats[table] = len(rows)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    return stats


@bp.route("/backup/restore", methods=["POST"])
@admin_required
@limiter.limit(_upload_rate_limit, methods=["POST"])
def backup_restore():
    """Spielt eine zuvor heruntergeladene Sicherung zurück."""
    f = request.files.get("backup_zip")
    if not f or not f.filename:
        flash("Keine Datei ausgewählt.", "error")
        return redirect(url_for("admin.datensicherung"))

    if not f.filename.lower().endswith(".zip"):
        flash("Nur ZIP-Dateien sind erlaubt.", "error")
        return redirect(url_for("admin.datensicherung"))

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name
            f.save(tmp)

        try:
            meta, tables = _read_backup(tmp_path)
        except (zipfile.BadZipFile, ValueError) as exc:
            flash(f"Ungültiges Backup-ZIP: {exc}", "error")
            return redirect(url_for("admin.datensicherung"))

        def _do(c: sqlite3.Connection) -> dict:
            return restore_backup(c, tables)

        try:
            stats = get_writer().submit(_do, wait=True)
        except Exception as exc:
            current_app.logger.exception("Restore fehlgeschlagen")
            flash(f"Restore fehlgeschlagen: {exc}", "error")
            return redirect(url_for("admin.datensicherung"))

        total = sum(stats.values())
        flash(
            f"Konfiguration zurückgespielt: {total} Datensätze in "
            f"{len(stats)} Tabellen (Backup vom "
            f"{meta.get('created_at', '?')}).",
            "success",
        )
    finally:
        if tmp_path:
            try:
                import os
                os.unlink(tmp_path)
            except OSError:
                pass

    return redirect(url_for("admin.datensicherung"))
