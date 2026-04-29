"""Admin-Blueprint: Übersicht, App-Einstellungen, Update, Glossar.

Thematische Submodule: scanner, mail, sicherheit, ldap, stammdaten.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from typing import Optional

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    Response, current_app, send_file,
)

from .. import login_required, admin_required, get_db
from ...db_writer import get_writer
from ... import limiter

from db_write_tx import write_tx


bp = Blueprint("admin", __name__, url_prefix="/admin")


def _upload_rate_limit():
    """VULN-009: Rate-Limit für Admin-Uploads (ZIP, CSV). Wird zur
    Request-Zeit aus ``app_settings['upload_rate_limit']`` gelesen, damit
    Admin-Änderungen über die Web-UI ohne Neustart wirksam werden."""
    try:
        from .. import app_settings as _aps
        return _aps.get_upload_rate_limit(get_db())
    except Exception:
        return "10 per minute;60 per hour"


def _hash_pw(pw: str) -> str:
    """Wrapper auf den modernen Passwort-Hash (pbkdf2:sha256, siehe auth)."""
    from ..auth import _hash_pw as _modern_hash
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

    # Konfigurierbare Wesentlichkeitskriterien (alle, inkl. inaktiv) mit Details
    kriterien_rows = db.execute("""
        SELECT * FROM wesentlichkeitskriterien ORDER BY sort_order, id
    """).fetchall()
    wesentlichkeitskriterien = []
    for k in kriterien_rows:
        details = db.execute("""
            SELECT id, bezeichnung, sort_order, aktiv
            FROM wesentlichkeitskriterium_details
            WHERE kriterium_id = ?
            ORDER BY sort_order, id
        """, (k["id"],)).fetchall()
        verwendungen = db.execute(
            "SELECT COUNT(*) FROM idv_wesentlichkeit WHERE kriterium_id=?", (k["id"],)
        ).fetchone()[0]
        d = dict(k)
        d["details"] = [dict(r) for r in details]
        d["verwendungen"] = verwendungen
        wesentlichkeitskriterien.append(d)

    from ...app_settings import get_bool as _get_bool
    return render_template("admin/index.html",
        org_units=org_units,
        geschaeftsprozesse=geschaeftsprozesse, plattformen=plattformen,
        klassifizierungen=klassifizierungen,
        klassifizierungs_bereiche=_KLASSIFIZIERUNGS_BEREICHE,
        wesentlichkeitskriterien=wesentlichkeitskriterien,
        filter_panel_open_setting=_get_bool(db, "filter_panel_open", False))


@bp.route("/export/excel")
@admin_required
def export_excel():
    """Prüfer-Export: IDV-Register + Maßnahmen + Prüfungen + Nachweise als Excel."""
    from ...excel_export import register_excel_bytes
    payload = register_excel_bytes(get_db())
    fname = f"idv-register-{datetime.now().strftime('%Y%m%d-%H%M')}.xlsx"
    return send_file(
        io.BytesIO(payload),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=fname,
    )


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

    from flask import current_app
    from ... import config_store as _cs
    local_users = []
    raw_local = _cs.get_bootstrap("IDV_LOCAL_USERS", None)
    if isinstance(raw_local, list) and raw_local:
        for entry in raw_local:
            if not isinstance(entry, dict):
                continue
            username = str(entry.get("username") or "").strip()
            if not username or not (entry.get("password_hash") or entry.get("password")):
                continue
            local_users.append({
                "username": username,
                "name":     str(entry.get("name") or username),
                "role":     str(entry.get("role") or "–"),
                "active":   bool(entry.get("active", True)),
            })
    else:
        # Fallback: bereits verarbeitetes Dict aus app.config (nur aktive User)
        for username, info in (current_app.config.get("IDV_LOCAL_USERS") or {}).items():
            local_users.append({
                "username": username,
                "name":     str(info.get("name") or username),
                "role":     str(info.get("role") or "–"),
                "active":   True,
            })

    return render_template("admin/mitarbeiter.html",
        org_units=org_units,
        persons=persons,
        local_users=local_users)

# ── UI-Einstellungen ───────────────────────────────────────────────────────

@bp.route("/ui-einstellungen", methods=["POST"])
@admin_required
def save_ui_settings():
    from ...app_settings import set_bool
    db = get_db()
    set_bool(db, "filter_panel_open", "filter_panel_open" in request.form)
    flash("UI-Einstellungen gespeichert.", "success")
    return redirect(url_for("admin.index"))


# ── App-Einstellungen (SMTP etc.) ──────────────────────────────────────────

@bp.route("/einstellungen", methods=["POST"])
@admin_required
def save_settings():
    # VULN-007: SMTP-Passwort gesondert behandeln (Fernet-Verschlüsselung)
    from ...email_service import EMAIL_TEMPLATES, encrypt_smtp_password
    smtp_pw_enc = _encrypt_smtp_password(
        request.form.get("smtp_password", ""), encrypt_smtp_password
    )

    keys = ["smtp_host", "smtp_port", "smtp_user",
            "smtp_from", "smtp_tls", "local_login_enabled",
            "app_base_url"]
    # Dynamisch alle E-Mail-Template-Keys aufnehmen
    for tpl_key in EMAIL_TEMPLATES:
        keys.append(f"notify_enabled_{tpl_key}")
        keys.append(f"email_tpl_{tpl_key}_subject")
        keys.append(f"email_tpl_{tpl_key}_body")
    kv = [(k, request.form.get(k, "")) for k in keys]
    if smtp_pw_enc is not None:
        kv.append(("smtp_password", smtp_pw_enc))
    def _do(c):
        with write_tx(c):
            for _k, _v in kv:
                c.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)",
                          (_k, _v))
    get_writer().submit(_do, wait=True)
    flash("Einstellungen gespeichert.", "success")
    return redirect(url_for("admin.mail") + "#email-vorlagen")


def _encrypt_smtp_password(submitted: str, encrypt_fn) -> Optional[str]:
    """Verschlüsselt ein SMTP-Passwort für die spätere Persistenz.

    Gibt den verschlüsselten Wert zurueck oder ``None``, wenn nichts
    gespeichert werden soll (leerer Wert = Altbestand behalten) bzw. wenn
    die Verschlüsselung fehlgeschlagen ist (Fehler wird geflasht).
    """
    if not submitted:
        return None
    try:
        return encrypt_fn(submitted)
    except Exception as exc:
        current_app.logger.error(
            "SMTP-Passwort-Verschlüsselung fehlgeschlagen: %s", exc
        )
        flash(
            "SMTP-Passwort konnte nicht verschlüsselt werden – nicht gespeichert.",
            "error",
        )
        return None


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


def _sidecar_updates_enabled() -> bool:
    """Sidecar-ZIP-Updates können über ``app_settings['allow_sidecar_updates']``
    komplett abgeschaltet werden (VULN-B: reduziert den Admin-RCE-Vektor, wenn
    ZIP-Uploads nicht benötigt werden oder separate Change-Management-Prozesse
    verwendet werden). Admin-UI unter ``/admin/update``."""
    from ... import app_settings as _aps
    try:
        return _aps.allow_sidecar_updates(get_db())
    except Exception:
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

    service_name, service_auto_detected = _effective_service_name()
    return render_template(
        "admin/update.html",
        bundled_version=bundled_version,
        active_version=active_version,
        update_active=update_active,
        changelog=changelog,
        upd_dir=upd_dir,
        sidecar_updates_enabled=_sidecar_updates_enabled(),
        service_name=service_name,
        service_auto_detected=service_auto_detected,
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


# Top-Level-Importnamen, die im Bundle aus dem scanner/-Verzeichnis stammen
# (idvault.spec: pathex=['.', 'scanner']). Sie müssen flach unter updates/
# abgelegt werden, damit der Sidecar-Finder in run.py sie findet.
#
# scanner_protocol/path_utils/teams_scanner werden ebenfalls als Top-Level
# importiert (siehe `from scanner_protocol import emit` in
# network_scanner.py / teams_scanner.py / webapp/routes/admin/scanner.py
# bzw. `from path_utils import ...` in webapp/__init__.py). Ohne diese
# Eintraege landet ein Sidecar-Update von scanner_protocol.py unter
# updates/scanner/scanner_protocol.py – wo der SidecarFinder es nicht
# findet, sodass weiterhin die gebuendelte Version geladen wird.
_SCANNER_TOPLEVEL_MODULES = frozenset({
    "network_scanner",
    "excel_export",
    "scanner_protocol",
    "path_utils",
    "teams_scanner",
})


def _zip_remap(rel: str) -> str:
    """
    Mappt Pfade aus dem ZIP auf die Sidecar-Verzeichnisstruktur.

    Besonderheiten:
    - GitHub-ZIPs enthalten Templates unter ``webapp/templates/`` —
      der ChoiceLoader in ``run.py`` erwartet sie jedoch direkt unter
      ``updates/templates/``.
    - Scanner-Module (``network_scanner``, ``excel_export``) werden im
      PyInstaller-Bundle als Top-Level-Module importiert
      (``pathex=['.', 'scanner']`` in ``idvault.spec``). Damit der
      Sidecar-Finder in ``run.py`` sie überhaupt findet, müssen sie
      flach unter ``updates/`` liegen – nicht unter ``updates/scanner/``.
    """
    if rel.startswith('webapp/templates/'):
        return rel[len('webapp/'):]   # → templates/...
    if rel.startswith('scanner/') and rel.endswith('.py'):
        # scanner/network_scanner.py → network_scanner.py, sofern das
        # Modul als Top-Level bekannt ist (Whitelist vermeidet
        # Kollisionen mit anderen Dateien im scanner/-Verzeichnis).
        module_name = os.path.splitext(rel[len('scanner/'):])[0]
        if module_name in _SCANNER_TOPLEVEL_MODULES:
            return rel[len('scanner/'):]
    return rel


@bp.route("/update/upload", methods=["POST"])
@admin_required
@limiter.limit(_upload_rate_limit, methods=["POST"])
def update_upload():
    # VULN-B: Opt-out über app_settings.allow_sidecar_updates (Admin-UI).
    # Wenn deaktiviert, wird der Endpoint komplett abgewiesen – das
    # reduziert den Admin-RCE-Vektor in Umgebungen, in denen
    # Sidecar-Updates nicht gebraucht werden.
    if not _sidecar_updates_enabled():
        current_app.logger.warning(
            "Sidecar-Update-Upload blockiert (app_settings.allow_sidecar_updates=0)"
        )
        flash(
            "Sidecar-Updates sind deaktiviert. Unter Administration → "
            "Update den Schalter wieder aktivieren.",
            "error",
        )
        return redirect(url_for("admin.update_index"))

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


_detected_svc_name: str | None = None  # None = noch nicht geprüft


def _detect_windows_service_name() -> str:
    """Ermittelt den Windows-Dienstnamen des laufenden Prozesses via PID-Abgleich.

    Durchsucht alle aktiven Win32-Dienste nach einem Eintrag, dessen ProcessId
    mit os.getpid() übereinstimmt. Funktioniert zuverlässig bei nativer
    Dienst-Registrierung (``sc create binPath=idvault.exe``).

    Bei Service-Wrappern wie NSSM oder winsw meldet der SCM die PID des
    Wrappers, nicht die von idvault.exe – in diesem Fall liefert die Funktion
    '' zurück; IDV_SERVICE_NAME muss dann manuell gesetzt werden.

    Returns: Dienstname (interner Name) oder '' wenn nicht erkannt.
    """
    global _detected_svc_name
    if _detected_svc_name is not None:
        return _detected_svc_name
    result = ''
    if os.name == 'nt' and getattr(sys, 'frozen', False):
        try:
            import win32service
            scm = win32service.OpenSCManager(
                None, None, win32service.SC_MANAGER_ENUMERATE_SERVICE
            )
            try:
                pid = os.getpid()
                for svc in win32service.EnumServicesStatusEx(
                    scm,
                    win32service.SERVICE_WIN32,
                    win32service.SERVICE_STATE_ALL,
                ):
                    if svc.get('ProcessId') == pid:
                        result = svc['ServiceName']
                        break
            finally:
                win32service.CloseServiceHandle(scm)
        except Exception:
            pass
    _detected_svc_name = result
    return result


def _effective_service_name() -> tuple[str, bool]:
    """Gibt (Dienstname, auto_detected) zurück.

    Priorität:
      1. IDV_SERVICE_NAME aus config.json (explizit)
      2. Automatische Erkennung via EnumServicesStatusEx (nativ registriert)
    """
    from ... import config_store
    explicit = (config_store.get_str("IDV_SERVICE_NAME", "") or "").strip()
    if explicit:
        return explicit, False
    auto = _detect_windows_service_name()
    return auto, bool(auto)


def _trigger_restart():
    """Startet die EXE sauber neu (gemeinsame Logik für Update & Rollback).

    Direktmodus (kein Dienstname ermittelbar):
      Schreibt eine Batch-Datei neben die EXE, die:
        1. PyInstaller-Umgebungsvariablen löscht (_MEIPASS2 etc.)
        2. PATH auf Windows-Systemstandard zurücksetzt
        3. ~3 Sekunden wartet, bis der Port freigegeben ist
        4. Die EXE über "start" in einem neuen Konsolenfenster startet
        5. Sich selbst löscht

    Dienstmodus (IDV_SERVICE_NAME gesetzt oder automatisch erkannt):
      Schreibt eine Batch-Datei, die nach dem Prozessende ``sc start
      <servicename>`` ausführt. Der Windows-Dienst-Manager startet den
      Dienst dann sauber über den SCM neu – kein loses EXE-Fenster.
      Voraussetzung: Das Dienstkonto (z. B. LOCAL SYSTEM) muss das Recht
      SERVICE_START für den eigenen Dienst besitzen (bei LOCAL SYSTEM
      standardmäßig vorhanden).
    """
    def _do():
        import time
        time.sleep(1.5)
        if getattr(sys, 'frozen', False):
            exe = sys.executable
            service_name, _ = _effective_service_name()
            bat_path = os.path.join(os.path.dirname(exe), '_idvault_restart.bat')
            if service_name:
                # Dienstmodus: nach dem Exit des Prozesses den Dienst über
                # sc.exe neu starten. ping dient als portabler sleep-Ersatz.
                bat = (
                    '@echo off\r\n'
                    'ping -n 4 127.0.0.1 > nul\r\n'
                    'sc start "{svc}"\r\n'
                    'del "%~f0"\r\n'
                ).format(svc=service_name)
            else:
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


@bp.route("/update/sidecar-toggle", methods=["POST"])
@admin_required
def update_sidecar_toggle():
    """Schaltet den Sidecar-Update-Schalter (app_settings.allow_sidecar_updates)."""
    from ... import app_settings as _aps
    enabled = request.form.get("enabled") == "1"
    _aps.set_bool(get_db(), "allow_sidecar_updates", enabled)
    flash(
        "Sidecar-Updates " + ("aktiviert." if enabled else "deaktiviert."),
        "success",
    )
    return redirect(url_for("admin.update_index"))


@bp.route("/update/rollback", methods=["POST"])
@admin_required
def update_rollback():
    """Benennt das updates/-Verzeichnis um und startet neu (gebündelte Version)."""
    # Rollback bleibt auch bei abgeschalteten Uploads erlaubt: die Funktion
    # dient gerade dazu, ein bereits eingespieltes Update rückgängig zu machen.
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


# ── Glossar-Verwaltung ───────────────────────────────────────────────────────

_GLOSSAR_TEXT_KEYS_OVERVIEW = [
    "glossar_hintergrund_text",
    "glossar_info_unten",
]


@bp.route("/glossar")
@admin_required
def glossar_overview():
    db = get_db()
    glossar_eintraege = db.execute(
        "SELECT * FROM glossar_eintraege ORDER BY sort_order, id"
    ).fetchall()
    rows = db.execute(
        "SELECT key, value FROM app_settings WHERE key IN ({})".format(
            ",".join("?" * len(_GLOSSAR_TEXT_KEYS_OVERVIEW))
        ),
        _GLOSSAR_TEXT_KEYS_OVERVIEW,
    ).fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    return render_template(
        "admin/glossar.html",
        glossar_eintraege=glossar_eintraege,
        settings=settings,
    )


@bp.route("/glossar/neu", methods=["GET", "POST"])
@admin_required
def new_glossar():
    db = get_db()
    if request.method == "POST":
        params = (
            request.form["begriff"].strip(),
            request.form.get("entwickler", "").strip(),
            request.form.get("ort", "").strip(),
            request.form.get("fokus", "").strip(),
            request.form.get("beschreibung", "").strip(),
            1 if request.form.get("im_register") else 0,
            int(request.form.get("sort_order") or 0),
        )
        def _do(c):
            with write_tx(c):
                c.execute("""
                    INSERT INTO glossar_eintraege
                        (begriff, entwickler, ort, fokus, beschreibung, im_register, sort_order, aktiv)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """, params)
        get_writer().submit(_do, wait=True)
        flash("Glossar-Eintrag angelegt.", "success")
        return redirect(url_for("admin.glossar_overview"))
    return render_template("admin/glossar_edit.html", row=None)


@bp.route("/glossar/<int:gid>/bearbeiten", methods=["GET", "POST"])
@admin_required
def edit_glossar(gid):
    db = get_db()
    row = db.execute("SELECT * FROM glossar_eintraege WHERE id = ?", (gid,)).fetchone()
    if not row:
        flash("Eintrag nicht gefunden.", "error")
        return redirect(url_for("admin.glossar_overview"))
    if request.method == "POST":
        params = (
            request.form["begriff"].strip(),
            request.form.get("entwickler", "").strip(),
            request.form.get("ort", "").strip(),
            request.form.get("fokus", "").strip(),
            request.form.get("beschreibung", "").strip(),
            1 if request.form.get("im_register") else 0,
            int(request.form.get("sort_order") or 0),
            1 if request.form.get("aktiv") else 0,
            gid,
        )
        def _do(c):
            with write_tx(c):
                c.execute("""
                    UPDATE glossar_eintraege
                    SET begriff      = ?,
                        entwickler   = ?,
                        ort          = ?,
                        fokus        = ?,
                        beschreibung = ?,
                        im_register  = ?,
                        sort_order   = ?,
                        aktiv        = ?
                    WHERE id = ?
                """, params)
        get_writer().submit(_do, wait=True)
        flash("Glossar-Eintrag gespeichert.", "success")
        return redirect(url_for("admin.glossar_overview"))
    return render_template("admin/glossar_edit.html", row=dict(row))


@bp.route("/glossar/<int:gid>/loeschen", methods=["POST"])
@admin_required
def delete_glossar(gid):
    def _do(c):
        with write_tx(c):
            c.execute("DELETE FROM glossar_eintraege WHERE id = ?", (gid,))
    get_writer().submit(_do, wait=True)
    flash("Glossar-Eintrag gelöscht.", "success")
    return redirect(url_for("admin.glossar_overview"))


@bp.route("/glossar/erklaerung", methods=["GET", "POST"])
@admin_required
def glossar_erklaerung():
    db = get_db()
    if request.method == "POST":
        kv = [(key, request.form.get(key, "").strip())
              for key in _GLOSSAR_TEXT_KEYS_OVERVIEW]
        def _do(c):
            with write_tx(c):
                for _k, _v in kv:
                    c.execute(
                        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                        (_k, _v),
                    )
        get_writer().submit(_do, wait=True)
        flash("Erläuterungstexte gespeichert.", "success")
        return redirect(url_for("admin.glossar_erklaerung"))
    rows = db.execute(
        "SELECT key, value FROM app_settings WHERE key IN ({})".format(
            ",".join("?" * len(_GLOSSAR_TEXT_KEYS_OVERVIEW))
        ),
        _GLOSSAR_TEXT_KEYS_OVERVIEW,
    ).fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    return render_template("admin/glossar_erklaerung.html", settings=settings)


# ── Submodule registrieren (Blueprint-Routen anhängen) ─────────────────
from . import scanner, mail, sicherheit, ldap, stammdaten, similarity, pfad_profile, freigabe, testfall_vorlagen, backup  # noqa: E402,F401
from .scanner import (  # noqa: E402,F401
    start_scheduler,
    _scan_is_running,
    _load_scanner_config,
    _scan_is_paused,
    _has_checkpoint,
)
