"""Admin-Blueprint: Stammdaten verwalten"""
import csv
import io
import os
import sqlite3
import sys
import json
import re
import hashlib
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response, jsonify, current_app
from . import login_required, admin_required, write_access_required, get_db
from ..security import in_clause
from .. import limiter
from datetime import datetime, timezone, timedelta

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _upload_rate_limit():
    """VULN-009: Rate-Limit für Admin-Uploads (ZIP, CSV). Wird zur
    Request-Zeit aus app.config gelesen, damit config.json-Änderungen greifen."""
    try:
        return current_app.config.get(
            "IDV_UPLOAD_RATE_LIMIT", "10 per minute;60 per hour"
        )
    except RuntimeError:
        return "10 per minute;60 per hour"

# ── Scanner-Konfiguration & Scan-Trigger ────────────────────────────────────

_scan_lock  = threading.Lock()
_scan_state = {"pid": None, "started": None}   # veränderlich, kein global nötig

# ── Zeitplan-Scheduler ────────────────────────────────────────────────────────
_scheduler_thread_obj: threading.Thread = None
# Verhindert Doppelauslösung: letztes Auslösedatum "YYYY-MM-DD" (in-memory)
_schedule_last_triggered: str = None


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


_SCANNER_CFG_KEYS = frozenset({
    "scan_paths", "extensions", "db_path", "log_path",
    "hash_size_limit_mb", "max_workers", "move_detection", "scan_since", "read_file_owner",
    "blacklist_paths", "whitelist_paths",
})


def _default_scanner_cfg() -> dict:
    """Erstellt die Standardkonfiguration mit relativen Default-Pfaden.

    Die Pfade werden bewusst relativ zur config.json gespeichert (gut
    lesbar, portable Installation). Der Scanner löst sie beim Start gegen
    das Verzeichnis der config.json auf.
    """
    return {
        "scan_paths": [],
        "extensions": _DEFAULT_SCANNER_EXTENSIONS,
        "blacklist_paths": _DEFAULT_SCANNER_EXCLUDE,
        "whitelist_paths": [],
        "db_path": "instance/idvault.db",
        "log_path": "instance/logs/idv_scanner.log",
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
        # Zusammengeführte config.json: Scanner-Einstellungen unter "scanner"-Schlüssel.
        # Nur bekannte Scanner-Keys übernehmen, damit Top-Level-Keys (SECRET_KEY, PORT …)
        # nicht in den Scanner-Abschnitt eingeschleppt werden.
        scanner_data = full.get("scanner", full)
        cfg.update({k: v for k, v in scanner_data.items() if k in _SCANNER_CFG_KEYS})
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
    # Nur bekannte Scanner-Keys speichern – keine Top-Level-Keys duplizieren
    full["scanner"] = {k: v for k, v in cfg.items() if k in _SCANNER_CFG_KEYS}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(full, f, indent=2, ensure_ascii=False)


def _load_path_mappings() -> list:
    """Lädt path_mappings aus dem Top-Level der config.json."""
    from .. import config_store
    cfg = config_store.load_config_json()
    mappings = cfg.get("path_mappings", [])
    return mappings if isinstance(mappings, list) else []


def _save_path_mappings(mappings: list) -> None:
    """Speichert path_mappings als Top-Level-Schlüssel in config.json."""
    from .. import config_store
    config_store.write_top_level_key("path_mappings", mappings)


def _load_scanner_runas() -> dict:
    """Lädt Run-As-Konfiguration aus app_settings.

    Gibt dict mit keys zurück: domain, username, password (entschlüsselt),
    password_enc (verschlüsselt), decrypt_error (Fehlermeldung oder "").
    Fehlende Felder sind leere Strings.
    """
    try:
        db = get_db()
        rows = db.execute(
            "SELECT key, value FROM app_settings WHERE key IN ("
            "'scanner_runas_domain', 'scanner_runas_username', 'scanner_runas_password'"
            ")"
        ).fetchall()
        settings = {r["key"]: r["value"] for r in rows}
        domain       = settings.get("scanner_runas_domain",   "") or ""
        username     = settings.get("scanner_runas_username", "") or ""
        password_enc = settings.get("scanner_runas_password", "") or ""
        password = ""
        decrypt_error = ""
        if password_enc:
            try:
                from ..ldap_auth import decrypt_password
                password = decrypt_password(
                    password_enc, current_app.config["SECRET_KEY"]
                )
            except Exception as exc:
                # Typisch: SECRET_KEY hat sich seit dem Speichern geändert,
                # oder die verschlüsselten Daten sind korrupt. Ohne Passwort
                # kann der Scanner nicht als Run-As-User starten.
                decrypt_error = f"{type(exc).__name__}: {exc}"
        return {
            "domain":         domain,
            "username":       username,
            "password":       password,
            "password_enc":   password_enc,
            "decrypt_error":  decrypt_error,
        }
    except Exception:
        return {"domain": "", "username": "", "password": "",
                "password_enc": "", "decrypt_error": ""}


def _unc_share_root(path: str):
    """Extrahiert ``\\\\server\\share`` aus einem UNC-Pfad.

    WNetAddConnection2 registriert Credentials pro ``\\\\server\\share`` –
    tiefere Pfade müssen auf diesen Root reduziert werden, sonst meldet
    Windows ``ERROR_BAD_NET_NAME``. Gibt ``None`` zurück, wenn ``path``
    kein UNC-Pfad ist (z. B. lokaler Pfad).
    """
    if not path:
        return None
    p = path.replace("/", "\\")
    if not p.startswith("\\\\"):
        return None
    # Nach "\\\\" liefert split ['', '', 'server', 'share', ...]
    parts = p.split("\\")
    if len(parts) < 4 or not parts[2] or not parts[3]:
        return None
    return "\\\\" + parts[2] + "\\" + parts[3]


def _mpr_bindings():
    """Baut die ctypes-Bindings für WNetAddConnection2W / WNetCancelConnection2W.

    Bewusst über ``ctypes`` statt ``win32wnet``: ``ctypes`` + ``mpr.dll``
    sind in jeder Python-Standard- und PyInstaller-Umgebung vorhanden,
    ``win32wnet.pyd`` hingegen nur wenn es explizit als Hidden-Import
    gebundelt wurde. Damit ist diese Funktionalität auch per Sidecar-
    Update ohne EXE-Neubau ausrollbar.
    """
    import ctypes
    from ctypes import wintypes

    class _NETRESOURCEW(ctypes.Structure):
        _fields_ = [
            ("dwScope",       wintypes.DWORD),
            ("dwType",        wintypes.DWORD),
            ("dwDisplayType", wintypes.DWORD),
            ("dwUsage",       wintypes.DWORD),
            ("lpLocalName",   wintypes.LPWSTR),
            ("lpRemoteName",  wintypes.LPWSTR),
            ("lpComment",     wintypes.LPWSTR),
            ("lpProvider",    wintypes.LPWSTR),
        ]

    mpr = ctypes.WinDLL("mpr.dll", use_last_error=True)

    add = mpr.WNetAddConnection2W
    add.argtypes = [
        ctypes.POINTER(_NETRESOURCEW),
        wintypes.LPCWSTR,   # password
        wintypes.LPCWSTR,   # username
        wintypes.DWORD,     # flags
    ]
    add.restype = wintypes.DWORD

    cancel = mpr.WNetCancelConnection2W
    cancel.argtypes = [
        wintypes.LPCWSTR,   # name
        wintypes.DWORD,     # flags
        wintypes.BOOL,      # force
    ]
    cancel.restype = wintypes.DWORD

    return _NETRESOURCEW, add, cancel


# Bekannte Windows-Fehlercodes für WNetAddConnection2. Die Liste ist nicht
# erschöpfend – unbekannte Codes werden als numerischer Wert geloggt.
_WNET_ERROR_HINTS = {
    5:    "Zugriff verweigert (Share-/NTFS-Rechte prüfen)",
    53:   "Netzwerkpfad nicht gefunden (DNS / SMB)",
    67:   "Netzwerkname kann nicht gefunden werden",
    86:   "Falsches Netzwerkkennwort",
    1219: "Session hält bereits andere Credentials für diesen Server "
          "(ERROR_SESSION_CREDENTIAL_CONFLICT)",
    1326: "Anmeldung fehlgeschlagen – Benutzername/Passwort falsch "
          "oder abgelaufen (ERROR_LOGON_FAILURE)",
    1327: "Konto-Einschränkung (leeres Passwort, gesperrt, abgelaufen)",
    2250: "Netzwerkverbindung existiert nicht",
}


def _register_unc_credentials(scan_paths: list, domain: str, username: str,
                              password: str) -> tuple:
    """Registriert AD-Credentials via ``WNetAddConnection2W`` für jeden
    ``\\\\server\\share``-Root der konfigurierten Scan-Pfade.

    Analog zu ``net use \\\\server\\share /user:DOMAIN\\foo pw`` speichert
    Windows die Credentials in der LSA-Credential-Cache der aktuellen
    Logon-Session. Subprozesse, die später als derselbe Dienst-User
    gestartet werden, erben diese Session und nutzen die Credentials
    transparent für UNC-Zugriffe via NTLM.

    Gibt ``(registered_roots, messages)`` zurück. ``registered_roots``
    kann an ``_cancel_unc_credentials`` übergeben werden. ``messages``
    enthält Diagnose-Zeilen für jedes registrierte/fehlgeschlagene Share
    und wird vom Aufrufer ins Scanner-Log geschrieben.
    """
    import ctypes

    NETRESOURCEW, WNetAddConnection2W, WNetCancelConnection2W = _mpr_bindings()
    RESOURCETYPE_DISK = 0x00000001

    full_user = f"{domain}\\{username}" if (domain and domain != ".") else username

    # Eindeutige Share-Roots bestimmen (Reihenfolge beibehalten)
    roots: list = []
    seen = set()
    for p in scan_paths or []:
        root = _unc_share_root(p)
        if not root:
            continue
        key = root.lower()
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)

    registered: list = []
    messages: list = []

    for root in roots:
        # Bestehende Verbindung zu diesem Share entfernen – sonst schlägt
        # WNetAddConnection2 mit ERROR_SESSION_CREDENTIAL_CONFLICT (1219)
        # fehl, wenn der Dienstkontext bereits eine andere Zuordnung
        # (z. B. vom vorherigen Scan-Lauf oder vom Dienstkonto selbst)
        # eingetragen hat. Rückgabewert wird bewusst ignoriert.
        WNetCancelConnection2W(root, 0, True)

        nr = NETRESOURCEW()
        nr.dwType       = RESOURCETYPE_DISK
        nr.lpRemoteName = root
        nr.lpLocalName  = None  # keine Laufwerksbuchstaben-Zuordnung
        nr.lpProvider   = None

        rc = WNetAddConnection2W(ctypes.byref(nr), password, full_user, 0)
        if rc == 0:
            registered.append(root)
            messages.append(
                f"UNC-Credentials registriert für {root} (als {full_user})"
            )
        else:
            hint = _WNET_ERROR_HINTS.get(rc, "")
            tail = f" – {hint}" if hint else ""
            messages.append(
                f"WNetAddConnection2 für {root} fehlgeschlagen: "
                f"WinError {rc}{tail}"
            )

    return registered, messages


def _cancel_unc_credentials(registered_roots: list) -> None:
    """Räumt per ``_register_unc_credentials`` angelegte Verbindungen auf.

    Fehler werden geschluckt – der Scanner-Lauf ist zu diesem Zeitpunkt
    bereits beendet, ein Cleanup-Fehler darf den Aufrufer nicht stören.
    """
    if not registered_roots:
        return
    try:
        _NETRES, _add, WNetCancelConnection2W = _mpr_bindings()
    except Exception:
        return
    for root in registered_roots:
        try:
            WNetCancelConnection2W(root, 0, True)
        except Exception:
            pass


_RUNAS_REQUIRED_MODULES = (
    "pywintypes", "win32security",
)


def _check_runas_modules() -> tuple:
    """Prüft, ob alle pywin32-Module für die Run-As-Diagnose vorhanden sind.

    Benötigt wird ``win32security`` (``LogonUser``) für den Credential-Test
    im Admin-Bereich. Die eigentliche Credential-Registrierung im Scan-
    Start läuft über ``ctypes`` + ``mpr.dll`` – ohne pywin32-Abhängigkeit,
    damit sie auch per Sidecar-Update ohne EXE-Neubau wirksam wird.
    Gibt ``(ok, missing)`` zurück.
    """
    missing = []
    for mod in _RUNAS_REQUIRED_MODULES:
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    return (not missing), missing


def _truncate_scanner_log(log_path: str) -> None:
    """Leert die stdout/stderr-Log-Datei zu Beginn eines Scan-Starts.

    Muss VOR _write_scanner_notice aufgerufen werden – danach wird nur
    noch angehängt, damit alle Diagnose-Zeilen des Start-Vorgangs
    erhalten bleiben (früher hat der Fallback-Zweig die Datei erneut
    mit ``open(..., "w")`` überschrieben und alle Notices gelöscht).
    """
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8"):
            pass
    except Exception:
        pass


def _write_scanner_notice(log_path: str, lines: list) -> None:
    """Hängt Hinweiszeilen an die stdout/stderr-Log-Datei des Scanners an.

    Die Zeilen erscheinen im Admin-Bereich im Scan-Log-Viewer
    (Dropdown ``stdout/stderr-Mitschnitt``). Präfix ``[IDVAULT-START]``
    macht sie leicht von regulären Scanner-Log-Zeilen unterscheidbar.
    """
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(f"{ts} [IDVAULT-START] {line}\n")
    except Exception:
        pass  # Logging-Fehler dürfen den Start nicht blockieren


def _scanner_subprocess_env() -> dict:
    """Baut das Environment für den Scanner-Subprocess.

    Setzt ``PYTHONIOENCODING=utf-8`` + ``PYTHONUTF8=1``, damit Python
    auf Windows seine stdout/stderr als UTF-8 enkodiert. Ohne diese
    Variablen schreibt Python Umlaute in CP1252 in das
    ``scanner_output.log`` – der Log-Viewer, der die Datei als UTF-8
    liest, zeigt dann ``�`` statt ``ä/ö/ü``.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"]       = "1"
    return env


def _start_scanner_proc(cmd: list, cwd: str, log_path: str):
    """Startet den Scanner-Subprocess.

    Auf Windows mit konfiguriertem Run-As-Benutzer werden dessen
    Credentials vor dem Start via ``WNetAddConnection2`` für jeden
    ``\\\\server\\share`` der konfigurierten Scan-Pfade in der aktuellen
    Logon-Session registriert (Analogon zu ``net use``). Der Scanner
    läuft weiterhin im Kontext des idvault-Dienstes; UNC-Zugriffe nutzen
    die registrierten Credentials per NTLM.

    Diese Lösung ersetzt den früheren ``LogonUser`` +
    ``CreateProcessAsUser``-Weg: Letzterer ist in vielen gehärteten
    Umgebungen durch Group-Policy (``ERROR_ACCESS_DISABLED_BY_POLICY``,
    WinError 1260) blockiert und benötigt Sonderprivilegien
    (``SeAssignPrimaryTokenPrivilege``), die nur LOCAL SYSTEM besitzt.
    ``WNetAddConnection2`` benötigt keines von beidem.

    Schreibt bei jedem Start mindestens eine ``[IDVAULT-START]``-Zeile
    ins Log, die den gewählten Pfad und – falls Run-As aktiv – die
    registrierten Share-Roots dokumentiert.

    Gibt (pid, wait_fn) zurück. wait_fn() blockiert bis der Prozess
    beendet ist und hebt die Share-Registrierungen wieder auf.
    """
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )

    # Log einmal zu Beginn leeren – alle nachfolgenden Schreibvorgänge
    # (Notices + Scanner-Output) hängen an.
    _truncate_scanner_log(log_path)

    runas = _load_scanner_runas()
    username_set   = bool(runas.get("username"))
    has_enc_pw     = bool(runas.get("password_enc"))
    password_ok    = bool(runas.get("password"))
    decrypt_err    = runas.get("decrypt_error", "")
    runas_configured = username_set and password_ok

    # Immer eine Diagnose-Zeile zum Start schreiben.
    if not username_set:
        _write_scanner_notice(log_path, [
            "Run-As nicht konfiguriert – Scanner läuft im Kontext des "
            "idvault-Prozesses (Dienstkonto)."
        ])
    elif has_enc_pw and not password_ok:
        # Passwort liegt verschlüsselt vor, ließ sich aber nicht
        # entschlüsseln. Häufigste Ursache: SECRET_KEY hat sich seit
        # dem Speichern der Konfiguration geändert.
        msg = (
            f"Run-As-Passwort für {runas['domain'] or '.'}\\{runas['username']} "
            f"konnte nicht entschlüsselt werden ({decrypt_err}). "
            f"Typische Ursache: Der SECRET_KEY der Anwendung hat sich seit "
            f"dem Speichern geändert. Abhilfe: Administration → Scanner-"
            f"Einstellungen → Run-As → Passwort erneut eintragen und "
            f"speichern. Scanner läuft bis dahin im Kontext des idvault-"
            f"Prozesses (Dienstkonto)."
        )
        current_app.logger.error(msg)
        _write_scanner_notice(log_path, [msg])
    elif not has_enc_pw:
        _write_scanner_notice(log_path, [
            f"Run-As-Benutzer {runas['domain'] or '.'}\\{runas['username']} "
            f"gespeichert, aber kein Passwort hinterlegt. Scanner läuft im "
            f"Kontext des idvault-Prozesses (Dienstkonto)."
        ])

    registered_unc: list = []
    if os.name == "nt" and runas_configured:
        try:
            scan_paths = _load_scanner_config().get("scan_paths", [])
        except Exception:
            scan_paths = []
        unc_roots_in_config = [r for r in (_unc_share_root(p)
                                           for p in scan_paths) if r]

        _write_scanner_notice(log_path, [
            f"Registriere UNC-Credentials für Run-As-Benutzer "
            f"{runas['domain'] or '.'}\\{runas['username']} "
            f"(WNetAddConnection2)…"
        ])
        try:
            registered_unc, msgs = _register_unc_credentials(
                scan_paths,
                runas["domain"] or ".",
                runas["username"],
                runas["password"],
            )
            if msgs:
                _write_scanner_notice(log_path, msgs)
            if not unc_roots_in_config:
                _write_scanner_notice(log_path, [
                    "Hinweis: Keine UNC-Pfade (\\\\server\\share) in den "
                    "Scan-Pfaden konfiguriert – Run-As-Credentials werden "
                    "nicht benötigt und nicht registriert."
                ])
            elif not registered_unc:
                _write_scanner_notice(log_path, [
                    "Alle UNC-Registrierungen fehlgeschlagen – Scanner "
                    "läuft im Dienstkontext ohne gesonderte Credentials."
                ])
            else:
                current_app.logger.info(
                    "UNC-Credentials für %s\\%s registriert: %s",
                    runas["domain"] or ".", runas["username"],
                    ", ".join(registered_unc),
                )
        except Exception as exc:
            msg = (
                f"WNet-Credential-Registrierung fehlgeschlagen "
                f"({type(exc).__name__}: {exc}) – Scanner läuft im "
                f"Dienstkontext ohne gesonderte Credentials."
            )
            current_app.logger.error(msg)
            _write_scanner_notice(log_path, [msg])

    # Scanner im Kontext des idvault-Prozesses (Dienstkonto) starten.
    # "a" (append) damit die oben geschriebenen [IDVAULT-START]-Zeilen
    # nicht überschrieben werden. Line-buffering (buffering=1) sorgt dafür,
    # dass Scanner-Output zeitnah sichtbar wird.
    log_fh = open(log_path, "a", encoding="utf-8", buffering=1)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=log_fh,
            cwd=cwd,
            creationflags=creationflags,
            env=_scanner_subprocess_env(),
        )
    except Exception:
        log_fh.close()
        _cancel_unc_credentials(registered_unc)
        raise

    def _wait():
        try:
            proc.wait()
        finally:
            log_fh.close()
            _cancel_unc_credentials(registered_unc)

    return proc.pid, _wait


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


# ── Zeitplan-Scheduler-Hilfsfunktionen ───────────────────────────────────────

_WEEKDAY_NAMES = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
                  "Freitag", "Samstag", "Sonntag"]


def _load_schedule_settings(db) -> dict:
    """Liest Zeitplan-Einstellungen aus app_settings. Fehlende Schlüssel → Defaults."""
    defaults = {
        "scan_schedule_enabled": "0",
        "scan_schedule_type":    "daily",
        "scan_schedule_time":    "02:00",
        "scan_schedule_weekday": "0",   # 0 = Montag (Python weekday())
    }
    for key in defaults:
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        if row:
            defaults[key] = row["value"]
    return defaults


def _next_scheduled_scan(schedule: dict) -> str:
    """Berechnet den nächsten geplanten Scan-Zeitpunkt als lesbaren String.
    Gibt None zurück wenn kein Zeitplan aktiv oder Konfiguration ungültig."""
    if schedule.get("scan_schedule_enabled") != "1":
        return None
    try:
        h, m = map(int, schedule["scan_schedule_time"].split(":"))
    except Exception:
        return None

    now = datetime.now()
    scheduled_today = now.replace(hour=h, minute=m, second=0, microsecond=0)

    if schedule["scan_schedule_type"] == "daily":
        next_dt = scheduled_today if now < scheduled_today else scheduled_today + timedelta(days=1)
    elif schedule["scan_schedule_type"] == "weekly":
        try:
            target_wd = int(schedule["scan_schedule_weekday"])
        except Exception:
            return None
        days_ahead = target_wd - now.weekday()
        if days_ahead < 0:
            days_ahead += 7
        elif days_ahead == 0 and now >= scheduled_today:
            days_ahead = 7
        next_dt = scheduled_today + timedelta(days=days_ahead)
    else:
        return None

    return next_dt.strftime("%d.%m.%Y um %H:%M Uhr")


def _trigger_scheduled_scan() -> bool:
    """Startet einen Scan im Hintergrund (Zeitplan-Auslösung).

    Muss innerhalb eines Flask-App-Kontexts aufgerufen werden.
    Gibt True zurück wenn der Scan erfolgreich gestartet wurde.
    """
    with _scan_lock:
        if _scan_is_running():
            return False

        config_path = _scanner_config_path()
        if not os.path.isfile(config_path):
            return False

        scanner_dir = _scanner_dir()
        os.makedirs(scanner_dir, exist_ok=True)

        for sig_name in ("scanner_pause.signal", "scanner_cancel.signal"):
            try:
                os.remove(os.path.join(scanner_dir, sig_name))
            except FileNotFoundError:
                pass

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--scan", "--config", config_path,
                   "--signal-dir", scanner_dir]
        else:
            script = _scanner_script_path()
            if not os.path.isfile(script):
                current_app.logger.error(
                    "Zeitplan-Scan: Scanner-Skript nicht gefunden: %s", script
                )
                return False
            cmd = [sys.executable, script, "--config", config_path,
                   "--signal-dir", scanner_dir]

        logs_dir = _instance_logs_dir()
        os.makedirs(logs_dir, exist_ok=True)
        output_log = os.path.join(logs_dir, "scanner_output.log")

        try:
            pid, wait_fn = _start_scanner_proc(cmd, scanner_dir, output_log)
            _scan_state["pid"]     = pid
            _scan_state["started"] = datetime.now(timezone.utc).isoformat()

            def _watch():
                wait_fn()
                with _scan_lock:
                    if _scan_state.get("pid") == pid:
                        _scan_state["pid"]     = None
                        _scan_state["started"] = None

            threading.Thread(target=_watch, daemon=True).start()
            current_app.logger.info("Zeitplan-Scan gestartet (PID %d).", pid)
            return True
        except Exception as exc:
            current_app.logger.error("Zeitplan-Scan konnte nicht gestartet werden: %s", exc)
            return False


def _scheduler_loop(app) -> None:
    """Daemon-Thread: prüft jede Minute ob ein geplanter Scan fällig ist."""
    global _schedule_last_triggered

    # Kurze Wartezeit beim Start – App soll vollständig hochgefahren sein
    time.sleep(30)

    while True:
        try:
            with app.app_context():
                db = get_db()
                cfg = _load_schedule_settings(db)

                if cfg["scan_schedule_enabled"] != "1":
                    pass  # Weiter schlafen
                else:
                    now      = datetime.now()
                    today_str = now.strftime("%Y-%m-%d")

                    # Heute bereits ausgelöst (in-memory Check)?
                    if _schedule_last_triggered != today_str:
                        # Auch DB-seitig prüfen (überlebt Neustart)
                        db_row = db.execute(
                            "SELECT value FROM app_settings "
                            "WHERE key='scan_schedule_last_triggered_date'"
                        ).fetchone()
                        db_last = db_row["value"] if db_row else None

                        if db_last != today_str:
                            # Uhrzeit auswerten
                            try:
                                h, m = map(int, cfg["scan_schedule_time"].split(":"))
                            except Exception:
                                h, m = 2, 0

                            if now.hour > h or (now.hour == h and now.minute >= m):
                                # Wochentag prüfen (bei wöchentlichem Zeitplan)
                                should_run = True
                                if cfg["scan_schedule_type"] == "weekly":
                                    try:
                                        target_wd = int(cfg["scan_schedule_weekday"])
                                    except Exception:
                                        target_wd = -1
                                    should_run = (now.weekday() == target_wd)

                                if should_run:
                                    started = _trigger_scheduled_scan()
                                    if started:
                                        _schedule_last_triggered = today_str
                                        db.execute(
                                            "INSERT OR REPLACE INTO app_settings "
                                            "(key, value) VALUES "
                                            "('scan_schedule_last_triggered_date', ?)",
                                            (today_str,)
                                        )
                                        db.commit()
        except Exception:
            try:
                app.logger.exception("Fehler im Scan-Scheduler")
            except Exception:
                pass

        time.sleep(60)


def start_scheduler(app) -> None:
    """Startet den Scheduler-Daemon-Thread (einmalig; idempotent)."""
    global _scheduler_thread_obj
    if _scheduler_thread_obj is not None and _scheduler_thread_obj.is_alive():
        return
    t = threading.Thread(
        target=_scheduler_loop,
        args=(app,),
        daemon=True,
        name="idvault-scan-scheduler",
    )
    _scheduler_thread_obj = t
    t.start()


@bp.route("/scanner-einstellungen", methods=["GET", "POST"])
@admin_required
def scanner_einstellungen():
    cfg = _load_scanner_config()
    path_mappings = _load_path_mappings()

    if request.method == "POST":
        # Separates Formular nur für Pfad-Mappings?
        if request.form.get("_only_path_mappings") == "1":
            try:
                pm_raw = request.form.get("path_mappings_json", "[]")
                pm_new = json.loads(pm_raw)
                if not isinstance(pm_new, list):
                    pm_new = []
                _save_path_mappings(pm_new)
                flash("Pfad-Mappings gespeichert.", "success")
            except Exception as exc:
                flash(f"Fehler beim Speichern der Pfad-Mappings: {exc}", "error")
            return redirect(url_for("admin.scanner_einstellungen"))

        scan_paths    = [p.strip() for p in request.form.get("scan_paths",    "").splitlines() if p.strip()]
        extensions    = [e.strip().lower() for e in request.form.get("extensions",    "").splitlines() if e.strip()]
        blacklist_paths = [p.strip() for p in request.form.get("blacklist_paths", "").splitlines() if p.strip()]
        whitelist_paths = [p.strip() for p in request.form.get("whitelist_paths", "").splitlines() if p.strip()]

        # path_mappings aus JSON-Feld (wird per JS serialisiert)
        try:
            pm_raw = request.form.get("path_mappings_json", "[]")
            path_mappings_new = json.loads(pm_raw)
            if not isinstance(path_mappings_new, list):
                path_mappings_new = []
        except (ValueError, TypeError):
            path_mappings_new = []
            flash("Pfad-Mappings konnten nicht gelesen werden – bitte prüfen.", "warning")

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
            "blacklist_paths":   blacklist_paths,
            "whitelist_paths":   whitelist_paths,
            "hash_size_limit_mb": hash_limit,
            "max_workers":       max_workers,
            "move_detection":    move_det,
            "scan_since":        scan_since,
            "read_file_owner":   read_file_owner,
        })
        # Zeitplan-Einstellungen validieren
        sched_enabled = "1" if request.form.get("scan_schedule_enabled") == "1" else "0"
        sched_type    = request.form.get("scan_schedule_type", "daily")
        if sched_type not in ("daily", "weekly"):
            sched_type = "daily"
        sched_time = request.form.get("scan_schedule_time", "02:00").strip()
        try:
            _sh, _sm = map(int, sched_time.split(":"))
            if not (0 <= _sh <= 23 and 0 <= _sm <= 59):
                raise ValueError
            sched_time = f"{_sh:02d}:{_sm:02d}"
        except Exception:
            sched_time = "02:00"
        sched_weekday = request.form.get("scan_schedule_weekday", "0")
        try:
            _wd = int(sched_weekday)
            if not (0 <= _wd <= 6):
                raise ValueError
            sched_weekday = str(_wd)
        except Exception:
            sched_weekday = "0"

        try:
            _save_scanner_config(cfg)
            _save_path_mappings(path_mappings_new)
            path_mappings = path_mappings_new
            # App-Settings für Auto-Ignorieren, Verwerfen und Zeitplan speichern
            db = get_db()
            val_ai = "1" if request.form.get("auto_ignore_no_formula") == "1" else "0"
            val_dc = "1" if request.form.get("discard_no_formula") == "1" else "0"
            val_cf = "1" if request.form.get("auto_classify_by_filename") == "1" else "0"
            for _key, _val in [
                ("auto_ignore_no_formula",    val_ai),
                ("discard_no_formula",        val_dc),
                ("auto_classify_by_filename", val_cf),
                ("scan_schedule_enabled",     sched_enabled),
                ("scan_schedule_type",        sched_type),
                ("scan_schedule_time",        sched_time),
                ("scan_schedule_weekday",     sched_weekday),
            ]:
                db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)",
                           (_key, _val))
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
    classify_fn = db.execute(
        "SELECT value FROM app_settings WHERE key='auto_classify_by_filename'"
    ).fetchone()
    schedule = _load_schedule_settings(db)
    runas = _load_scanner_runas()
    return render_template("admin/scanner_einstellungen.html",
                           cfg=cfg, scan_running=_scan_is_running(),
                           path_mappings=path_mappings,
                           auto_ignore_no_formula=(auto_ignore["value"] if auto_ignore else "0"),
                           discard_no_formula=(discard_nf["value"] if discard_nf else "0"),
                           auto_classify_by_filename=(classify_fn["value"] if classify_fn else "1"),
                           schedule=schedule,
                           schedule_next=_next_scheduled_scan(schedule),
                           weekday_names=_WEEKDAY_NAMES,
                           runas=runas)


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
        # Subprocess von der Konsole des Parents isolieren: sonst werden
        # Windows-Konsolen-Control-Events (z.B. CTRL_C_EVENT, die bei
        # Netzlaufwerk-Störungen ausgelöst werden) an alle an dieselbe
        # Konsole gekoppelten Prozesse zugestellt. Der Scanner fängt
        # KeyboardInterrupt ab und läuft weiter – der Flask-Prozess
        # (Werkzeug-Dev-Server) bekommt das gleiche Signal und beendet
        # sich still, wie bei Ctrl+C. Siehe claude/debug-network-scan.
        # Abbrechen erfolgt via Signal-Dateien, nicht via CTRL_C.
        # Wenn ein technischer AD-Benutzer konfiguriert ist, werden dessen
        # Credentials vor dem Start via WNetAddConnection2 für die UNC-
        # Share-Roots der Scan-Pfade registriert (Windows).
        try:
            pid, wait_fn = _start_scanner_proc(
                cmd, os.path.dirname(scanner_dir), output_log
            )
            _scan_state["pid"]     = pid
            _scan_state["started"] = datetime.now(timezone.utc).isoformat()

            def _watch():
                wait_fn()
                with _scan_lock:
                    if _scan_state.get("pid") == pid:
                        _scan_state["pid"]     = None
                        _scan_state["started"] = None

            threading.Thread(target=_watch, daemon=True).start()
            mode_label = "fortgesetzt" if resume else "gestartet"
            return jsonify({"ok": True, "msg": f"Scan {mode_label} (PID {pid}).", "pid": pid})
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


@bp.route("/scanner/runas/speichern", methods=["POST"])
@admin_required
def scanner_runas_speichern():
    """Speichert den technischen AD-Benutzer für den Scanner."""
    from ..ldap_auth import encrypt_password

    db = get_db()
    domain   = request.form.get("runas_domain",   "").strip()
    username = request.form.get("runas_username",  "").strip()
    password_plain = request.form.get("runas_password", "").strip()

    # Bestehendes Passwort beibehalten wenn Feld leer gelassen wird
    if password_plain:
        secret_key   = current_app.config["SECRET_KEY"]
        password_enc = encrypt_password(password_plain, secret_key)
    else:
        existing = db.execute(
            "SELECT value FROM app_settings WHERE key='scanner_runas_password'"
        ).fetchone()
        password_enc = existing["value"] if existing else ""

    for key, val in [
        ("scanner_runas_domain",   domain),
        ("scanner_runas_username", username),
        ("scanner_runas_password", password_enc),
    ]:
        db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (key, val),
        )
    db.commit()

    if username:
        display = f"{domain}\\{username}" if domain else username
        flash(f"Scanner-Benutzer \"{display}\" gespeichert.", "success")
    else:
        flash(
            "Technischer Scanner-Benutzer entfernt – "
            "Scanner läuft im aktuellen Benutzerkontext.",
            "success",
        )
    return redirect(url_for("admin.scanner_einstellungen") + "#runas")


@bp.route("/scanner/runas/testen", methods=["POST"])
@admin_required
def scanner_runas_testen():
    """Testet die konfigurierten Run-As-Credentials via Windows LogonUser.

    Die eigentliche UNC-Credential-Registrierung beim Scan-Start läuft
    über ``ctypes`` + ``mpr.dll`` und ist damit unabhängig von pywin32.
    Dieser Endpoint dient nur der Diagnose: LogonUser meldet innerhalb
    von Millisekunden, ob Benutzer/Passwort gegen das AD gültig sind –
    ohne auf einen Scan-Lauf warten zu müssen.
    """
    if os.name != "nt":
        return jsonify(ok=False, msg="Nur auf Windows-Systemen verfügbar.")

    runas = _load_scanner_runas()
    username = runas.get("username", "")
    domain   = runas.get("domain")  or "."
    password = runas.get("password", "")

    if not username:
        return jsonify(ok=False, msg="Kein Benutzer konfiguriert.")
    if not password:
        return jsonify(ok=False, msg="Kein Kennwort hinterlegt.")

    try:
        import win32security
    except ImportError:
        return jsonify(
            ok=False,
            msg=("pywin32 nicht installiert/gebundlet – der Credential-Test "
                 "kann nicht ausgeführt werden. Der Scan-Start selbst läuft "
                 "über ctypes und funktioniert unabhängig davon; bei Fehlern "
                 "bitte direkt das Scanner-Log prüfen.")
        )

    try:
        token = win32security.LogonUser(
            username,
            domain,
            password,
            win32security.LOGON32_LOGON_NETWORK,
            win32security.LOGON32_PROVIDER_DEFAULT,
        )
        token.Close()
    except Exception as exc:
        return jsonify(ok=False, msg=f"LogonUser fehlgeschlagen: {exc}")

    display = f"{domain}\\{username}" if domain != "." else username
    return jsonify(
        ok=True,
        msg=(f"Anmeldung als \"{display}\" erfolgreich. "
             f"Die Credentials werden beim nächsten Scan-Start via "
             f"WNetAddConnection2 für die konfigurierten UNC-Shares "
             f"registriert.")
    )


@bp.route("/scanner/runas/status")
@admin_required
def scanner_runas_status():
    """Liefert den Diagnose-Status der Run-As-Konfiguration als JSON.

    Wird von der Scanner-Einstellungen-Seite beim Laden aufgerufen, um
    einen roten Warnbanner einzublenden, wenn die Konfiguration zwar
    gespeichert, aber nicht wirksam ist (z. B. weil pywin32-Module für
    WNetAddConnection2 im EXE-Build fehlen).
    """
    if os.name != "nt":
        return jsonify(platform_ok=False, modules_ok=True, missing=[],
                       configured=False, password_ok=True,
                       message="Run-As ist nur auf Windows-Systemen wirksam.")

    runas = _load_scanner_runas()
    configured  = bool(runas.get("username"))
    password_ok = bool(runas.get("password")) if runas.get("password_enc") else True
    modules_ok, missing = _check_runas_modules()

    return jsonify(
        platform_ok=True,
        configured=configured,
        password_ok=password_ok,
        modules_ok=modules_ok,
        missing=missing,
    )


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

# Persistente Teams-Keys in config.json["teams"]. Pfad-Defaults (db_path, log_path)
# werden bewusst NICHT mitgespeichert – sie hängen vom Instance-Pfad ab und werden
# zur Laufzeit aus der App-Config abgeleitet.
_TEAMS_CFG_PERSIST_KEYS = frozenset({
    "tenant_id", "client_id", "client_secret",
    "hash_size_limit_mb", "download_for_ooxml", "move_detection",
    "extensions", "teams",
    "blacklist_paths", "whitelist_paths",
})


def _teams_script_path() -> str:
    return os.path.join(_scanner_dir(), "teams_scanner.py")


def _default_teams_cfg() -> dict:
    """Laufzeit-Defaults inkl. abgeleiteter Pfade. Werden nur zur Anzeige und
    an den Teams-Scanner-Subprocess übergeben – NICHT in config.json persistiert.
    """
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
        "blacklist_paths":    [],
        "whitelist_paths":    [],
    }


def _load_teams_config() -> dict:
    """Lädt Teams-Konfiguration aus config.json["teams"] (Haupt-config.json).

    Die früher separate ``scanner/teams_config.json`` wurde in die Haupt-
    ``config.json`` konsolidiert (siehe run.py für die einmalige Migration).
    """
    from .. import config_store
    cfg = _default_teams_cfg()
    section = config_store.get_section("teams") or {}
    # Nur bekannte Keys übernehmen, damit Pfad-Defaults (db_path, log_path)
    # aus _default_teams_cfg() die Runtime-Werte behalten.
    for key in _TEAMS_CFG_PERSIST_KEYS:
        if key in section:
            cfg[key] = section[key]
    return cfg


def _save_teams_config(cfg: dict) -> None:
    """Speichert Teams-Konfiguration nach config.json["teams"].

    Nur persistente Keys werden geschrieben (keine Laufzeit-Pfade).
    """
    from .. import config_store
    to_persist = {k: v for k, v in cfg.items() if k in _TEAMS_CFG_PERSIST_KEYS}
    config_store.write_section("teams", to_persist)


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

        blacklist_paths_t = [p.strip() for p in request.form.get("blacklist_paths", "").splitlines() if p.strip()]
        whitelist_paths_t = [p.strip() for p in request.form.get("whitelist_paths", "").splitlines() if p.strip()]

        cfg.update({
            "tenant_id":          tenant_id,
            "client_id":          client_id,
            "client_secret":      client_secret,
            "extensions":         extensions or _DEFAULT_TEAMS_EXTENSIONS,
            "hash_size_limit_mb": hash_limit,
            "download_for_ooxml": download_ooxml,
            "move_detection":     move_det,
            "teams":              teams,
            "blacklist_paths":    blacklist_paths_t,
            "whitelist_paths":    whitelist_paths_t,
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

        # Haupt-config.json – der Teams-Scanner liest die "teams"-Sektion daraus.
        config_path = _scanner_config_path()
        if not os.path.isfile(config_path):
            return jsonify({"ok": False, "msg": "config.json nicht gefunden. Bitte zuerst speichern."})

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
                cwd=os.path.dirname(scanner_dir),
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
    smtp_log  = db.execute(
        "SELECT sent_at, recipients, subject, success, error_msg "
        "FROM smtp_log ORDER BY id DESC LIMIT 50"
    ).fetchall()
    from ..email_service import EMAIL_TEMPLATES as _email_tpls, _DEFAULTS as _email_defaults
    return render_template("admin/mail.html",
        settings=settings,
        email_templates=_email_tpls,
        email_defaults=_email_defaults,
        smtp_log=smtp_log)


@bp.route("/mail/test", methods=["POST"])
@admin_required
def mail_test():
    """AJAX-Endpunkt: Sendet eine Test-E-Mail und gibt JSON zurück.

    Liest die SMTP-Felder aus dem POST-Body (aktuelle Formularwerte),
    sodass der Test auch mit noch nicht gespeicherten Einstellungen funktioniert.
    Leeres Passwort-Feld bedeutet: gespeichertes DB-Passwort verwenden.
    """
    from ..email_service import send_smtp_test
    db       = get_db()
    to_email = request.form.get("to_email", "").strip()

    f_host  = request.form.get("smtp_host", "").strip() or None
    f_port  = request.form.get("smtp_port", "").strip()
    f_user  = request.form.get("smtp_user", "").strip()  # leer = kein Auth
    f_pw    = request.form.get("smtp_password", "")      # leer = DB-Wert behalten
    f_from  = request.form.get("smtp_from", "").strip() or None
    f_tls   = request.form.get("smtp_tls", None)         # 'starttls'|'ssl'|'none'

    ok, msg = send_smtp_test(
        db, to_email,
        host      = f_host,
        port      = int(f_port) if f_port else None,
        user      = f_user,
        password  = f_pw if f_pw else None,
        smtp_from = f_from,
        tls_mode  = f_tls if f_tls in ("starttls", "ssl", "none") else None,
    )
    return jsonify({"success": ok, "message": msg})


# ── Personen ───────────────────────────────────────────────────────────────

@bp.route("/person/neu", methods=["POST"])
@login_required
def new_person():
    db = get_db()
    try:
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
    except sqlite3.OperationalError as exc:
        current_app.logger.warning("new_person: Datenbank gesperrt: %s", exc)
        flash("Datenbank vorübergehend gesperrt, bitte in wenigen Sekunden erneut versuchen.", "error")
        return redirect(url_for("admin.index"))
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

        try:
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
        except sqlite3.OperationalError as exc:
            current_app.logger.warning("edit_person (pid=%s): Datenbank gesperrt: %s", pid, exc)
            flash("Datenbank vorübergehend gesperrt, bitte in wenigen Sekunden erneut versuchen.", "error")
            return redirect(url_for("admin.index"))
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
        ph, ph_params = in_clause(ids)
        db.execute(f"UPDATE persons SET aktiv=0 WHERE id IN ({ph})", ph_params)
        db.commit()
        flash(f"{len(ids)} Person(en) deaktiviert.", "success")

    elif action == "delete":
        import sqlite3 as _sq
        deleted = skipped = 0
        for pid in ids:
            try:
                db.execute("DELETE FROM persons WHERE id=?", (pid,))
                db.commit()
                deleted += 1
            except _sq.IntegrityError as exc:
                # VULN-011: FK-Verletzungen (person hat IDVs) sind erwartet,
                # aber andere DB-Fehler protokollieren wir.
                db.rollback()
                skipped += 1
                current_app.logger.info(
                    "Person %s nicht löschbar (FK-Constraint): %s", pid, exc
                )
            except _sq.DatabaseError as exc:
                db.rollback()
                skipped += 1
                current_app.logger.warning(
                    "Person %s: Datenbankfehler beim Löschen: %s", pid, exc
                )
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
        INSERT INTO org_units (bezeichnung, ebene, parent_id, created_at)
        VALUES (?,?,?,?)
    """, (
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
            UPDATE org_units SET bezeichnung=?, ebene=?, parent_id=?, aktiv=?
            WHERE id=?
        """, (
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
        ph, ph_params = in_clause(ids)
        db.execute(f"UPDATE geschaeftsprozesse SET aktiv=0 WHERE id IN ({ph})", ph_params)
        db.commit()
        flash(f"{len(ids)} Geschäftsprozess(e) deaktiviert.", "success")

    elif action == "delete":
        import sqlite3 as _sq
        deleted = skipped = 0
        for gid in ids:
            try:
                db.execute("UPDATE idv_register SET gp_id=NULL WHERE gp_id=?", (gid,))
                db.execute("DELETE FROM geschaeftsprozesse WHERE id=?", (gid,))
                db.commit()
                deleted += 1
            except _sq.DatabaseError as exc:
                db.rollback()
                skipped += 1
                current_app.logger.warning(
                    "Geschäftsprozess %s nicht löschbar: %s", gid, exc
                )
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
    except Exception as exc:
        # VULN-011: Fehler protokollieren; im Ausnahmefall lieber nicht
        # speichern als Klartext abzulegen.
        current_app.logger.error(
            "SMTP-Passwort-Verschlüsselung fehlgeschlagen: %s", exc
        )
        flash(
            "SMTP-Passwort konnte nicht verschlüsselt werden – nicht gespeichert.",
            "error",
        )
        return
    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
        ("smtp_password", enc),
    )


# ── Scanner-Log ───────────────────────────────────────────────────────────

def _resolve_scanner_log_path() -> str:
    """Liefert den absoluten Pfad zur Scanner-Log-Datei.

    Liest ``log_path`` aus der Scanner-Konfiguration (config.json).
    Relative Pfade werden – analog zur Logik im Scanner-Skript – gegen
    das Verzeichnis der ``config.json`` aufgelöst, sodass die Webapp
    dieselbe Datei findet wie der Scanner-Subprocess.
    """
    cfg = _load_scanner_config()
    log_path = cfg.get("log_path") or "instance/logs/idv_scanner.log"
    if not os.path.isabs(log_path):
        config_dir = os.path.dirname(os.path.abspath(_scanner_config_path()))
        log_path = os.path.normpath(os.path.join(config_dir, log_path))
    return log_path


def _resolve_scanner_output_log_path() -> str:
    """Liefert den Pfad zum stdout/stderr-Mitschnitt des Scanner-Subprocess.

    Diese Datei enthält Crash-Meldungen, die *vor* dem Initialisieren
    des Loggers auftreten (z. B. Fehler bei der WNet-Credential-
    Registrierung oder Tracebacks aus ``main()``).
    """
    return os.path.join(_instance_logs_dir(), "scanner_output.log")


def _resolve_app_log_path() -> str:
    """Liefert den Pfad zur Haupt-App-Log-Datei (idvault.log)."""
    return os.path.join(_instance_logs_dir(), "idvault.log")


def _resolve_scanner_crash_log_path() -> str:
    """Liefert den Pfad zur Crash-Log-Datei des Scanner-Subprocess.

    Wird von run.py geschrieben, wenn ``import idv_scanner`` oder
    ``idv_scanner.main()`` mit einer unbehandelten Ausnahme abbricht –
    also bevor stdout/stderr umgeleitet wurden.
    """
    return os.path.join(_instance_logs_dir(), "scanner_crash.log")


def _resolve_app_crash_log_path() -> str:
    """Liefert den Pfad zum App-Crash-Log (stderr-Umleitung in run.py, nur EXE).

    Enthält Python-Tracebacks und PyInstaller-Bootloader-Fehler des
    Hauptprozesses; wird nur im gebündelten EXE-Modus geschrieben.
    """
    return os.path.join(_instance_logs_dir(), "idvault_crash.log")


def _read_log_tail(path: str, max_lines: int = 1000) -> tuple:
    """Liest die letzten ``max_lines`` Zeilen einer Log-Datei.

    Gibt ``(lines, error_msg, mtime, size)`` zurück. ``lines`` ist eine
    Liste in Datei-Reihenfolge (älteste zuerst). Bei fehlender Datei wird
    ``([], "", None, 0)`` geliefert; bei Lesefehlern enthält ``error_msg``
    die Fehlermeldung.
    """
    if not path or not os.path.isfile(path):
        return [], "", None, 0
    try:
        st = os.stat(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if max_lines and len(lines) > max_lines:
            lines = lines[-max_lines:]
        return lines, "", st.st_mtime, st.st_size
    except Exception as exc:
        return [], str(exc), None, 0


@bp.route("/scanner/log")
@admin_required
def scanner_log():
    """Zeigt die letzten Einträge der Scanner-Log-Datei."""
    log_path        = _resolve_scanner_log_path()
    output_log_path = _resolve_scanner_output_log_path()

    lines, err, mtime, _size = _read_log_tail(log_path, max_lines=1000)
    if err:
        flash(f"Scan-Log konnte nicht gelesen werden: {err}", "error")

    from ..login_logger import get_log_path as _get_login_log_path
    login_log_path = _get_login_log_path()
    crash_log_path     = _resolve_scanner_crash_log_path()
    app_crash_log_path = _resolve_app_crash_log_path()
    app_log_path       = _resolve_app_log_path()
    output_exists      = os.path.isfile(output_log_path)
    login_log_exists   = os.path.isfile(login_log_path)
    crash_exists       = os.path.isfile(crash_log_path)
    app_crash_exists   = os.path.isfile(app_crash_log_path)
    app_log_exists     = os.path.isfile(app_log_path)

    return render_template("admin/scanner_log.html",
                           lines=lines,
                           log_path=log_path,
                           output_log_path=output_log_path,
                           login_log_path=login_log_path,
                           crash_log_path=crash_log_path,
                           app_crash_log_path=app_crash_log_path,
                           app_log_path=app_log_path,
                           output_exists=output_exists,
                           login_log_exists=login_log_exists,
                           crash_exists=crash_exists,
                           app_crash_exists=app_crash_exists,
                           app_log_exists=app_log_exists,
                           scan_running=_scan_is_running(),
                           log_mtime=mtime)


@bp.route("/scanner/log.txt")
@admin_required
def scanner_log_raw():
    """Liefert die letzten N Zeilen der Scanner-Log-Datei als Plaintext.

    Wird vom Log-Viewer per AJAX abgefragt, um die Anzeige live zu
    aktualisieren, während ein Scan läuft.

    Query-Parameter:
        lines  – Anzahl Zeilen (Standard: 500, Maximum: 5000)
        which  – ``scanner`` (Standard) oder ``output`` (stdout/stderr-
                 Mitschnitt des Subprocess)
    """
    try:
        max_lines = max(1, min(5000, int(request.args.get("lines", 500))))
    except ValueError:
        max_lines = 500

    which = request.args.get("which", "scanner")
    if which == "output":
        path = _resolve_scanner_output_log_path()
    elif which == "login":
        from ..login_logger import get_log_path as _get_login_log_path
        path = _get_login_log_path()
    elif which == "crash":
        path = _resolve_scanner_crash_log_path()
    elif which == "app_crash":
        path = _resolve_app_crash_log_path()
    elif which == "app":
        path = _resolve_app_log_path()
    else:
        path = _resolve_scanner_log_path()

    lines, err, _mtime, _size = _read_log_tail(path, max_lines=max_lines)
    if err:
        return Response(
            f"Fehler beim Lesen von {path}: {err}",
            status=500,
            mimetype="text/plain; charset=utf-8",
        )
    if not lines and not os.path.isfile(path):
        return Response(
            f"Log-Datei existiert noch nicht: {path}\n"
            f"(Wird beim ersten Scan-Start angelegt.)",
            mimetype="text/plain; charset=utf-8",
        )
    return Response("".join(lines), mimetype="text/plain; charset=utf-8")


# ── Login-Log ─────────────────────────────────────────────────────────────

@bp.route("/login-log")
@admin_required
def login_log():
    """Leitet auf den einheitlichen Log-Viewer weiter (Login-Log als Quelle)."""
    return redirect(url_for("admin.scanner_log", which="login"))


# ── Mitarbeiter-Import ─────────────────────────────────────────────────────

@bp.route("/import/personen", methods=["POST"])
@admin_required
@limiter.limit(_upload_rate_limit, methods=["POST"])
def import_persons():
    """CSV-Import: user_id, email (SMTP-Adresse), ad_name, oe_bezeichnung,
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

            user_id       = r.get("user_id") or r.get("userid") or r.get("benutzername") or ""
            email         = r.get("email") or r.get("smtp") or r.get("smtp_adresse") or r.get("mailadresse") or ""
            ad_name       = r.get("ad_name") or r.get("adname") or r.get("ad") or ""
            oe_bezeichnung = r.get("oe_bezeichnung") or r.get("oe") or r.get("abteilung") or ""
            nachname      = r.get("nachname") or r.get("name") or ""
            vorname       = r.get("vorname") or ""
            kuerzel       = (r.get("kuerzel") or user_id[:3]).upper()
            rolle         = r.get("rolle") or "Fachverantwortlicher"

            if not (nachname or user_id):
                errors += 1
                continue

            # OE auflösen
            org_unit_id = None
            if oe_bezeichnung:
                oe_row = db.execute(
                    "SELECT id FROM org_units WHERE LOWER(bezeichnung)=LOWER(?)", (oe_bezeichnung,)
                ).fetchone()
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
    content = "user_id;email;ad_name;oe_bezeichnung;nachname;vorname;kuerzel;rolle\n"
    content += "mmu;max.mustermann@bank.de;DOMAIN\\mmu;Kreditabteilung;Mustermann;Max;MMU;Fachverantwortlicher\n"
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
@limiter.limit(_upload_rate_limit, methods=["POST"])
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
        # Vor dem Schreiben die DB-Rohwerte lesen – überschriebene Felder
        # aus config.json dürfen NICHT in die DB propagiert werden, damit
        # die DB-Werte intakt bleiben, falls der Override später entfernt
        # wird. Siehe Plan: "Web-UI schreibt DB; config.json überschreibt".
        db_row = db.execute("SELECT * FROM ldap_config WHERE id = 1").fetchone()
        db_cfg = dict(db_row) if db_row else {}

        # Merged Config (inkl. _override_keys) für UI-konforme Logik
        effective = get_ldap_config(db) or {}
        overridden = set(effective.get("_override_keys") or [])

        def _field(name, form_val, coerce=lambda x: x, default=None):
            """Liefert den DB-seitig zu schreibenden Wert.

            Ist das Feld via config.json überschrieben, bleibt der bestehende
            DB-Wert unverändert; ansonsten wird der Formularwert genommen.
            """
            if name in overridden:
                return db_cfg.get(name, default)
            return coerce(form_val)

        enabled = _field("enabled",
                         1 if request.form.get("enabled") else 0,
                         default=0)
        server_url = _field("server_url",
                            request.form.get("server_url", "").strip(),
                            default="")
        try:
            port_form = int(request.form.get("port") or 636)
        except (TypeError, ValueError):
            port_form = 636
        port = _field("port", port_form, default=636)
        base_dn = _field("base_dn",
                         request.form.get("base_dn", "").strip(),
                         default="")
        bind_dn = _field("bind_dn",
                         request.form.get("bind_dn", "").strip(),
                         default="")
        user_attr = _field("user_attr",
                           request.form.get("user_attr", "sAMAccountName"),
                           default="sAMAccountName")
        ssl_verify = _field("ssl_verify",
                            1 if request.form.get("ssl_verify") else 0,
                            default=1)

        # Bind-Passwort: Override im config.json hat Vorrang. DB-Wert nur
        # ändern, wenn das Feld NICHT überschrieben ist und ein neues
        # Passwort eingegeben wurde.
        if "bind_password" in overridden:
            bind_password_enc = db_cfg.get("bind_password", "")
        else:
            bind_password_plain = request.form.get("bind_password", "").strip()
            if bind_password_plain:
                secret_key = current_app.config["SECRET_KEY"]
                bind_password_enc = encrypt_password(bind_password_plain, secret_key)
            else:
                bind_password_enc = db_cfg.get("bind_password", "")

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
        if overridden:
            flash(
                "LDAP-Konfiguration gespeichert. Hinweis: "
                f"{', '.join(sorted(overridden))} werden aktuell durch "
                "config.json überschrieben und wurden nicht verändert.",
                "success",
            )
        else:
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
    override_keys = list((cfg or {}).get("_override_keys") or [])
    return render_template("admin/ldap_config.html",
                           cfg=cfg, override_keys=override_keys)


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


def _sidecar_updates_enabled() -> bool:
    """Sidecar-ZIP-Updates können per config.json komplett abgeschaltet werden
    (VULN-B: reduziert den Admin-RCE-Vektor, wenn ZIP-Uploads nicht benötigt
    werden oder separate Change-Management-Prozesse verwendet werden)."""
    return bool(current_app.config.get("IDV_ALLOW_SIDECAR_UPDATES", True))


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
_SCANNER_TOPLEVEL_MODULES = frozenset({"idv_scanner", "idv_export"})


def _zip_remap(rel: str) -> str:
    """
    Mappt Pfade aus dem ZIP auf die Sidecar-Verzeichnisstruktur.

    Besonderheiten:
    - GitHub-ZIPs enthalten Templates unter ``webapp/templates/`` —
      der ChoiceLoader in ``run.py`` erwartet sie jedoch direkt unter
      ``updates/templates/``.
    - Scanner-Module (``idv_scanner``, ``idv_export``) werden im
      PyInstaller-Bundle als Top-Level-Module importiert
      (``pathex=['.', 'scanner']`` in ``idvault.spec``). Damit der
      Sidecar-Finder in ``run.py`` sie überhaupt findet, müssen sie
      flach unter ``updates/`` liegen – nicht unter ``updates/scanner/``.
    """
    if rel.startswith('webapp/templates/'):
        return rel[len('webapp/'):]   # → templates/...
    if rel.startswith('scanner/') and rel.endswith('.py'):
        # scanner/idv_scanner.py → idv_scanner.py, sofern das
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
    # VULN-B: Opt-out über config.json. Wenn IDV_ALLOW_SIDECAR_UPDATES=0
    # gesetzt ist, wird der Endpoint komplett abgewiesen – das reduziert
    # den Admin-RCE-Vektor in Umgebungen, in denen Sidecar-Updates nicht
    # gebraucht werden.
    if not _sidecar_updates_enabled():
        current_app.logger.warning(
            "Sidecar-Update-Upload blockiert (IDV_ALLOW_SIDECAR_UPDATES=0)"
        )
        flash(
            "Sidecar-Updates sind deaktiviert (config.json → "
            "IDV_ALLOW_SIDECAR_UPDATES=0). Zum Aktivieren bitte Wert auf 1 setzen "
            "und neu starten.",
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
      1. IDV_SERVICE_NAME aus config.json / Umgebungsvariable (explizit)
      2. Automatische Erkennung via EnumServicesStatusEx (nativ registriert)
    """
    explicit = os.environ.get('IDV_SERVICE_NAME', '').strip()
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
