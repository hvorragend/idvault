"""Admin-Blueprint: Stammdaten verwalten"""
import csv
import io
import os
import sys
import json
import re
import hashlib
import shutil
import subprocess
import tempfile
import threading
import zipfile
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response, jsonify, current_app
from . import login_required, admin_required, write_access_required, get_db
from datetime import datetime, timezone, timedelta

bp = Blueprint("admin", __name__, url_prefix="/admin")

# ── Scanner-Konfiguration & Scan-Trigger ────────────────────────────────────

_scan_lock  = threading.Lock()
_scan_state = {"pid": None, "started": None}   # veränderlich, kein global nötig


def _scanner_dir():
    if getattr(sys, 'frozen', False):
        # Im PyInstaller-Bundle: Ordner neben der .exe (persistent & beschreibbar)
        return os.path.join(os.path.dirname(sys.executable), "scanner")
    return os.path.join(os.path.dirname(current_app.root_path), "scanner")


def _scanner_config_path():
    # Zusammengeführte config.json liegt neben der EXE bzw. im Projektverzeichnis
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), "config.json")
    return os.path.join(os.path.dirname(current_app.root_path), "config.json")


def _scanner_script_path():
    return os.path.join(_scanner_dir(), "idv_scanner.py")


_DEFAULT_SCANNER_EXTENSIONS = [
    ".xls", ".xlsx", ".xlsm", ".xlsb", ".xltm", ".xltx",
    ".accdb", ".mdb", ".accde", ".accdr",
    ".ida", ".idv",
    ".bas", ".cls", ".frm",
    ".pbix", ".pbit",
    ".dotm", ".pptm",
    ".py", ".r", ".rmd", ".sql",
]
_DEFAULT_SCANNER_EXCLUDE = [
    "~$", ".tmp",
    "$RECYCLE.BIN",
    "System Volume Information",
    "AppData",
]


def _instance_logs_dir() -> str:
    """Gibt den absoluten Pfad zum instance/logs/-Verzeichnis zurück."""
    return os.path.join(os.path.dirname(current_app.config['DATABASE']), 'logs')


def _default_scanner_cfg() -> dict:
    """Erstellt die Standardkonfiguration mit dem tatsächlichen DB-Pfad der Webapp."""
    from flask import current_app
    return {
        "scan_paths": [],
        "extensions": _DEFAULT_SCANNER_EXTENSIONS,
        "exclude_paths": _DEFAULT_SCANNER_EXCLUDE,
        "db_path": current_app.config['DATABASE'],
        "log_path": os.path.join(_instance_logs_dir(), 'idv_scanner.log'),
        "hash_size_limit_mb": 500,
        "max_workers": 4,
        "move_detection": "name_and_hash",
        "scan_since": None,
        "read_file_owner": True,
    }


def _load_scanner_config() -> dict:
    cfg = _default_scanner_cfg()
    try:
        with open(_scanner_config_path(), encoding="utf-8") as f:
            full = json.load(f)
        # Zusammengeführte config.json: Scanner-Einstellungen unter "scanner"-Schlüssel
        cfg.update(full.get("scanner", full))
    except Exception:
        pass
    return cfg


def _save_scanner_config(cfg: dict):
    path = _scanner_config_path()
    # Bestehende config.json einlesen um andere Schlüssel (z.B. SECRET_KEY) zu erhalten
    try:
        with open(path, encoding="utf-8") as f:
            full = json.load(f)
    except Exception:
        full = {}
    full["scanner"] = cfg
    with open(path, "w", encoding="utf-8") as f:
        json.dump(full, f, indent=2, ensure_ascii=False)


def _scan_is_running() -> bool:
    pid = _scan_state.get("pid")
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        _scan_state["pid"]     = None
        _scan_state["started"] = None
        return False


def _pause_path() -> str:
    return os.path.join(_scanner_dir(), "scanner_pause.signal")


def _cancel_path() -> str:
    return os.path.join(_scanner_dir(), "scanner_cancel.signal")


def _checkpoint_path() -> str:
    return os.path.join(_scanner_dir(), "scanner_checkpoint.json")


def _scan_is_paused() -> bool:
    return os.path.exists(_pause_path())


def _has_checkpoint() -> bool:
    return os.path.exists(_checkpoint_path())


@bp.route("/scanner-einstellungen", methods=["GET", "POST"])
@admin_required
def scanner_einstellungen():
    cfg = _load_scanner_config()

    if request.method == "POST":
        scan_paths    = [p.strip() for p in request.form.get("scan_paths",    "").splitlines() if p.strip()]
        extensions    = [e.strip().lower() for e in request.form.get("extensions",    "").splitlines() if e.strip()]
        exclude_paths = [p.strip() for p in request.form.get("exclude_paths", "").splitlines() if p.strip()]

        try:
            hash_limit  = max(1, int(request.form.get("hash_size_limit_mb", 500)))
        except ValueError:
            hash_limit  = 500
        try:
            max_workers = max(1, min(32, int(request.form.get("max_workers", 4))))
        except ValueError:
            max_workers = 4

        move_det         = request.form.get("move_detection", "name_and_hash")
        scan_since       = request.form.get("scan_since", "").strip() or None
        read_file_owner  = request.form.get("read_file_owner") == "1"

        cfg.update({
            "scan_paths":        scan_paths,
            "extensions":        extensions,
            "exclude_paths":     exclude_paths,
            "hash_size_limit_mb": hash_limit,
            "max_workers":       max_workers,
            "move_detection":    move_det,
            "scan_since":        scan_since,
            "read_file_owner":   read_file_owner,
        })
        try:
            _save_scanner_config(cfg)
            # App-Settings für Auto-Ignorieren und Verwerfen speichern
            db = get_db()
            val_ai = "1" if request.form.get("auto_ignore_no_formula") == "1" else "0"
            val_dc = "1" if request.form.get("discard_no_formula") == "1" else "0"
            db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)",
                       ("auto_ignore_no_formula", val_ai))
            db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)",
                       ("discard_no_formula", val_dc))
            db.commit()
            flash("Scanner-Konfiguration gespeichert.", "success")
        except Exception as exc:
            flash(f"Fehler beim Speichern: {exc}", "error")
        return redirect(url_for("admin.scanner_einstellungen"))

    db = get_db()
    auto_ignore = db.execute(
        "SELECT value FROM app_settings WHERE key='auto_ignore_no_formula'"
    ).fetchone()
    discard_nf = db.execute(
        "SELECT value FROM app_settings WHERE key='discard_no_formula'"
    ).fetchone()
    return render_template("admin/scanner_einstellungen.html",
                           cfg=cfg, scan_running=_scan_is_running(),
                           auto_ignore_no_formula=(auto_ignore["value"] if auto_ignore else "0"),
                           discard_no_formula=(discard_nf["value"] if discard_nf else "0"))


@bp.route("/scanner/starten", methods=["POST"])
@write_access_required
def scanner_starten():
    with _scan_lock:
        if _scan_is_running():
            return jsonify({"ok": False, "msg": "Ein Scan läuft bereits."})

        config = _scanner_config_path()

        if not os.path.isfile(config):
            return jsonify({"ok": False, "msg": f"Konfiguration nicht gefunden: {config}"})

        scanner_dir = _scanner_dir()
        os.makedirs(scanner_dir, exist_ok=True)

        resume = request.form.get("resume") == "1" and _has_checkpoint()

        # Verwaiste Signal-Dateien aus einem vorherigen Crash bereinigen
        for sig_name in ("scanner_pause.signal", "scanner_cancel.signal"):
            sig_path = os.path.join(scanner_dir, sig_name)
            try:
                os.remove(sig_path)
            except FileNotFoundError:
                pass

        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, "--scan", "--config", config,
                   "--signal-dir", scanner_dir]
        else:
            script = _scanner_script_path()
            if not os.path.isfile(script):
                return jsonify({"ok": False, "msg": f"Scanner-Skript nicht gefunden: {script}"})
            cmd = [sys.executable, script, "--config", config,
                   "--signal-dir", scanner_dir]

        if resume:
            cmd.append("--resume")

        _logs_dir = _instance_logs_dir()
        os.makedirs(_logs_dir, exist_ok=True)
        output_log = os.path.join(_logs_dir, "scanner_output.log")
        try:
            log_fh = open(output_log, "w", encoding="utf-8")
            # Subprocess von der Konsole des Parents isolieren: sonst werden
            # Windows-Konsolen-Control-Events (z.B. CTRL_C_EVENT, die bei
            # Netzlaufwerk-Störungen ausgelöst werden) an alle an dieselbe
            # Konsole gekoppelten Prozesse zugestellt. Der Scanner fängt
            # KeyboardInterrupt ab und läuft weiter – der Flask-Prozess
            # (Werkzeug-Dev-Server) bekommt das gleiche Signal und beendet
            # sich still, wie bei Ctrl+C. Siehe claude/debug-network-scan.
            # Abbrechen erfolgt via Signal-Dateien, nicht via CTRL_C.
            _creationflags = 0
            if os.name == 'nt':
                _creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP  # eigene Signalgruppe
                    | subprocess.CREATE_NO_WINDOW         # keine geteilte Konsole
                )
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=log_fh,
                cwd=scanner_dir,
                creationflags=_creationflags,
            )
            _scan_state["pid"]     = proc.pid
            _scan_state["started"] = datetime.now(timezone.utc).isoformat()

            def _watch():
                proc.wait()
                log_fh.close()
                with _scan_lock:
                    if _scan_state.get("pid") == proc.pid:
                        _scan_state["pid"]     = None
                        _scan_state["started"] = None

            threading.Thread(target=_watch, daemon=True).start()
            mode_label = "fortgesetzt" if resume else "gestartet"
            return jsonify({"ok": True, "msg": f"Scan {mode_label} (PID {proc.pid}).", "pid": proc.pid})
        except Exception as exc:
            return jsonify({"ok": False, "msg": str(exc)})


@bp.route("/scanner/status")
@login_required
def scanner_status():
    running = _scan_is_running()
    return jsonify({
        "running":        running,
        "started":        _scan_state.get("started"),
        "paused":         _scan_is_paused() if running else False,
        "has_checkpoint": _has_checkpoint(),
    })


@bp.route("/scanner/pause", methods=["POST"])
@write_access_required
def scanner_pause():
    if not _scan_is_running():
        return jsonify({"ok": False, "msg": "Kein Scan aktiv."})
    os.makedirs(_scanner_dir(), exist_ok=True)
    open(_pause_path(), "w").close()
    return jsonify({"ok": True, "msg": "Pause angefordert."})


@bp.route("/scanner/fortsetzen", methods=["POST"])
@write_access_required
def scanner_fortsetzen():
    try:
        os.remove(_pause_path())
    except FileNotFoundError:
        pass
    return jsonify({"ok": True, "msg": "Fortsetzung signalisiert."})


@bp.route("/scanner/abbrechen", methods=["POST"])
@write_access_required
def scanner_abbrechen():
    if not _scan_is_running():
        return jsonify({"ok": False, "msg": "Kein Scan aktiv."})
    os.makedirs(_scanner_dir(), exist_ok=True)
    # Pause zuerst aufheben, dann Abbruch signalisieren
    try:
        os.remove(_pause_path())
    except FileNotFoundError:
        pass
    open(_cancel_path(), "w").close()
    return jsonify({"ok": True, "msg": "Abbruch angefordert."})


@bp.route("/scanner/bereinigen", methods=["POST"])
@admin_required
def scanner_bereinigen():
    db = get_db()
    try:
        tage = int(request.form.get("tage", 180))
    except (ValueError, TypeError):
        tage = 180
    if tage < 7:
        flash("Mindestalter für die Bereinigung: 7 Tage.", "error")
        return redirect(url_for("admin.scanner_einstellungen") + "#bereinigung")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=tage)).isoformat()

    hist_count = db.execute(
        "DELETE FROM idv_file_history WHERE changed_at < ?", (cutoff,)
    ).rowcount

    runs_count = db.execute("""
        DELETE FROM scan_runs
        WHERE started_at < ?
          AND id NOT IN (
              SELECT DISTINCT last_scan_run_id FROM idv_files
              WHERE last_scan_run_id IS NOT NULL
          )
    """, (cutoff,)).rowcount

    db.commit()
    flash(
        f"Bereinigung abgeschlossen: {hist_count} History-Einträge und "
        f"{runs_count} Scan-Läufe gelöscht (älter als {tage} Tage).",
        "success"
    )
    return redirect(url_for("admin.scanner_einstellungen") + "#bereinigung")


@bp.route("/scanner/db-importieren", methods=["POST"])
@admin_required
def scanner_db_importieren():
    """Importiert Scanner-Ergebnisse aus einer externen SQLite-Datei (Multi-Scanner-Merge)."""
    import sqlite3 as _sqlite3

    src_path = request.form.get("src_db_path", "").strip()
    if not src_path:
        flash("Bitte einen Datenbankpfad angeben.", "error")
        return redirect(url_for("admin.scanner_einstellungen") + "#db-import")

    if not os.path.isfile(src_path):
        flash(f"Datei nicht gefunden: {src_path}", "error")
        return redirect(url_for("admin.scanner_einstellungen") + "#db-import")

    try:
        src = _sqlite3.connect(src_path)
        src.row_factory = _sqlite3.Row
    except Exception as exc:
        flash(f"Quelldatenbank kann nicht geöffnet werden: {exc}", "error")
        return redirect(url_for("admin.scanner_einstellungen") + "#db-import")

    dst = get_db()
    now = datetime.now(timezone.utc).isoformat()

    stats = {"runs": 0, "files_new": 0, "files_updated": 0, "history": 0}

    try:
        # ── 1. scan_runs importieren ──────────────────────────────────────
        run_id_map = {}   # src_run_id → dst_run_id
        src_runs = src.execute(
            "SELECT * FROM scan_runs ORDER BY id"
        ).fetchall()
        for run in src_runs:
            cur = dst.execute("""
                INSERT INTO scan_runs
                    (started_at, finished_at, scan_paths,
                     total_files, new_files, changed_files, moved_files,
                     restored_files, archived_files, errors)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run["started_at"], run["finished_at"], run["scan_paths"],
                run["total_files"] or 0, run["new_files"] or 0,
                run["changed_files"] or 0, run["moved_files"] or 0,
                run["restored_files"] or 0, run["archived_files"] or 0,
                run["errors"] or 0,
            ))
            run_id_map[run["id"]] = cur.lastrowid
            stats["runs"] += 1

        # ── 2. idv_files zusammenführen ──────────────────────────────────
        file_id_map = {}  # src_file_id → dst_file_id
        src_files = src.execute("SELECT * FROM idv_files ORDER BY id").fetchall()
        for f in src_files:
            existing = dst.execute(
                "SELECT id, last_seen_at FROM idv_files WHERE full_path = ?",
                (f["full_path"],)
            ).fetchone()

            if existing is None:
                # Neue Datei einfügen
                cur = dst.execute("""
                    INSERT INTO idv_files (
                        file_hash, full_path, file_name, extension, share_root,
                        relative_path, size_bytes, created_at, modified_at, file_owner,
                        office_author, office_last_author, office_created, office_modified,
                        has_macros, has_external_links, sheet_count, named_ranges_count,
                        formula_count, has_sheet_protection, protected_sheets_count,
                        sheet_protection_has_pw, workbook_protected,
                        first_seen_at, last_seen_at, last_scan_run_id, status
                    ) VALUES (
                        :file_hash, :full_path, :file_name, :extension, :share_root,
                        :relative_path, :size_bytes, :created_at, :modified_at, :file_owner,
                        :office_author, :office_last_author, :office_created, :office_modified,
                        :has_macros, :has_external_links, :sheet_count, :named_ranges_count,
                        :formula_count, :has_sheet_protection, :protected_sheets_count,
                        :sheet_protection_has_pw, :workbook_protected,
                        :first_seen_at, :last_seen_at, :last_scan_run_id, :status
                    )
                """, {
                    "file_hash":          f["file_hash"],
                    "full_path":          f["full_path"],
                    "file_name":          f["file_name"],
                    "extension":          f["extension"],
                    "share_root":         f["share_root"],
                    "relative_path":      f["relative_path"],
                    "size_bytes":         f["size_bytes"],
                    "created_at":         f["created_at"],
                    "modified_at":        f["modified_at"],
                    "file_owner":         f["file_owner"],
                    "office_author":      f["office_author"],
                    "office_last_author": f["office_last_author"],
                    "office_created":     f["office_created"],
                    "office_modified":    f["office_modified"],
                    "has_macros":         f["has_macros"] or 0,
                    "has_external_links": f["has_external_links"] or 0,
                    "sheet_count":        f["sheet_count"],
                    "named_ranges_count": f["named_ranges_count"],
                    "formula_count":      f["formula_count"] or 0,
                    "has_sheet_protection":    f["has_sheet_protection"] or 0,
                    "protected_sheets_count":  f["protected_sheets_count"] or 0,
                    "sheet_protection_has_pw": f["sheet_protection_has_pw"] or 0,
                    "workbook_protected":      f["workbook_protected"] or 0,
                    "first_seen_at":      f["first_seen_at"] or now,
                    "last_seen_at":       f["last_seen_at"] or now,
                    "last_scan_run_id":   run_id_map.get(f["last_scan_run_id"]),
                    "status":             f["status"] or "active",
                })
                file_id_map[f["id"]] = cur.lastrowid
                stats["files_new"] += 1
            else:
                # Vorhandene Datei: aktualisieren wenn Quelle neuer
                file_id_map[f["id"]] = existing["id"]
                src_ts  = f["last_seen_at"] or ""
                dst_ts  = existing["last_seen_at"] or ""
                if src_ts > dst_ts:
                    dst.execute("""
                        UPDATE idv_files SET
                            file_hash = ?, size_bytes = ?,
                            modified_at = ?, file_owner = ?,
                            office_author = ?, office_last_author = ?,
                            office_modified = ?,
                            has_macros = ?, has_external_links = ?,
                            sheet_count = ?, named_ranges_count = ?,
                            formula_count = ?,
                            has_sheet_protection = ?, protected_sheets_count = ?,
                            sheet_protection_has_pw = ?, workbook_protected = ?,
                            last_seen_at = ?, last_scan_run_id = ?, status = ?
                        WHERE id = ?
                    """, (
                        f["file_hash"], f["size_bytes"],
                        f["modified_at"], f["file_owner"],
                        f["office_author"], f["office_last_author"],
                        f["office_modified"],
                        f["has_macros"] or 0, f["has_external_links"] or 0,
                        f["sheet_count"], f["named_ranges_count"],
                        f["formula_count"] or 0,
                        f["has_sheet_protection"] or 0, f["protected_sheets_count"] or 0,
                        f["sheet_protection_has_pw"] or 0, f["workbook_protected"] or 0,
                        f["last_seen_at"], run_id_map.get(f["last_scan_run_id"]),
                        f["status"] or "active",
                        existing["id"],
                    ))
                    stats["files_updated"] += 1

        # ── 3. idv_file_history übertragen ────────────────────────────────
        src_hist = src.execute("SELECT * FROM idv_file_history ORDER BY id").fetchall()
        for h in src_hist:
            dst_file_id = file_id_map.get(h["file_id"])
            dst_run_id  = run_id_map.get(h["scan_run_id"])
            if dst_file_id is None or dst_run_id is None:
                continue
            dst.execute("""
                INSERT INTO idv_file_history
                    (file_id, scan_run_id, change_type, old_hash, new_hash, changed_at, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                dst_file_id, dst_run_id,
                h["change_type"], h["old_hash"], h["new_hash"],
                h["changed_at"], h["details"],
            ))
            stats["history"] += 1

        dst.commit()
        src.close()

        flash(
            f"Import abgeschlossen: {stats['runs']} Scan-Läufe, "
            f"{stats['files_new']} neue Dateien, "
            f"{stats['files_updated']} aktualisiert, "
            f"{stats['history']} History-Einträge.",
            "success"
        )

    except Exception as exc:
        dst.rollback()
        src.close()
        flash(f"Fehler beim Import: {exc}", "error")

    return redirect(url_for("admin.scanner_einstellungen") + "#db-import")


# ── Teams-Scanner-Konfiguration & Scan-Trigger ──────────────────────────────

_teams_scan_lock  = threading.Lock()
_teams_scan_state = {"pid": None, "started": None}

_DEFAULT_TEAMS_EXTENSIONS = [
    ".xls", ".xlsx", ".xlsm", ".xlsb", ".xltm", ".xltx",
    ".accdb", ".mdb", ".accde", ".accdr",
    ".ida", ".idv",
    ".pbix", ".pbit",
    ".dotm", ".pptm",
    ".py", ".r", ".rmd", ".sql",
]


def _teams_config_path() -> str:
    return os.path.join(_scanner_dir(), "teams_config.json")


def _teams_script_path() -> str:
    return os.path.join(_scanner_dir(), "teams_scanner.py")


def _default_teams_cfg() -> dict:
    return {
        "tenant_id":          "",
        "client_id":          "",
        "client_secret":      "",
        "db_path":            current_app.config["DATABASE"],
        "log_path":           os.path.join(_instance_logs_dir(), 'teams_scanner.log'),
        "hash_size_limit_mb": 100,
        "download_for_ooxml": True,
        "move_detection":     "name_and_hash",
        "extensions":         _DEFAULT_TEAMS_EXTENSIONS,
        "teams":              [],
    }


def _load_teams_config() -> dict:
    cfg = _default_teams_cfg()
    try:
        with open(_teams_config_path(), encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def _save_teams_config(cfg: dict) -> None:
    path = _teams_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _teams_scan_is_running() -> bool:
    pid = _teams_scan_state.get("pid")
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        _teams_scan_state["pid"]     = None
        _teams_scan_state["started"] = None
        return False


@bp.route("/teams-einstellungen", methods=["GET", "POST"])
@admin_required
def teams_einstellungen():
    cfg = _load_teams_config()

    if request.method == "POST":
        tenant_id     = request.form.get("tenant_id",     "").strip()
        client_id     = request.form.get("client_id",     "").strip()
        client_secret = request.form.get("client_secret", "").strip()
        extensions    = [e.strip().lower() for e in request.form.get("extensions", "").splitlines() if e.strip()]
        move_det      = request.form.get("move_detection", "name_and_hash")

        try:
            hash_limit = max(1, int(request.form.get("hash_size_limit_mb", 100)))
        except ValueError:
            hash_limit = 100

        download_ooxml = request.form.get("download_for_ooxml") == "1"

        # Teams-Liste aus JSON-Feld (wird per JS aus der Tabelle serialisiert)
        try:
            teams_raw = request.form.get("teams_json", "[]")
            teams = json.loads(teams_raw)
            if not isinstance(teams, list):
                teams = []
        except (ValueError, TypeError):
            teams = []
            flash("Teams-Liste konnte nicht gelesen werden – bitte prüfen.", "warning")

        cfg.update({
            "tenant_id":          tenant_id,
            "client_id":          client_id,
            "client_secret":      client_secret,
            "extensions":         extensions or _DEFAULT_TEAMS_EXTENSIONS,
            "hash_size_limit_mb": hash_limit,
            "download_for_ooxml": download_ooxml,
            "move_detection":     move_det,
            "teams":              teams,
        })
        try:
            _save_teams_config(cfg)
            flash("Teams-Konfiguration gespeichert.", "success")
        except Exception as exc:
            flash(f"Fehler beim Speichern: {exc}", "error")
        return redirect(url_for("admin.teams_einstellungen"))

    return render_template("admin/teams_einstellungen.html",
                           cfg=cfg,
                           teams_scan_running=_teams_scan_is_running())


@bp.route("/teams/starten", methods=["POST"])
@write_access_required
def teams_scan_starten():
    with _teams_scan_lock:
        if _teams_scan_is_running():
            return jsonify({"ok": False, "msg": "Ein Teams-Scan läuft bereits."})

        config_path = _teams_config_path()
        if not os.path.isfile(config_path):
            return jsonify({"ok": False, "msg": "Teams-Konfiguration nicht gefunden. Bitte zuerst speichern."})

        cfg = _load_teams_config()
        if not cfg.get("teams"):
            return jsonify({"ok": False, "msg": "Keine Teams/Sites konfiguriert."})
        if not cfg.get("tenant_id") or not cfg.get("client_id"):
            return jsonify({"ok": False, "msg": "Azure AD Zugangsdaten (tenant_id, client_id) fehlen."})

        scanner_dir = _scanner_dir()
        os.makedirs(scanner_dir, exist_ok=True)

        script = _teams_script_path()
        if not os.path.isfile(script):
            return jsonify({"ok": False, "msg": f"Teams-Scanner nicht gefunden: {script}"})

        cmd = [sys.executable, script, "--config", config_path]
        _logs_dir = _instance_logs_dir()
        os.makedirs(_logs_dir, exist_ok=True)
        output_log = os.path.join(_logs_dir, "teams_scanner_output.log")

        try:
            log_fh = open(output_log, "w", encoding="utf-8")
            # Subprocess von der Konsole des Parents isolieren – siehe
            # scanner_starten() oben. Verhindert, dass Windows-Konsolen-Events
            # vom Subprocess den Flask-Dev-Server beenden.
            _creationflags = 0
            if os.name == 'nt':
                _creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW
                )
            proc   = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=log_fh,
                cwd=scanner_dir,
                creationflags=_creationflags,
            )
            _teams_scan_state["pid"]     = proc.pid
            _teams_scan_state["started"] = datetime.now(timezone.utc).isoformat()

            def _watch():
                proc.wait()
                log_fh.close()
                with _teams_scan_lock:
                    if _teams_scan_state.get("pid") == proc.pid:
                        _teams_scan_state["pid"]     = None
                        _teams_scan_state["started"] = None

            threading.Thread(target=_watch, daemon=True).start()
            return jsonify({"ok": True, "msg": f"Teams-Scan gestartet (PID {proc.pid}).", "pid": proc.pid})
        except Exception as exc:
            return jsonify({"ok": False, "msg": str(exc)})


@bp.route("/teams/status")
@login_required
def teams_scan_status():
    running = _teams_scan_is_running()
    return jsonify({
        "running": running,
        "started": _teams_scan_state.get("started"),
    })


def _hash_pw(pw: str) -> str:
    """Wrapper auf den modernen Passwort-Hash (VULN-001 Remediation).

    Leitet an ``webapp.routes.auth._hash_pw`` weiter, das werkzeug-Hashes
    (pbkdf2:sha256 mit Salt) erzeugt. Siehe dort für Details zur
    Rehash-on-Login-Migration bestehender SHA-256-Hashes.
    """
    from .auth import _hash_pw as _modern_hash
    return _modern_hash(pw)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_hyperlink_url(cell: str) -> str:
    """Extrahiert die URL aus einer Excel-HYPERLINK-Formel, z.B.
    =HYPERLINK("https://...") → https://...
    Falls kein HYPERLINK-Muster erkannt wird, wird der Rohwert zurückgegeben."""
    m = re.search(r'HYPERLINK\("([^"]+)"', cell)
    return m.group(1) if m else cell.strip()


# ── Übersicht ──────────────────────────────────────────────────────────────

_KLASSIFIZIERUNGS_BEREICHE = [
    ("idv_typ",               "IDV-Typ"),
    ("pruefintervall_monate", "Prüfintervall (Monate)"),
    ("nutzungsfrequenz",      "Nutzungsfrequenz"),
    ("pruefungsart",          "Prüfungsart"),
    ("pruefungs_ergebnis",    "Prüfungsergebnis"),
    ("massnahmentyp",         "Maßnahmentyp"),
    ("massnahmen_prioritaet", "Maßnahmen-Priorität"),
    ("gda_stufen",            "GDA-Stufen (Bezeichnung & Beschreibung)"),
]


@bp.route("/")
@login_required
def index():
    db = get_db()
    org_units          = db.execute("SELECT * FROM org_units ORDER BY bezeichnung").fetchall()
    geschaeftsprozesse = db.execute("SELECT * FROM geschaeftsprozesse ORDER BY gp_nummer").fetchall()
    plattformen        = db.execute("SELECT * FROM plattformen ORDER BY bezeichnung").fetchall()

    # Klassifizierungen gruppiert nach Bereich
    klassifizierungen = {}
    for bereich, _ in _KLASSIFIZIERUNGS_BEREICHE:
        klassifizierungen[bereich] = db.execute("""
            SELECT * FROM klassifizierungen WHERE bereich=? ORDER BY sort_order, wert
        """, (bereich,)).fetchall()

    # Konfigurierbare Wesentlichkeitskriterien (alle, inkl. inaktiv)
    wesentlichkeitskriterien = db.execute("""
        SELECT * FROM wesentlichkeitskriterien ORDER BY sort_order, id
    """).fetchall()

    return render_template("admin/index.html",
        org_units=org_units,
        geschaeftsprozesse=geschaeftsprozesse, plattformen=plattformen,
        klassifizierungen=klassifizierungen,
        klassifizierungs_bereiche=_KLASSIFIZIERUNGS_BEREICHE,
        wesentlichkeitskriterien=wesentlichkeitskriterien)


@bp.route("/mitarbeiter")
@login_required
def mitarbeiter():
    db = get_db()
    org_units = db.execute("SELECT * FROM org_units ORDER BY bezeichnung").fetchall()
    persons   = db.execute("""
        SELECT p.*, o.bezeichnung AS org
        FROM persons p LEFT JOIN org_units o ON p.org_unit_id=o.id
        ORDER BY p.nachname
    """).fetchall()
    return render_template("admin/mitarbeiter.html",
        org_units=org_units,
        persons=persons)


@bp.route("/mail", methods=["GET", "POST"])
@admin_required
def mail():
    db = get_db()
    if request.method == "POST":
        # VULN-007: SMTP-Passwort gesondert behandeln (Fernet-Verschlüsselung)
        from ..email_service import EMAIL_TEMPLATES, encrypt_smtp_password
        _save_smtp_password(db, request.form.get("smtp_password", ""),
                            encrypt_smtp_password)

        keys = ["smtp_host", "smtp_port", "smtp_user",
                "smtp_from", "smtp_tls", "app_base_url"]
        for tpl_key in EMAIL_TEMPLATES:
            keys.append(f"notify_enabled_{tpl_key}")
            keys.append(f"email_tpl_{tpl_key}_subject")
            keys.append(f"email_tpl_{tpl_key}_body")
        for k in keys:
            val = request.form.get(k, "")
            db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)", (k, val))
        db.commit()
        flash("Einstellungen gespeichert.", "success")
        return redirect(url_for("admin.mail") + "#email-vorlagen")

    settings = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM app_settings").fetchall()}
    from ..email_service import EMAIL_TEMPLATES as _email_tpls, _DEFAULTS as _email_defaults
    return render_template("admin/mail.html",
        settings=settings,
        email_templates=_email_tpls,
        email_defaults=_email_defaults)


# ── Personen ───────────────────────────────────────────────────────────────

@bp.route("/person/neu", methods=["POST"])
@login_required
def new_person():
    db = get_db()
    db.execute("""
        INSERT INTO persons (kuerzel, nachname, vorname, email, rolle, org_unit_id,
                             user_id, ad_name, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        request.form.get("kuerzel", "").strip().upper(),
        request.form.get("nachname", "").strip(),
        request.form.get("vorname", "").strip(),
        request.form.get("email") or None,
        request.form.get("rolle") or None,
        request.form.get("org_unit_id") or None,
        request.form.get("user_id") or None,
        request.form.get("ad_name") or None,
        _now()
    ))
    db.commit()
    flash("Person angelegt.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/person/<int:pid>/bearbeiten", methods=["GET", "POST"])
@login_required
def edit_person(pid):
    db = get_db()
    person = db.execute("SELECT * FROM persons WHERE id = ?", (pid,)).fetchone()
    if not person:
        flash("Person nicht gefunden.", "error")
        return redirect(url_for("admin.index"))

    org_units = db.execute("SELECT * FROM org_units ORDER BY bezeichnung").fetchall()

    if request.method == "POST":
        new_pw = request.form.get("password", "").strip()
        pw_hash = _hash_pw(new_pw) if new_pw else person["password_hash"]

        db.execute("""
            UPDATE persons SET
                kuerzel=?, nachname=?, vorname=?, email=?, rolle=?,
                org_unit_id=?, user_id=?, ad_name=?, password_hash=?, aktiv=?
            WHERE id=?
        """, (
            request.form.get("kuerzel", "").strip().upper(),
            request.form.get("nachname", "").strip(),
            request.form.get("vorname", "").strip(),
            request.form.get("email") or None,
            request.form.get("rolle") or None,
            request.form.get("org_unit_id") or None,
            request.form.get("user_id") or None,
            request.form.get("ad_name") or None,
            pw_hash,
            1 if request.form.get("aktiv") else 0,
            pid
        ))
        db.commit()
        flash("Person gespeichert.", "success")
        return redirect(url_for("admin.index"))

    return render_template("admin/person_edit.html", person=person, org_units=org_units)


@bp.route("/person/<int:pid>/loeschen", methods=["POST"])
@admin_required
def delete_person(pid):
    db = get_db()
    db.execute("UPDATE persons SET aktiv=0 WHERE id=?", (pid,))
    db.commit()
    flash("Person deaktiviert.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/person/<int:pid>/endgueltig-loeschen", methods=["POST"])
@admin_required
def delete_person_hard(pid):
    db = get_db()
    try:
        db.execute("DELETE FROM persons WHERE id=?", (pid,))
        db.commit()
        flash("Person gelöscht.", "success")
    except Exception:
        db.rollback()
        flash("Person konnte nicht gelöscht werden (noch IDVs zugeordnet) – bitte zuerst deaktivieren.", "warning")
    return redirect(url_for("admin.index"))


@bp.route("/personen/bulk", methods=["POST"])
@admin_required
def bulk_persons():
    """Bulk-Aktion auf mehrere Personen: deactivate oder delete."""
    db     = get_db()
    action = request.form.get("action", "")
    raw    = request.form.getlist("person_ids")
    ids    = [int(i) for i in raw if i.isdigit()]

    if not ids:
        flash("Keine Personen ausgewählt.", "warning")
        return redirect(url_for("admin.index"))

    if action == "deactivate":
        ph = ",".join("?" * len(ids))
        db.execute(f"UPDATE persons SET aktiv=0 WHERE id IN ({ph})", ids)
        db.commit()
        flash(f"{len(ids)} Person(en) deaktiviert.", "success")

    elif action == "delete":
        deleted = skipped = 0
        for pid in ids:
            try:
                db.execute("DELETE FROM persons WHERE id=?", (pid,))
                db.commit()
                deleted += 1
            except Exception:
                db.rollback()
                skipped += 1
        msg = f"{deleted} Person(en) gelöscht."
        if skipped:
            msg += f" {skipped} konnte(n) nicht gelöscht werden (noch IDVs zugeordnet) → bitte zuerst deaktivieren."
        flash(msg, "success" if not skipped else "warning")

    else:
        flash("Unbekannte Aktion.", "error")

    return redirect(url_for("admin.index"))


# ── Organisationseinheiten ─────────────────────────────────────────────────

@bp.route("/oe/neu", methods=["POST"])
@login_required
def new_oe():
    db = get_db()
    db.execute("""
        INSERT INTO org_units (kuerzel, bezeichnung, ebene, parent_id, created_at)
        VALUES (?,?,?,?,?)
    """, (
        request.form.get("kuerzel", "").strip().upper(),
        request.form.get("bezeichnung", "").strip(),
        request.form.get("ebene") or None,
        request.form.get("parent_id") or None,
        _now()
    ))
    db.commit()
    flash("Organisationseinheit angelegt.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/oe/<int:oid>/bearbeiten", methods=["GET", "POST"])
@login_required
def edit_oe(oid):
    db = get_db()
    oe = db.execute("SELECT * FROM org_units WHERE id=?", (oid,)).fetchone()
    if not oe:
        flash("OE nicht gefunden.", "error")
        return redirect(url_for("admin.index"))

    all_oe = db.execute("SELECT * FROM org_units WHERE id!=? ORDER BY bezeichnung", (oid,)).fetchall()

    if request.method == "POST":
        db.execute("""
            UPDATE org_units SET kuerzel=?, bezeichnung=?, ebene=?, parent_id=?, aktiv=?
            WHERE id=?
        """, (
            request.form.get("kuerzel", "").strip().upper(),
            request.form.get("bezeichnung", "").strip(),
            request.form.get("ebene") or None,
            request.form.get("parent_id") or None,
            1 if request.form.get("aktiv") else 0,
            oid
        ))
        db.commit()
        flash("Organisationseinheit gespeichert.", "success")
        return redirect(url_for("admin.index"))

    return render_template("admin/oe_edit.html", oe=oe, all_oe=all_oe)


@bp.route("/oe/<int:oid>/loeschen", methods=["POST"])
@admin_required
def delete_oe(oid):
    db = get_db()
    db.execute("UPDATE org_units SET aktiv=0 WHERE id=?", (oid,))
    db.commit()
    flash("Organisationseinheit deaktiviert.", "success")
    return redirect(url_for("admin.index"))


# ── Geschäftsprozesse ──────────────────────────────────────────────────────

@bp.route("/gp/neu", methods=["POST"])
@login_required
def new_gp():
    db = get_db()
    now = _now()
    db.execute("""
        INSERT INTO geschaeftsprozesse
          (gp_nummer, bezeichnung, bereich, ist_kritisch, ist_wesentlich, updated_at, created_at)
        VALUES (?,?,?,?,?,?,?)
    """, (
        request.form.get("gp_nummer", "").strip(),
        request.form.get("bezeichnung", "").strip(),
        request.form.get("bereich") or None,
        1 if request.form.get("ist_kritisch") else 0,
        1 if request.form.get("ist_wesentlich") else 0,
        now, now
    ))
    db.commit()
    flash("Geschäftsprozess angelegt.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/gp/<int:gid>/bearbeiten", methods=["GET", "POST"])
@login_required
def edit_gp(gid):
    db = get_db()
    gp = db.execute("SELECT * FROM geschaeftsprozesse WHERE id=?", (gid,)).fetchone()
    if not gp:
        flash("Geschäftsprozess nicht gefunden.", "error")
        return redirect(url_for("admin.index"))

    if request.method == "POST":
        db.execute("""
            UPDATE geschaeftsprozesse SET
                gp_nummer=?, bezeichnung=?, ist_kritisch=?, ist_wesentlich=?,
                beschreibung=?,
                schutzbedarf_a=?, schutzbedarf_c=?, schutzbedarf_i=?, schutzbedarf_n=?,
                aktiv=?, updated_at=?
            WHERE id=?
        """, (
            request.form.get("gp_nummer", "").strip(),
            request.form.get("bezeichnung", "").strip(),
            1 if request.form.get("ist_kritisch") else 0,
            1 if request.form.get("ist_wesentlich") else 0,
            request.form.get("beschreibung") or None,
            request.form.get("schutzbedarf_a") or None,
            request.form.get("schutzbedarf_c") or None,
            request.form.get("schutzbedarf_i") or None,
            request.form.get("schutzbedarf_n") or None,
            1 if request.form.get("aktiv") else 0,
            _now(), gid
        ))
        db.commit()
        flash("Geschäftsprozess gespeichert.", "success")
        return redirect(url_for("admin.index"))

    org_units = db.execute("SELECT * FROM org_units ORDER BY bezeichnung").fetchall()
    return render_template("admin/gp_edit.html", gp=gp, org_units=org_units)


@bp.route("/gp/<int:gid>/loeschen", methods=["POST"])
@admin_required
def delete_gp(gid):
    db = get_db()
    db.execute("UPDATE geschaeftsprozesse SET aktiv=0 WHERE id=?", (gid,))
    db.commit()
    flash("Geschäftsprozess deaktiviert.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/gp/alle-loeschen", methods=["POST"])
@admin_required
def delete_all_gp():
    """Löscht alle Geschäftsprozesse unwiderruflich.
    Verknüpfungen in idv_register.gp_id werden dabei auf NULL gesetzt."""
    db = get_db()
    db.execute("UPDATE idv_register SET gp_id=NULL WHERE gp_id IS NOT NULL")
    db.execute("DELETE FROM geschaeftsprozesse")
    db.commit()
    flash("Alle Geschäftsprozesse wurden gelöscht.", "success")
    return redirect(url_for("admin.index") + "#geschaeftsprozesse")


@bp.route("/gps/bulk", methods=["POST"])
@admin_required
def bulk_gps():
    """Bulk-Aktion auf mehrere Geschäftsprozesse: deactivate oder delete."""
    db     = get_db()
    action = request.form.get("action", "")
    raw    = request.form.getlist("gp_ids")
    ids    = [int(i) for i in raw if i.isdigit()]

    if not ids:
        flash("Keine Geschäftsprozesse ausgewählt.", "warning")
        return redirect(url_for("admin.index") + "#geschaeftsprozesse")

    if action == "deactivate":
        ph = ",".join("?" * len(ids))
        db.execute(f"UPDATE geschaeftsprozesse SET aktiv=0 WHERE id IN ({ph})", ids)
        db.commit()
        flash(f"{len(ids)} Geschäftsprozess(e) deaktiviert.", "success")

    elif action == "delete":
        deleted = skipped = 0
        for gid in ids:
            try:
                db.execute("UPDATE idv_register SET gp_id=NULL WHERE gp_id=?", (gid,))
                db.execute("DELETE FROM geschaeftsprozesse WHERE id=?", (gid,))
                db.commit()
                deleted += 1
            except Exception:
                db.rollback()
                skipped += 1
        msg = f"{deleted} Geschäftsprozess(e) gelöscht."
        if skipped:
            msg += f" {skipped} konnte(n) nicht gelöscht werden."
        flash(msg, "success" if not skipped else "warning")

    else:
        flash("Unbekannte Aktion.", "error")

    return redirect(url_for("admin.index") + "#geschaeftsprozesse")


# ── Plattformen ────────────────────────────────────────────────────────────

@bp.route("/plattform/neu", methods=["POST"])
@login_required
def new_plattform():
    db = get_db()
    db.execute("""
        INSERT INTO plattformen (bezeichnung, typ, hersteller)
        VALUES (?,?,?)
    """, (
        request.form.get("bezeichnung", "").strip(),
        request.form.get("typ") or None,
        request.form.get("hersteller") or None,
    ))
    db.commit()
    flash("Plattform angelegt.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/plattform/<int:plid>/bearbeiten", methods=["GET", "POST"])
@login_required
def edit_plattform(plid):
    db = get_db()
    pl = db.execute("SELECT * FROM plattformen WHERE id=?", (plid,)).fetchone()
    if not pl:
        flash("Plattform nicht gefunden.", "error")
        return redirect(url_for("admin.index"))

    if request.method == "POST":
        db.execute("""
            UPDATE plattformen SET bezeichnung=?, typ=?, hersteller=?, aktiv=?
            WHERE id=?
        """, (
            request.form.get("bezeichnung", "").strip(),
            request.form.get("typ") or None,
            request.form.get("hersteller") or None,
            1 if request.form.get("aktiv") else 0,
            plid
        ))
        db.commit()
        flash("Plattform gespeichert.", "success")
        return redirect(url_for("admin.index"))

    return render_template("admin/plattform_edit.html", pl=pl)


@bp.route("/plattform/<int:plid>/loeschen", methods=["POST"])
@admin_required
def delete_plattform(plid):
    db = get_db()
    db.execute("UPDATE plattformen SET aktiv=0 WHERE id=?", (plid,))
    db.commit()
    flash("Plattform deaktiviert.", "success")
    return redirect(url_for("admin.index"))


# ── App-Einstellungen (SMTP etc.) ──────────────────────────────────────────

@bp.route("/einstellungen", methods=["POST"])
@admin_required
def save_settings():
    db = get_db()
    # VULN-007: SMTP-Passwort gesondert behandeln (Fernet-Verschlüsselung)
    from ..email_service import EMAIL_TEMPLATES, encrypt_smtp_password
    _save_smtp_password(db, request.form.get("smtp_password", ""),
                        encrypt_smtp_password)

    keys = ["smtp_host", "smtp_port", "smtp_user",
            "smtp_from", "smtp_tls", "local_login_enabled",
            "app_base_url"]
    # Dynamisch alle E-Mail-Template-Keys aufnehmen
    for tpl_key in EMAIL_TEMPLATES:
        keys.append(f"notify_enabled_{tpl_key}")
        keys.append(f"email_tpl_{tpl_key}_subject")
        keys.append(f"email_tpl_{tpl_key}_body")
    for k in keys:
        val = request.form.get(k, "")
        db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)", (k, val))
    db.commit()
    flash("Einstellungen gespeichert.", "success")
    return redirect(url_for("admin.mail") + "#email-vorlagen")


def _save_smtp_password(db, submitted: str, encrypt_fn) -> None:
    """Speichert das SMTP-Passwort verschlüsselt.

    - Leerer Wert bedeutet "Passwort beibehalten" (z. B. wenn der Admin nur
      andere Felder bearbeitet hat). Das Feld im Formular ist bewusst leer,
      um das Klartext-Passwort nicht ins HTML zu schreiben.
    - Nicht-leerer Wert wird Fernet-verschlüsselt und mit "enc:"-Präfix
      abgelegt.
    """
    if not submitted:
        return  # Altbestand nicht überschreiben
    try:
        enc = encrypt_fn(submitted)
    except Exception:
        # Im Ausnahmefall lieber nicht speichern als Klartext abzulegen
        flash("SMTP-Passwort konnte nicht verschlüsselt werden – nicht gespeichert.",
              "error")
        return
    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
        ("smtp_password", enc),
    )


# ── Login-Log ─────────────────────────────────────────────────────────────

@bp.route("/login-log")
@admin_required
def login_log():
    """Zeigt die letzten Login-Versuche aus instance/login.log."""
    import os
    from ..login_logger import get_log_path
    log_path = get_log_path()
    lines = []
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        lines = lines[-1000:]
        lines.reverse()
    except FileNotFoundError:
        pass
    except Exception as e:
        flash(f"Log konnte nicht gelesen werden: {e}", "error")

    # Logrotate-Konfiguration mit realem Pfad generieren
    logrotate_conf = f"""{log_path} {{
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}}

{os.path.join(os.path.dirname(log_path), 'idvault.log')} {{
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}}"""

    return render_template("admin/login_log.html",
                           lines=lines,
                           log_path=log_path,
                           logrotate_conf=logrotate_conf)


# ── Mitarbeiter-Import ─────────────────────────────────────────────────────

@bp.route("/import/personen", methods=["POST"])
@admin_required
def import_persons():
    """CSV-Import: user_id, email (SMTP-Adresse), ad_name, oe_kuerzel,
       nachname, vorname, kuerzel, rolle  (Trennzeichen ; oder ,)"""
    f = request.files.get("csv_file")
    if not f or not f.filename:
        flash("Keine Datei ausgewählt.", "error")
        return redirect(url_for("admin.index"))

    db      = get_db()
    content = f.read().decode("utf-8-sig")  # BOM-sicher
    dialect = "excel" if "," in content.split("\n")[0] else "excel-tab"
    # Erkenne Semikolon als Trenner
    first_line = content.split("\n")[0]
    delimiter  = ";" if first_line.count(";") >= first_line.count(",") else ","

    reader  = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    created = updated = errors = 0
    now     = _now()

    for row in reader:
        try:
            # Spalten-Aliase normalisieren (case-insensitive)
            r = {k.strip().lower(): (v or "").strip() for k, v in row.items()}

            user_id  = r.get("user_id") or r.get("userid") or r.get("benutzername") or ""
            email    = r.get("email") or r.get("smtp") or r.get("smtp_adresse") or r.get("mailadresse") or ""
            ad_name  = r.get("ad_name") or r.get("adname") or r.get("ad") or ""
            oe_k     = (r.get("oe") or r.get("oe_kuerzel") or r.get("abteilung") or "").upper()
            nachname = r.get("nachname") or r.get("name") or ""
            vorname  = r.get("vorname") or ""
            kuerzel  = (r.get("kuerzel") or user_id[:3]).upper()
            rolle    = r.get("rolle") or "Fachverantwortlicher"

            if not (nachname or user_id):
                errors += 1
                continue

            # OE auflösen
            org_unit_id = None
            if oe_k:
                oe_row = db.execute("SELECT id FROM org_units WHERE kuerzel=?", (oe_k,)).fetchone()
                if oe_row:
                    org_unit_id = oe_row["id"]

            # Prüfen ob user_id schon existiert
            existing = None
            if user_id:
                existing = db.execute("SELECT id FROM persons WHERE user_id=?", (user_id,)).fetchone()
            if not existing and kuerzel:
                existing = db.execute("SELECT id FROM persons WHERE kuerzel=?", (kuerzel,)).fetchone()

            if existing:
                db.execute("""
                    UPDATE persons SET
                        email=COALESCE(NULLIF(?,''), email),
                        ad_name=COALESCE(NULLIF(?,''), ad_name),
                        org_unit_id=COALESCE(?,org_unit_id),
                        user_id=COALESCE(NULLIF(?,''), user_id),
                        rolle=COALESCE(NULLIF(?,''), rolle)
                    WHERE id=?
                """, (email, ad_name, org_unit_id, user_id, rolle, existing["id"]))
                updated += 1
            else:
                db.execute("""
                    INSERT INTO persons
                        (kuerzel, nachname, vorname, email, rolle, org_unit_id,
                         user_id, ad_name, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (kuerzel, nachname, vorname, email or None, rolle,
                      org_unit_id, user_id or None, ad_name or None, now))
                created += 1
        except Exception as exc:
            errors += 1

    db.commit()
    flash(f"Import abgeschlossen: {created} neu, {updated} aktualisiert, {errors} Fehler.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/import/vorlage")
@login_required
def import_template():
    """CSV-Vorlage herunterladen."""
    content = "user_id;email;ad_name;oe_kuerzel;nachname;vorname;kuerzel;rolle\n"
    content += "mmu;max.mustermann@bank.de;DOMAIN\\mmu;KRE;Mustermann;Max;MMU;Fachverantwortlicher\n"
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=mitarbeiter_vorlage.csv"}
    )


# ── Klassifizierungen ──────────────────────────────────────────────────────

@bp.route("/klassifizierungen/<bereich>/neu", methods=["POST"])
@login_required
def new_klassifizierung(bereich):
    db  = get_db()
    wert = request.form.get("wert", "").strip()
    if not wert:
        flash("Wert darf nicht leer sein.", "error")
        return redirect(url_for("admin.index") + f"#klass-{bereich}")

    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order),0) FROM klassifizierungen WHERE bereich=?", (bereich,)
    ).fetchone()[0]

    db.execute("""
        INSERT INTO klassifizierungen (bereich, wert, bezeichnung, beschreibung, sort_order, aktiv)
        VALUES (?,?,?,?,?,1)
        ON CONFLICT(bereich, wert) DO UPDATE SET
            bezeichnung=excluded.bezeichnung,
            beschreibung=excluded.beschreibung,
            aktiv=1
    """, (
        bereich,
        wert,
        request.form.get("bezeichnung") or None,
        request.form.get("beschreibung") or None,
        max_order + 1
    ))
    db.commit()
    flash(f"Eintrag '{wert}' in '{bereich}' angelegt.", "success")
    return redirect(url_for("admin.index") + f"#klass-{bereich}")


@bp.route("/klassifizierungen/<int:kid>/bearbeiten", methods=["GET", "POST"])
@login_required
def edit_klassifizierung(kid):
    db  = get_db()
    row = db.execute("SELECT * FROM klassifizierungen WHERE id=?", (kid,)).fetchone()
    if not row:
        flash("Eintrag nicht gefunden.", "error")
        return redirect(url_for("admin.index"))

    if request.method == "POST":
        db.execute("""
            UPDATE klassifizierungen
            SET wert=?, bezeichnung=?, beschreibung=?, sort_order=?, aktiv=?
            WHERE id=?
        """, (
            request.form.get("wert", "").strip(),
            request.form.get("bezeichnung") or None,
            request.form.get("beschreibung") or None,
            int(request.form.get("sort_order", row["sort_order"])),
            1 if request.form.get("aktiv") else 0,
            kid
        ))
        db.commit()
        flash("Eintrag gespeichert.", "success")
        return redirect(url_for("admin.index") + f"#klass-{row['bereich']}")

    bereich_label = dict(_KLASSIFIZIERUNGS_BEREICHE).get(row["bereich"], row["bereich"])
    return render_template("admin/klassifizierung_edit.html",
                           row=row, bereich_label=bereich_label)


@bp.route("/klassifizierungen/<int:kid>/loeschen", methods=["POST"])
@admin_required
def delete_klassifizierung(kid):
    db  = get_db()
    row = db.execute("SELECT bereich FROM klassifizierungen WHERE id=?", (kid,)).fetchone()
    db.execute("UPDATE klassifizierungen SET aktiv=0 WHERE id=?", (kid,))
    db.commit()
    flash("Eintrag deaktiviert.", "success")
    bereich = row["bereich"] if row else ""
    return redirect(url_for("admin.index") + f"#klass-{bereich}")


# ── Wesentlichkeitskriterien ───────────────────────────────────────────────

@bp.route("/wesentlichkeit/neu", methods=["POST"])
@admin_required
def new_wesentlichkeitskriterium():
    db = get_db()
    bezeichnung = request.form.get("bezeichnung", "").strip()
    if not bezeichnung:
        flash("Bezeichnung darf nicht leer sein.", "error")
        return redirect(url_for("admin.index") + "#wesentlichkeit")

    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), 0) FROM wesentlichkeitskriterien"
    ).fetchone()[0]

    db.execute("""
        INSERT INTO wesentlichkeitskriterien
            (bezeichnung, beschreibung, begruendung_pflicht, sort_order, aktiv)
        VALUES (?, ?, ?, ?, 1)
    """, (
        bezeichnung,
        request.form.get("beschreibung") or None,
        1 if request.form.get("begruendung_pflicht") else 0,
        max_order + 1,
    ))
    db.commit()
    flash(f"Kriterium '{bezeichnung}' angelegt.", "success")
    return redirect(url_for("admin.index") + "#wesentlichkeit")


@bp.route("/wesentlichkeit/<int:kid>/bearbeiten", methods=["GET", "POST"])
@admin_required
def edit_wesentlichkeitskriterium(kid):
    db  = get_db()
    row = db.execute("SELECT * FROM wesentlichkeitskriterien WHERE id=?", (kid,)).fetchone()
    if not row:
        flash("Kriterium nicht gefunden.", "error")
        return redirect(url_for("admin.index") + "#wesentlichkeit")

    if request.method == "POST":
        db.execute("""
            UPDATE wesentlichkeitskriterien
            SET bezeichnung=?, beschreibung=?, begruendung_pflicht=?, sort_order=?, aktiv=?
            WHERE id=?
        """, (
            request.form.get("bezeichnung", "").strip(),
            request.form.get("beschreibung") or None,
            1 if request.form.get("begruendung_pflicht") else 0,
            int(request.form.get("sort_order", row["sort_order"])),
            1 if request.form.get("aktiv") else 0,
            kid,
        ))
        db.commit()
        flash("Kriterium gespeichert.", "success")
        return redirect(url_for("admin.index") + "#wesentlichkeit")

    return render_template("admin/wesentlichkeit_edit.html", row=row)


@bp.route("/wesentlichkeit/<int:kid>/loeschen", methods=["POST"])
@admin_required
def delete_wesentlichkeitskriterium(kid):
    db = get_db()
    db.execute("UPDATE wesentlichkeitskriterien SET aktiv=0 WHERE id=?", (kid,))
    db.commit()
    flash("Kriterium deaktiviert. Vorhandene Antworten bleiben erhalten.", "success")
    return redirect(url_for("admin.index") + "#wesentlichkeit")


# ── Geschäftsprozess-Import ────────────────────────────────────────────────

@bp.route("/import/geschaeftsprozesse/vorlage")
@login_required
def import_gp_template():
    """CSV-Vorlage für GP-Import herunterladen."""
    content  = "gp_nummer;bezeichnung;bereich;ist_kritisch;ist_wesentlich;beschreibung\n"
    content += "GP-XXX-001;Mein Prozess;Steuerung;1;1;Kurzbeschreibung\n"
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=geschaeftsprozesse_vorlage.csv"}
    )


@bp.route("/import/geschaeftsprozesse", methods=["POST"])
@admin_required
def import_geschaeftsprozesse():
    """
    CSV-Import für Geschäftsprozesse – zwei Formate werden unterstützt:

    Prozess-Export-Format (Spalten):
        Nummer; Prozess_ID; Prozess_Titel; Beschreibung; Ebene; Zustand; Herkunft;
        Version; Prozesswesentlichkeit; Zeitkritikalitaet; Schutzbedarf_A;
        Schutzbedarf_C; Schutzbedarf_I; Schutzbedarf_N; Kritisch_Wichtig;
        Begründung_Kritisch_Wichtig; RTO; RPO; Auswirkung_Unterbrechung;
        Vorgaenger; Nummer_Bestandsprozess; Kommentare
    Upsert-Schlüssel: Prozess_ID → gp_nummer

    Standard-Format (Spalten):
        gp_nummer; bezeichnung; bereich; ist_kritisch (0/1); ist_wesentlich (0/1);
        beschreibung
    Upsert-Schlüssel: gp_nummer
    """
    f = request.files.get("csv_file")
    if not f or not f.filename:
        flash("Keine Datei ausgewählt.", "error")
        return redirect(url_for("admin.index") + "#geschaeftsprozesse")

    db      = get_db()
    content = f.read().decode("utf-8-sig")
    first_line = content.split("\n")[0]
    delimiter  = ";" if first_line.count(";") >= first_line.count(",") else ","
    reader     = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    created = updated = errors = 0
    now     = _now()

    # Format-Erkennung anhand der Header-Zeile
    raw_fields   = [k.strip() for k in (reader.fieldnames or []) if k and k.strip()]
    fields_lower = [f.lower() for f in raw_fields]
    is_prozess_export = "prozess_id" in fields_lower

    if is_prozess_export:
        # ── Prozess-Export-Format ────────────────────────────────────────────
        for row in reader:
            try:
                r = {k.strip(): (v or "").strip() for k, v in row.items() if k and k.strip()}

                gp_nummer   = r.get("Prozess_ID", "").strip()
                bezeichnung = r.get("Prozess_Titel", "").strip()
                if not gp_nummer or not bezeichnung:
                    errors += 1
                    continue

                beschreibung   = r.get("Beschreibung") or None
                wesentl_raw    = r.get("Prozesswesentlichkeit", "").strip().lower()
                ist_wesentlich = 1 if wesentl_raw == "wesentlich" else 0
                kritisch_raw   = r.get("Kritisch_Wichtig", "Nein").strip().lower()
                ist_kritisch   = 1 if kritisch_raw == "ja" else 0
                sb_a = r.get("Schutzbedarf_A") or None
                sb_c = r.get("Schutzbedarf_C") or None
                sb_i = r.get("Schutzbedarf_I") or None
                sb_n = r.get("Schutzbedarf_N") or None

                existing = db.execute(
                    "SELECT id FROM geschaeftsprozesse WHERE gp_nummer=?", (gp_nummer,)
                ).fetchone()

                if existing:
                    db.execute("""
                        UPDATE geschaeftsprozesse
                        SET bezeichnung=?,
                            beschreibung=COALESCE(?,beschreibung),
                            ist_kritisch=?,
                            ist_wesentlich=?,
                            schutzbedarf_a=COALESCE(?,schutzbedarf_a),
                            schutzbedarf_c=COALESCE(?,schutzbedarf_c),
                            schutzbedarf_i=COALESCE(?,schutzbedarf_i),
                            schutzbedarf_n=COALESCE(?,schutzbedarf_n),
                            aktiv=1,
                            updated_at=?
                        WHERE gp_nummer=?
                    """, (bezeichnung, beschreibung,
                          ist_kritisch, ist_wesentlich,
                          sb_a, sb_c, sb_i, sb_n,
                          now, gp_nummer))
                    updated += 1
                else:
                    db.execute("""
                        INSERT INTO geschaeftsprozesse
                            (gp_nummer, bezeichnung, beschreibung,
                             ist_kritisch, ist_wesentlich,
                             schutzbedarf_a, schutzbedarf_c, schutzbedarf_i, schutzbedarf_n,
                             created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """, (gp_nummer, bezeichnung, beschreibung,
                          ist_kritisch, ist_wesentlich,
                          sb_a, sb_c, sb_i, sb_n,
                          now, now))
                    created += 1
            except Exception:
                errors += 1

    else:
        # ── Standard-Format ──────────────────────────────────────────────────
        for row in reader:
            try:
                r = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
                gp_nummer   = r.get("gp_nummer", "").strip()
                bezeichnung = r.get("bezeichnung", "").strip()
                if not gp_nummer or not bezeichnung:
                    errors += 1
                    continue

                bereich        = r.get("bereich") or None
                ist_kritisch   = 1 if r.get("ist_kritisch", "0") in ("1", "ja", "true", "x") else 0
                ist_wesentlich = 1 if r.get("ist_wesentlich", "0") in ("1", "ja", "true", "x") else 0
                beschreibung   = r.get("beschreibung") or None

                existing = db.execute(
                    "SELECT id FROM geschaeftsprozesse WHERE gp_nummer=?", (gp_nummer,)
                ).fetchone()

                if existing:
                    db.execute("""
                        UPDATE geschaeftsprozesse
                        SET bezeichnung=?, bereich=COALESCE(?,bereich),
                            ist_kritisch=?, ist_wesentlich=?,
                            beschreibung=COALESCE(?,beschreibung),
                            aktiv=1, updated_at=?
                        WHERE gp_nummer=?
                    """, (bezeichnung, bereich, ist_kritisch, ist_wesentlich,
                          beschreibung, now, gp_nummer))
                    updated += 1
                else:
                    db.execute("""
                        INSERT INTO geschaeftsprozesse
                            (gp_nummer, bezeichnung, bereich, ist_kritisch, ist_wesentlich,
                             beschreibung, created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (gp_nummer, bezeichnung, bereich, ist_kritisch, ist_wesentlich,
                          beschreibung, now, now))
                    created += 1
            except Exception:
                errors += 1

    db.commit()
    flash(f"GP-Import: {created} neu, {updated} aktualisiert, {errors} Fehler.", "success")
    return redirect(url_for("admin.index") + "#geschaeftsprozesse")


# ══════════════════════════════════════════════════════════════════════════════
# Kompatibilitäts-Stubs für Routen, die in älteren EXE-Bundles referenziert
# werden, aber in dieser Version nicht implementiert sind. Verhindert
# BuildError in gebündelten Templates.
# ══════════════════════════════════════════════════════════════════════════════

_LDAP_ROLLEN = [
    "IDV-Administrator",
    "IDV-Koordinator",
    "Fachverantwortlicher",
    "IT-Sicherheit",
    "Revision",
]


@bp.route("/ldap-config", methods=["GET", "POST"])
@admin_required
def ldap_config():
    from ..ldap_auth import get_ldap_config, encrypt_password
    db = get_db()

    if request.method == "POST":
        enabled     = 1 if request.form.get("enabled") else 0
        server_url  = request.form.get("server_url", "").strip()
        port        = int(request.form.get("port") or 636)
        base_dn     = request.form.get("base_dn", "").strip()
        bind_dn     = request.form.get("bind_dn", "").strip()
        bind_password_plain = request.form.get("bind_password", "").strip()
        user_attr   = request.form.get("user_attr", "sAMAccountName")
        ssl_verify  = 1 if request.form.get("ssl_verify") else 0

        # Bestehendes Passwort beibehalten wenn Feld leer
        existing = get_ldap_config(db)
        if bind_password_plain:
            secret_key = current_app.config["SECRET_KEY"]
            bind_password_enc = encrypt_password(bind_password_plain, secret_key)
        else:
            bind_password_enc = existing["bind_password"] if existing else ""

        updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        db.execute("""
            INSERT INTO ldap_config
                (id, enabled, server_url, port, base_dn, bind_dn,
                 bind_password, user_attr, ssl_verify, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                enabled=excluded.enabled,
                server_url=excluded.server_url,
                port=excluded.port,
                base_dn=excluded.base_dn,
                bind_dn=excluded.bind_dn,
                bind_password=excluded.bind_password,
                user_attr=excluded.user_attr,
                ssl_verify=excluded.ssl_verify,
                updated_at=excluded.updated_at
        """, (enabled, server_url, port, base_dn, bind_dn,
              bind_password_enc, user_attr, ssl_verify, updated_at))
        db.commit()
        flash("LDAP-Konfiguration gespeichert.", "success")
        # VULN-012: TLS-Zertifikatsprüfung abgeschaltet → Audit-Warnung
        if enabled and not ssl_verify:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "LDAP-Konfiguration gespeichert MIT DEAKTIVIERTER "
                "Zertifikatsprüfung (ssl_verify=0) – Man-in-the-Middle-Angriffe "
                "auf LDAPS möglich."
            )
            flash(
                "Hinweis: Die Zertifikatsprüfung (ssl_verify) ist deaktiviert. "
                "Das macht LDAPS anfällig für Man-in-the-Middle-Angriffe. "
                "Für den Produktivbetrieb bitte aktivieren und das Server-"
                "Zertifikat aus der internen CA als vertrauenswürdig hinterlegen.",
                "warning",
            )
        return redirect(url_for("admin.ldap_config"))

    cfg = get_ldap_config(db)
    return render_template("admin/ldap_config.html", cfg=cfg)


@bp.route("/ldap-test", methods=["POST"])
@admin_required
def ldap_test():
    from ..ldap_auth import get_ldap_config, ldap_test_connection
    db = get_db()
    cfg = get_ldap_config(db)
    if not cfg or not cfg["server_url"]:
        return jsonify(ok=False, msg="Keine LDAP-Konfiguration gespeichert.")
    secret_key = current_app.config["SECRET_KEY"]
    ok, msg = ldap_test_connection(dict(cfg), secret_key)
    return jsonify(ok=ok, msg=msg)


@bp.route("/ldap-gruppen", methods=["GET"])
@admin_required
def ldap_gruppen():
    db = get_db()
    mappings = db.execute(
        "SELECT * FROM ldap_group_role_mapping ORDER BY sort_order, id"
    ).fetchall()
    return render_template("admin/ldap_gruppen.html",
                           mappings=mappings, rollen=_LDAP_ROLLEN)


@bp.route("/ldap-gruppe/neu", methods=["POST"])
@admin_required
def ldap_gruppe_neu():
    db = get_db()
    group_dn   = request.form.get("group_dn", "").strip()
    group_name = request.form.get("group_name", "").strip()
    rolle      = request.form.get("rolle", "").strip()
    sort_order = int(request.form.get("sort_order") or 99)

    if not group_dn or not rolle:
        flash("Gruppen-DN und Rolle sind Pflichtfelder.", "danger")
        return redirect(url_for("admin.ldap_gruppen"))

    try:
        db.execute("""
            INSERT INTO ldap_group_role_mapping (group_dn, group_name, rolle, sort_order)
            VALUES (?, ?, ?, ?)
        """, (group_dn, group_name or None, rolle, sort_order))
        db.commit()
        flash("Gruppen-Mapping angelegt.", "success")
    except Exception:
        flash("Fehler: Gruppen-DN ist bereits vorhanden.", "danger")
    return redirect(url_for("admin.ldap_gruppen"))


@bp.route("/ldap-gruppe/<int:mid>/bearbeiten", methods=["POST"])
@admin_required
def ldap_gruppe_bearbeiten(mid):
    db = get_db()
    group_dn   = request.form.get("group_dn", "").strip()
    group_name = request.form.get("group_name", "").strip()
    rolle      = request.form.get("rolle", "").strip()
    sort_order = int(request.form.get("sort_order") or 99)

    if not group_dn or not rolle:
        flash("Gruppen-DN und Rolle sind Pflichtfelder.", "danger")
        return redirect(url_for("admin.ldap_gruppen"))

    db.execute("""
        UPDATE ldap_group_role_mapping
        SET group_dn=?, group_name=?, rolle=?, sort_order=?
        WHERE id=?
    """, (group_dn, group_name or None, rolle, sort_order, mid))
    db.commit()
    flash("Gruppen-Mapping aktualisiert.", "success")
    return redirect(url_for("admin.ldap_gruppen"))


@bp.route("/ldap-gruppe/<int:mid>/loeschen", methods=["POST"])
@admin_required
def ldap_gruppe_loeschen(mid):
    db = get_db()
    db.execute("DELETE FROM ldap_group_role_mapping WHERE id=?", (mid,))
    db.commit()
    flash("Gruppen-Mapping gelöscht.", "success")
    return redirect(url_for("admin.ldap_gruppen"))


@bp.route("/ldap-import", methods=["GET", "POST"])
@admin_required
def ldap_import():
    from ..ldap_auth import get_ldap_config, ldap_list_users, ldap_sync_person
    db = get_db()
    cfg = get_ldap_config(db)
    secret_key = current_app.config["SECRET_KEY"]

    if request.method == "POST":
        action       = request.form.get("action", "import")
        selected_ids = request.form.getlist("user_ids")

        if not selected_ids:
            flash("Keine Benutzer ausgewählt.", "warning")
            return redirect(url_for("admin.ldap_import"))

        # ── Aktion: Löschen (Deaktivieren) ───────────────────────────────────
        if action == "delete":
            deactivated = skipped = 0
            for uid in selected_ids:
                row = db.execute(
                    "SELECT id FROM persons WHERE ad_name=? OR user_id=?", (uid, uid)
                ).fetchone()
                if row:
                    db.execute("UPDATE persons SET aktiv=0 WHERE id=?", (row["id"],))
                    deactivated += 1
                else:
                    skipped += 1
            db.commit()
            msg = f"{deactivated} Person(en) deaktiviert."
            if skipped:
                msg += f" {skipped} nicht gefunden (noch nicht importiert)."
            flash(msg, "success" if deactivated else "warning")
            return redirect(url_for("admin.ldap_import"))

        # ── Aktion: Importieren ───────────────────────────────────────────────
        extra_filter = request.form.get("extra_filter", "").strip()
        ok, msg, users = ldap_list_users(db, secret_key, extra_filter)
        if not ok:
            flash(f"LDAP-Fehler: {msg}", "danger")
            return redirect(url_for("admin.ldap_import"))

        selected_set = set(selected_ids)
        neu = geaendert = 0
        for u in users:
            if u["user_id"] not in selected_set:
                continue
            existing = db.execute(
                "SELECT id FROM persons WHERE ad_name=? OR user_id=?",
                (u["user_id"], u["user_id"])
            ).fetchone()
            ldap_sync_person(db, u)
            if existing:
                geaendert += 1
            else:
                neu += 1

        flash(f"Import abgeschlossen: {neu} neu angelegt, {geaendert} aktualisiert.", "success")
        return redirect(url_for("admin.ldap_import"))

    # GET: LDAP-Benutzer laden und Vorschau zeigen
    extra_filter = request.args.get("extra_filter", "").strip()
    if not cfg or not cfg["server_url"]:
        users = []
        ldap_msg = "LDAP nicht konfiguriert. Bitte zuerst die LDAP-Konfiguration einrichten."
        ldap_ok  = False
    else:
        ldap_ok, ldap_msg, users = ldap_list_users(db, secret_key, extra_filter)

    # Vorhandene user_ids für Markierung im UI
    existing_ids = {
        r["user_id"] for r in db.execute(
            "SELECT user_id FROM persons WHERE user_id IS NOT NULL AND user_id != ''"
        ).fetchall()
    }

    return render_template("admin/ldap_import.html",
                           cfg=cfg, users=users, ldap_ok=ldap_ok, ldap_msg=ldap_msg,
                           extra_filter=extra_filter, existing_ids=existing_ids,
                           rollen=_LDAP_ROLLEN)


# ══════════════════════════════════════════════════════════════════════════════
# Software-Update (Sidecar-Mechanismus)
# ══════════════════════════════════════════════════════════════════════════════

def _updates_dir() -> str:
    """Sidecar-Verzeichnis neben der .exe (bzw. neben run.py im Dev-Betrieb)."""
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), 'updates')
    return os.path.join(os.path.dirname(current_app.root_path), 'updates')


_ALLOWED_UPDATE_EXTS = {'.py', '.html', '.sql', '.json', '.css', '.js'}


def _validate_zip_member(name: str) -> bool:
    """Prüft, ob ein ZIP-Eintrag sicher extrahiert werden darf."""
    if os.path.isabs(name):
        return False
    parts = os.path.normpath(name).replace('\\', '/').split('/')
    if '..' in parts or '__pycache__' in parts:
        return False
    if not name.endswith('/'):
        ext = os.path.splitext(name)[1].lower()
        if ext not in _ALLOWED_UPDATE_EXTS:
            return False
    return True


@bp.route("/update")
@admin_required
def update_index():
    upd_dir = _updates_dir()
    version_info = None
    changelog = None
    version_file = os.path.join(upd_dir, 'version.json')
    if os.path.isfile(version_file):
        try:
            with open(version_file, encoding='utf-8') as f:
                version_info = json.load(f)
            changelog = version_info.get('changelog', [])
        except Exception:
            pass

    bundled_version = current_app.config.get('BUNDLED_VERSION', '0.1.0')
    active_version = (version_info or {}).get('version', bundled_version)
    update_active = os.path.isdir(upd_dir)

    return render_template(
        "admin/update.html",
        bundled_version=bundled_version,
        active_version=active_version,
        update_active=update_active,
        changelog=changelog,
        upd_dir=upd_dir,
    )


def _zip_strip_prefix(members: list[str]) -> str:
    """
    Erkennt automatisch ein gemeinsames Top-Level-Verzeichnis im ZIP
    (z.B. 'idvault-main/' bei GitHub-Repository-Downloads) und gibt
    es als zu entfernenden Präfix zurück. Gibt '' zurück falls keiner.
    """
    top_dirs = {m.split('/')[0] for m in members if '/' in m}
    if len(top_dirs) == 1:
        prefix = top_dirs.pop() + '/'
        # Nur als Präfix verwenden wenn alle Einträge darunter liegen
        if all(m.startswith(prefix) or m == prefix.rstrip('/') for m in members):
            return prefix
    return ''


def _zip_remap(rel: str) -> str:
    """
    Mappt Pfade aus dem ZIP auf die Sidecar-Verzeichnisstruktur.
    GitHub-ZIPs enthalten Templates unter webapp/templates/ —
    der ChoiceLoader erwartet sie jedoch direkt unter templates/.
    """
    if rel.startswith('webapp/templates/'):
        return rel[len('webapp/'):]   # → templates/...
    return rel


@bp.route("/update/upload", methods=["POST"])
@admin_required
def update_upload():
    f = request.files.get("update_zip")
    if not f or not f.filename:
        flash("Keine Datei ausgewählt.", "error")
        return redirect(url_for("admin.update_index"))

    if not f.filename.lower().endswith('.zip'):
        flash("Nur ZIP-Dateien sind erlaubt.", "error")
        return redirect(url_for("admin.update_index"))

    upd_dir = _updates_dir()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
            tmp_path = tmp.name
            f.save(tmp)

        with zipfile.ZipFile(tmp_path, 'r') as zf:
            members = zf.namelist()
            prefix = _zip_strip_prefix(members)

            os.makedirs(upd_dir, exist_ok=True)
            extracted = skipped = 0

            for member in members:
                # Top-Level-Präfix entfernen
                rel = member[len(prefix):] if prefix and member.startswith(prefix) else member

                # Verzeichniseinträge und leere Pfade überspringen
                if not rel or rel.endswith('/'):
                    continue

                # Sicherheitsprüfung: path-traversal und __pycache__
                parts = os.path.normpath(rel).replace('\\', '/').split('/')
                if '..' in parts or '__pycache__' in parts:
                    continue

                # Nur erlaubte Dateiendungen extrahieren, Rest still überspringen
                ext = os.path.splitext(rel)[1].lower()
                if ext not in _ALLOWED_UPDATE_EXTS:
                    skipped += 1
                    continue

                # __init__.py überspringen: Package-Inits müssen aus dem Bundle
                # stammen, sonst schlägt der Import von gebündelten C-Extensions
                # (z.B. unicodedata.pyd) im frozen EXE fehl.
                if os.path.basename(rel) == '__init__.py':
                    skipped += 1
                    continue

                # Pfad-Remapping (webapp/templates/ → templates/)
                rel = _zip_remap(rel)

                target = os.path.join(upd_dir, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, 'wb') as dst:
                    dst.write(src.read())
                extracted += 1

        if extracted == 0:
            flash("Keine verwertbaren Dateien im ZIP gefunden.", "warning")
        else:
            flash(
                f"Update eingespielt: {extracted} Dateien extrahiert"
                + (f", {skipped} übersprungen" if skipped else "")
                + ". Bitte App neu starten.",
                "success",
            )

    except zipfile.BadZipFile:
        flash("Ungültige ZIP-Datei.", "error")
    except Exception as exc:
        flash(f"Fehler beim Einspielen: {exc}", "error")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    return redirect(url_for("admin.update_index"))


def _trigger_restart():
    """Startet die EXE sauber neu (gemeinsame Logik für Update & Rollback).

    Schreibt eine Batch-Datei neben die EXE, die:
      1. PyInstaller-Umgebungsvariablen löscht (_MEIPASS2 etc.)
      2. PATH auf Windows-Systemstandard zurücksetzt
      3. ~3 Sekunden wartet, bis Port 5000 freigegeben ist
      4. Die EXE über "start" in einem neuen Konsolenfenster startet
      5. Sich selbst löscht
    cmd.exe läuft unsichtbar (CREATE_NO_WINDOW), die neue EXE-Instanz
    bekommt ein eigenes, sichtbares Konsolenfenster.
    """
    def _do():
        import time
        time.sleep(1.5)
        if getattr(sys, 'frozen', False):
            exe = sys.executable
            bat_path = os.path.join(os.path.dirname(exe), '_idvault_restart.bat')
            bat = (
                '@echo off\r\n'
                'set _MEIPASS2=\r\n'
                'set _PYI_ARCHIVE_FILE=\r\n'
                'set PATH=%SystemRoot%\\system32;%SystemRoot%;'
                '%SystemRoot%\\System32\\Wbem\r\n'
                'ping -n 4 127.0.0.1 > nul\r\n'
                'start "" "{exe}"\r\n'
                'del "%~f0"\r\n'
            ).format(exe=exe)
            with open(bat_path, 'w', encoding='ascii') as _f:
                _f.write(bat)
            subprocess.Popen(
                ['cmd.exe', '/c', bat_path],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            subprocess.Popen([sys.executable] + sys.argv)
        os._exit(0)

    threading.Thread(target=_do, daemon=True).start()


@bp.route("/update/restart", methods=["POST"])
@admin_required
def update_restart():
    """Startet den Prozess neu, damit Sidecar-Dateien wirksam werden."""
    _trigger_restart()
    return render_template("admin/update_restarting.html")


@bp.route("/update/rollback", methods=["POST"])
@admin_required
def update_rollback():
    """Benennt das updates/-Verzeichnis um und startet neu (gebündelte Version)."""
    upd_dir = _updates_dir()
    if not os.path.isdir(upd_dir):
        flash("Kein aktives Update vorhanden.", "info")
        return redirect(url_for("admin.update_index"))

    # os.rename ist auf Windows auch bei geöffneten Dateien (z.B. importierten
    # .pyc-Dateien) zuverlässig, während shutil.rmtree mit PermissionError
    # scheitern kann. Nach dem Rename gibt es kein updates/-Verzeichnis mehr;
    # die neue EXE-Instanz startet mit der gebündelten Version.
    bak_dir = upd_dir.rstrip(os.sep) + '.bak'
    try:
        if os.path.exists(bak_dir):
            shutil.rmtree(bak_dir, ignore_errors=True)
        os.rename(upd_dir, bak_dir)
    except Exception as exc:
        current_app.logger.exception("Rollback fehlgeschlagen")
        flash(f"Rollback fehlgeschlagen: {exc}", "error")
        return redirect(url_for("admin.update_index"))

    # Backup im Hintergrund löschen (nach dem Rename keine Locking-Probleme mehr)
    def _del_bak():
        import time
        time.sleep(2)
        shutil.rmtree(bak_dir, ignore_errors=True)
    threading.Thread(target=_del_bak, daemon=True).start()

    _trigger_restart()
    return render_template("admin/update_restarting.html")


@bp.route("/update/log")
@admin_required
def update_log():
    """Gibt die letzten 500 Zeilen des App-Logs als Plaintext zurück."""
    log_path = os.path.join(
        os.path.dirname(current_app.config['DATABASE']), 'logs', 'idvault.log'
    )
    if not os.path.isfile(log_path):
        return Response("Keine Log-Datei vorhanden.", mimetype='text/plain; charset=utf-8')
    try:
        with open(log_path, encoding='utf-8', errors='replace') as _f:
            lines = _f.readlines()
        content = "".join(lines[-500:])
    except Exception as exc:
        content = f"Fehler beim Lesen der Log-Datei: {exc}"
    return Response(content, mimetype='text/plain; charset=utf-8')
