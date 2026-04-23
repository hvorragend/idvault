"""Scanner-Konfiguration, Scan-Trigger, Teams-Scanner und Log-Anzeige."""
import os
import sys
import json
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta

from flask import render_template, request, redirect, url_for, flash, Response, jsonify, current_app

from . import bp, _upload_rate_limit
from .. import login_required, admin_required, write_access_required, get_db
from ...db_writer import get_writer
from ... import limiter

from db_write_tx import write_tx
from db import (
    apply_scan_run_start,
    apply_scan_run_end,
    apply_scanner_upsert_file,
    apply_scanner_upsert_file_batch,
    apply_scanner_archive_files,
    apply_scanner_archive_unseen,
    apply_scanner_update_status,
    apply_scanner_history,
    apply_scanner_save_delta_token,
)
try:
    from scanner.scanner_protocol import (
        OP_START_RUN, OP_END_RUN, OP_UPSERT_FILE, OP_MOVE_FILE,
        OP_ARCHIVE_FILES, OP_ARCHIVE_UNSEEN,
        OP_UPDATE_STATUS, OP_FILE_HISTORY,
        OP_LOG, OP_PROGRESS, OP_SAVE_DELTA_TOKEN,
    )
except ImportError:  # pragma: no cover — Fallback, falls scanner/ nicht
    # als Paket gefunden wird (alte Installationen ohne Namespace).
    from scanner_protocol import (
        OP_START_RUN, OP_END_RUN, OP_UPSERT_FILE, OP_MOVE_FILE,
        OP_ARCHIVE_FILES, OP_ARCHIVE_UNSEEN,
        OP_UPDATE_STATUS, OP_FILE_HISTORY,
        OP_LOG, OP_PROGRESS, OP_SAVE_DELTA_TOKEN,
    )


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


def _scanner_script_path():
    return os.path.join(_scanner_dir(), "network_scanner.py")


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
    "scan_paths", "extensions",
    "hash_size_limit_mb", "max_workers", "move_detection", "scan_since", "read_file_owner",
    "blacklist_paths", "whitelist_paths",
    "parallel_shares",
})


def _default_scanner_cfg() -> dict:
    """Erstellt die Standardkonfiguration für einen frisch installierten
    Scanner. Pfade (db_path/log_path) werden vom Scanner-Subprozess aus dem
    ``--db-path``-Argument abgeleitet und sind deshalb nicht Teil der
    persistierten Scanner-Config."""
    return {
        "scan_paths": [],
        "extensions": _DEFAULT_SCANNER_EXTENSIONS,
        "blacklist_paths": _DEFAULT_SCANNER_EXCLUDE,
        "whitelist_paths": [],
        "hash_size_limit_mb": 500,
        "max_workers": 4,
        "move_detection": "name_and_hash",
        "scan_since": None,
        "read_file_owner": True,
        "parallel_shares": 1,
    }


def _load_scanner_config() -> dict:
    """Lädt Scanner-Config aus ``app_settings['scanner_config']``."""
    from ... import app_settings as _aps
    cfg = _default_scanner_cfg()
    data = _aps.get_scanner_config(get_db())
    cfg.update({k: v for k, v in data.items() if k in _SCANNER_CFG_KEYS})
    return cfg


def _save_scanner_config(cfg: dict):
    from ... import app_settings as _aps
    to_persist = {k: v for k, v in cfg.items() if k in _SCANNER_CFG_KEYS}
    _aps.set_scanner_config(get_db(), to_persist)


def _load_path_mappings() -> list:
    """Lädt path_mappings aus ``app_settings['path_mappings']``."""
    from ... import app_settings as _aps
    return _aps.get_path_mappings(get_db())


def _save_path_mappings(mappings: list) -> None:
    """Speichert path_mappings nach ``app_settings['path_mappings']`` und
    aktualisiert den App-Config-Cache (``PATH_MAPPINGS``)."""
    from ... import app_settings as _aps
    _aps.set_path_mappings(get_db(), mappings)
    try:
        current_app.config["PATH_MAPPINGS"] = list(mappings or [])
    except RuntimeError:
        pass


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
                from ...ldap_auth import decrypt_password
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


_UPSERT_BATCH_SIZE = 500

_SCANNER_EVENT_HANDLERS = {
    OP_START_RUN:        apply_scan_run_start,
    OP_END_RUN:          apply_scan_run_end,
    OP_ARCHIVE_FILES:    apply_scanner_archive_files,
    OP_ARCHIVE_UNSEEN:   apply_scanner_archive_unseen,
    OP_UPDATE_STATUS:    apply_scanner_update_status,
    OP_FILE_HISTORY:     apply_scanner_history,
    OP_SAVE_DELTA_TOKEN: apply_scanner_save_delta_token,
}


def _dispatch_scanner_event(op: str, payload: dict) -> None:
    """Reicht ein vom Scanner emittiertes Event an den Writer-Thread weiter.

    ``OP_LOG`` / ``OP_PROGRESS`` und unbekannte Ops werden ignoriert – sie
    dienen nur der Anzeige und werden vom Stdout-Reader direkt in das
    Output-Log geschrieben, sofern sie dort landen sollen.
    """
    handler = _SCANNER_EVENT_HANDLERS.get(op)
    if handler is None:
        return
    get_writer().submit(
        lambda c, _h=handler, _p=payload: _h(c, _p),
        wait=False,
    )


def _flush_upsert_buffer(buf: list, log_fh) -> None:
    """Reicht den gepufferten Upsert-Batch als einzelnen Writer-Job ein."""
    if not buf:
        return
    batch = buf[:]
    buf.clear()
    try:
        get_writer().submit(
            lambda c, _b=batch: apply_scanner_upsert_file_batch(c, _b),
            wait=False,
        )
    except Exception as exc:
        try:
            log_fh.write(f"[scanner-batch-error] {exc}\n")
        except Exception:
            pass


def _stdout_reader_thread(proc, log_fh, state: dict) -> None:
    """Liest NDJSON-Events aus ``proc.stdout`` und dispatcht sie ueber den
    Writer-Thread. upsert_file- und move_file-Events werden in Batches von
    ``_UPSERT_BATCH_SIZE`` zusammengefasst, damit je Batch nur eine SQLite-
    Transaktion anfaellt. Alle anderen Events leeren zunaechst den Puffer,
    um die Reihenfolge zu wahren. Zeilen ohne gueltiges JSON wandern ins
    Output-Log."""
    upsert_buf: list = []
    try:
        for raw in iter(proc.stdout.readline, ""):
            line = raw.rstrip("\r\n")
            if not line:
                continue
            payload = None
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except Exception:
                    payload = None
            if isinstance(payload, dict) and "op" in payload:
                op = payload.pop("op")
                if op == OP_START_RUN:
                    state["scan_run_id"] = payload.get("scan_run_id")
                elif op == OP_END_RUN:
                    state["end_run_seen"] = True

                if op in (OP_UPSERT_FILE, OP_MOVE_FILE):
                    upsert_buf.append(payload)
                    if len(upsert_buf) >= _UPSERT_BATCH_SIZE:
                        _flush_upsert_buffer(upsert_buf, log_fh)
                else:
                    # Puffer leeren, bevor ein anderes Event verarbeitet wird,
                    # damit z. B. archive_files auf bereits geschriebene Zeilen trifft.
                    _flush_upsert_buffer(upsert_buf, log_fh)
                    try:
                        _dispatch_scanner_event(op, payload)
                    except Exception as exc:
                        try:
                            log_fh.write(f"[scanner-event-error] op={op}: {exc}\n")
                        except Exception:
                            pass
            else:
                try:
                    log_fh.write(line + "\n")
                except Exception:
                    pass
    except Exception as exc:
        try:
            log_fh.write(f"[stdout-reader-error] {exc}\n")
        except Exception:
            pass
    finally:
        _flush_upsert_buffer(upsert_buf, log_fh)


def _stderr_reader_thread(proc, log_fh) -> None:
    """Leitet stderr des Scanner-Subprozesses in das Output-Log."""
    try:
        for raw in iter(proc.stderr.readline, ""):
            line = raw.rstrip("\r\n")
            if not line:
                continue
            try:
                log_fh.write(line + "\n")
            except Exception:
                pass
    except Exception:
        pass


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
    #
    # stdout wird via PIPE eingelesen, weil der Scanner seit dem db_writer-
    # Pattern seine DB-Schreibvorgaenge als NDJSON auf stdout emittiert;
    # ein Reader-Thread parst jede Zeile und reicht sie an den Writer-
    # Thread der Webapp weiter. Nicht-JSON-Zeilen (sowie der komplette
    # stderr-Strom) werden direkt ins Output-Log gespiegelt.
    log_fh = open(log_path, "a", encoding="utf-8", buffering=1)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            creationflags=creationflags,
            env=_scanner_subprocess_env(),
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        log_fh.close()
        _cancel_unc_credentials(registered_unc)
        raise

    state = {"end_run_seen": False, "scan_run_id": None}
    stdout_thread = threading.Thread(
        target=_stdout_reader_thread,
        args=(proc, log_fh, state),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_stderr_reader_thread,
        args=(proc, log_fh),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    def _wait():
        try:
            proc.wait()
        finally:
            # Reader-Threads bis zum EOF drainen lassen, bevor wir das Log
            # schliessen – sonst gehen End-of-Scan-Zeilen verloren.
            try:
                stdout_thread.join(timeout=5)
            except Exception:
                pass
            try:
                stderr_thread.join(timeout=5)
            except Exception:
                pass

            # Ist der Scanner beendet worden, ohne einen OP_END_RUN zu
            # emittieren (Kill -9, OOM-Killer, Hardware-Reset …), bleibt
            # der scan_runs-Eintrag sonst ewig auf 'running'. In diesem
            # Fall setzen wir den Lauf synthetisch auf 'killed'.
            if not state["end_run_seen"] and state["scan_run_id"] is not None:
                scan_run_id = state["scan_run_id"]
                try:
                    log_fh.write(
                        f"[IDVAULT-END] Scanner-Prozess endete ohne "
                        f"end_run-Event (Run #{scan_run_id}) – "
                        f"synthetisiere status='killed'.\n"
                    )
                except Exception:
                    pass
                try:
                    finished_at = datetime.now(timezone.utc).isoformat()
                    get_writer().submit(
                        lambda c, _id=scan_run_id, _fin=finished_at:
                            apply_scan_run_end(c, {
                                "scan_run_id": _id,
                                "finished_at": _fin,
                                "status":      "killed",
                                "total":       0,
                                "new":         0,
                                "changed":     0,
                                "moved":       0,
                                "restored":    0,
                                "archived":    0,
                                "errors":      0,
                            }),
                        wait=False,
                    )
                except Exception:
                    pass

            try:
                log_fh.close()
            except Exception:
                pass
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

        db_path = current_app.config["DATABASE"]

        scanner_dir = _scanner_dir()
        os.makedirs(scanner_dir, exist_ok=True)

        for sig_name in ("scanner_pause.signal", "scanner_cancel.signal"):
            try:
                os.remove(os.path.join(scanner_dir, sig_name))
            except FileNotFoundError:
                pass

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--scan", "--db-path", db_path,
                   "--signal-dir", scanner_dir]
        else:
            script = _scanner_script_path()
            if not os.path.isfile(script):
                current_app.logger.error(
                    "Zeitplan-Scan: Scanner-Skript nicht gefunden: %s", script
                )
                return False
            cmd = [sys.executable, script, "--db-path", db_path,
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
                                        def _do(c, _today=today_str):
                                            with write_tx(c):
                                                c.execute(
                                                    "INSERT OR REPLACE INTO app_settings "
                                                    "(key, value) VALUES "
                                                    "('scan_schedule_last_triggered_date', ?)",
                                                    (_today,)
                                                )
                                        get_writer().submit(_do, wait=True)
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
        try:
            parallel_shares = max(1, min(8, int(request.form.get("parallel_shares", 1))))
        except ValueError:
            parallel_shares = 1

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
            "parallel_shares":   parallel_shares,
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
            val_ai = "1" if request.form.get("auto_ignore_no_formula") == "1" else "0"
            val_dc = "1" if request.form.get("discard_no_formula") == "1" else "0"
            val_cf = "1" if request.form.get("auto_classify_by_filename") == "1" else "0"
            val_ms = "1" if request.form.get("match_suggestions_enabled") == "1" else "0"
            val_sd = "1" if request.form.get("smart_defaults_enabled") == "1" else "0"
            _settings = [
                ("auto_ignore_no_formula",       val_ai),
                ("discard_no_formula",           val_dc),
                ("auto_classify_by_filename",    val_cf),
                ("match_suggestions_enabled",    val_ms),
                ("smart_defaults_enabled",       val_sd),
                ("scan_schedule_enabled",     sched_enabled),
                ("scan_schedule_type",        sched_type),
                ("scan_schedule_time",        sched_time),
                ("scan_schedule_weekday",     sched_weekday),
            ]
            def _do(c):
                with write_tx(c):
                    for _key, _val in _settings:
                        c.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)",
                                  (_key, _val))
            get_writer().submit(_do, wait=True)
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
    match_sug = db.execute(
        "SELECT value FROM app_settings WHERE key='match_suggestions_enabled'"
    ).fetchone()
    smart_def = db.execute(
        "SELECT value FROM app_settings WHERE key='smart_defaults_enabled'"
    ).fetchone()
    schedule = _load_schedule_settings(db)
    runas = _load_scanner_runas()
    return render_template("admin/scanner_einstellungen.html",
                           cfg=cfg, scan_running=_scan_is_running(),
                           path_mappings=path_mappings,
                           auto_ignore_no_formula=(auto_ignore["value"] if auto_ignore else "0"),
                           discard_no_formula=(discard_nf["value"] if discard_nf else "0"),
                           auto_classify_by_filename=(classify_fn["value"] if classify_fn else "0"),
                           match_suggestions_enabled=(match_sug["value"] if match_sug else "1"),
                           smart_defaults_enabled=(smart_def["value"] if smart_def else "1"),
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

        db_path = current_app.config["DATABASE"]

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
            cmd = [sys.executable, "--scan", "--db-path", db_path,
                   "--signal-dir", scanner_dir]
        else:
            script = _scanner_script_path()
            if not os.path.isfile(script):
                return jsonify({"ok": False, "msg": f"Scanner-Skript nicht gefunden: {script}"})
            cmd = [sys.executable, script, "--db-path", db_path,
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
    from ...ldap_auth import encrypt_password

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

    _runas_settings = [
        ("scanner_runas_domain",   domain),
        ("scanner_runas_username", username),
        ("scanner_runas_password", password_enc),
    ]
    def _do(c):
        with write_tx(c):
            for _key, _val in _runas_settings:
                c.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                    (_key, _val),
                )
    get_writer().submit(_do, wait=True)

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

    def _do(c, _cutoff=cutoff):
        with write_tx(c):
            hist = c.execute(
                "DELETE FROM idv_file_history WHERE changed_at < ?", (_cutoff,)
            ).rowcount
            runs = c.execute("""
                DELETE FROM scan_runs
                WHERE started_at < ?
                  AND id NOT IN (
                      SELECT DISTINCT last_scan_run_id FROM idv_files
                      WHERE last_scan_run_id IS NOT NULL
                  )
            """, (_cutoff,)).rowcount
        return hist, runs
    hist_count, runs_count = get_writer().submit(_do, wait=True)
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

    now = datetime.now(timezone.utc).isoformat()

    try:
        src_runs  = [dict(r) for r in src.execute("SELECT * FROM scan_runs ORDER BY id").fetchall()]
        src_files = [dict(r) for r in src.execute("SELECT * FROM idv_files ORDER BY id").fetchall()]
        src_hist  = [dict(r) for r in src.execute("SELECT * FROM idv_file_history ORDER BY id").fetchall()]
    except Exception as exc:
        src.close()
        flash(f"Fehler beim Lesen der Quelldatenbank: {exc}", "error")
        return redirect(url_for("admin.scanner_einstellungen") + "#db-import")
    finally:
        src.close()

    def _do(c):
        stats = {"runs": 0, "files_new": 0, "files_updated": 0, "history": 0}
        run_id_map = {}
        file_id_map = {}
        with write_tx(c):
            for run in src_runs:
                cur = c.execute("""
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

            for f in src_files:
                existing = c.execute(
                    "SELECT id, last_seen_at FROM idv_files WHERE full_path = ?",
                    (f["full_path"],)
                ).fetchone()

                if existing is None:
                    cur = c.execute("""
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
                    file_id_map[f["id"]] = existing["id"]
                    src_ts  = f["last_seen_at"] or ""
                    dst_ts  = existing["last_seen_at"] or ""
                    if src_ts > dst_ts:
                        c.execute("""
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

            for h in src_hist:
                dst_file_id = file_id_map.get(h["file_id"])
                dst_run_id  = run_id_map.get(h["scan_run_id"])
                if dst_file_id is None or dst_run_id is None:
                    continue
                c.execute("""
                    INSERT INTO idv_file_history
                        (file_id, scan_run_id, change_type, old_hash, new_hash, changed_at, details)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    dst_file_id, dst_run_id,
                    h["change_type"], h["old_hash"], h["new_hash"],
                    h["changed_at"], h["details"],
                ))
                stats["history"] += 1
        return stats

    try:
        stats = get_writer().submit(_do, wait=True)
        flash(
            f"Import abgeschlossen: {stats['runs']} Scan-Läufe, "
            f"{stats['files_new']} neue Dateien, "
            f"{stats['files_updated']} aktualisiert, "
            f"{stats['history']} History-Einträge.",
            "success"
        )
    except Exception as exc:
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

# Persistente Teams-Keys in app_settings['teams_config'] (JSON-Blob). Das
# client_secret wird separat Fernet-verschlüsselt in
# app_settings['teams_client_secret_enc'] abgelegt (siehe
# ``webapp/app_settings.py``).
_TEAMS_CFG_PERSIST_KEYS = frozenset({
    "tenant_id", "client_id",
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
    """Lädt Teams-Konfiguration aus app_settings + Fernet-entschlüsseltes
    client_secret. Ergänzt Runtime-Pfade (db_path/log_path) aus der App-Config."""
    from ... import app_settings as _aps
    cfg = _default_teams_cfg()
    db = get_db()
    section = _aps.get_teams_config(db)
    for key in _TEAMS_CFG_PERSIST_KEYS:
        if key in section:
            cfg[key] = section[key]
    cfg["client_secret"] = _aps.get_teams_client_secret(db)
    return cfg


def _save_teams_config(cfg: dict) -> None:
    """Speichert Teams-Konfiguration nach app_settings + Fernet-verschlüsseltes
    client_secret. Leerer Secret-Wert behält den bestehenden Wert (Admin-UI
    zeigt das Klartext-Passwort nicht an)."""
    from ... import app_settings as _aps
    db = get_db()
    to_persist = {k: v for k, v in cfg.items() if k in _TEAMS_CFG_PERSIST_KEYS}
    _aps.set_teams_config(db, to_persist)
    secret = cfg.get("client_secret")
    # Nur setzen wenn das Formular ein neues Secret liefert – Leerstring
    # erlaubt gezieltes Löschen über ein Flag (wird derzeit nicht genutzt).
    if secret:
        _aps.set_teams_client_secret(db, secret)


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

        db_path = current_app.config["DATABASE"]
        cmd = [sys.executable, script, "--db-path", db_path]
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

# ── Scanner-Log ───────────────────────────────────────────────────────────

def _resolve_scanner_log_path() -> str:
    """Liefert den absoluten Pfad zur Scanner-Log-Datei.

    Der Scanner-Subprozess leitet den Log-Pfad aus ``--db-path`` ab
    (``<db_parent>/logs/network_scanner.log``). Die Webapp nutzt
    dieselbe Logik, damit Scanner-Log-Viewer und Subprozess auf dieselbe
    Datei zeigen.
    """
    return os.path.join(_instance_logs_dir(), "network_scanner.log")


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

    Wird von run.py geschrieben, wenn ``import network_scanner`` oder
    ``network_scanner.main()`` mit einer unbehandelten Ausnahme abbricht –
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

    from ...login_logger import get_log_path as _get_login_log_path
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
        from ...login_logger import get_log_path as _get_login_log_path
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
