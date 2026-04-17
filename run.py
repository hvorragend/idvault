"""
idvault – Startpunkt
====================
Entwicklung:  python run.py
Produktion:   gunicorn "run:app" --workers 2 --bind 0.0.0.0:5000

Konfiguration (config.json):
  Beim ersten Start wird config.json mit einem zufälligen SECRET_KEY
  automatisch angelegt (falls weder Datei noch Env-Variable vorhanden).
  Vorlage: config.json.example → config.json kopieren und anpassen.

  OS-Umgebungsvariablen haben immer Vorrang über config.json.

Umgebungsvariablen (alternativ zu config.json oder als Override):
  SECRET_KEY       Flask Session Secret      (Pflicht in Produktion!)
  PORT             Netzwerkport              (Standard: 5000 / 5443 bei HTTPS)
  DEBUG            1 = Debug-Modus           (Standard: 0)
  IDV_HTTPS        1 = HTTPS aktivieren      (Standard: 0)
  IDV_SSL_CERT     Pfad zum Zertifikat (PEM) (Standard: instance/certs/cert.pem)
  IDV_SSL_KEY      Pfad zum priv. Schlüssel  (Standard: instance/certs/key.pem)
  IDV_SSL_AUTOGEN  1 = Selbstsigniertes Zertifikat erzeugen, falls fehlend
                                             (Standard: 1)
  IDV_DB_PATH      Pfad zur SQLite-Datenbank (Standard: instance/idvault.db)
"""

import os
import sys

# Projektverzeichnis zum Pfad hinzufügen
sys.path.insert(0, os.path.dirname(__file__))

# Absoluter Projektpfad – muss gesetzt sein bevor irgendein Modul geladen wird,
# damit create_app() korrekte Pfade ermittelt auch wenn webapp/__init__.py
# aus dem Sidecar-Override stammt.
if getattr(sys, 'frozen', False):
    _PROJECT_ROOT = os.path.dirname(sys.executable)
else:
    _PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault('IDV_PROJECT_ROOT', _PROJECT_ROOT)

# ── Fehler-Log: stderr → Datei umleiten (nur als EXE) ───────────────────────
# Schreibt Python-Tracebacks und PyInstaller-Bootloader-Fehler nach
# instance/idvault_crash.log (getrennt vom Flask-App-Log, damit der
# RotatingFileHandler in webapp/__init__.py die idvault.log ohne Windows-
# Dateisperren rotieren kann).
# Einfaches Logrotate: Datei > 2 MB wird vor dem Öffnen zu .1 umbenannt.
#
# WICHTIG: Im Scanner-Subprocess (--scan) NICHT umleiten. Der Parent setzt via
# subprocess.Popen(stdout/stderr=…) bereits scanner_output.log; ein dup2 hier
# würde das überschreiben und Scanner-Logs (StreamHandler → sys.stderr) landen
# fälschlich in idvault_crash.log. Der Scanner hat zudem sein eigenes
# Crash-Log (scanner_crash.log, siehe unten).
if getattr(sys, 'frozen', False) and '--scan' not in sys.argv:
    _log_dir = os.path.join(_PROJECT_ROOT, 'instance', 'logs')
    os.makedirs(_log_dir, exist_ok=True)
    try:
        _crash_log_path = os.path.join(_log_dir, 'idvault_crash.log')
        _crash_log_bak  = _crash_log_path + '.1'
        _MAX_CRASH_LOG  = 2 * 1024 * 1024  # 2 MB
        if os.path.exists(_crash_log_path) and \
                os.path.getsize(_crash_log_path) > _MAX_CRASH_LOG:
            if os.path.exists(_crash_log_bak):
                os.remove(_crash_log_bak)
            os.rename(_crash_log_path, _crash_log_bak)
        _log_fh = open(
            _crash_log_path,
            'a', encoding='utf-8', errors='replace', buffering=1
        )
        sys.stderr = _log_fh
        os.dup2(_log_fh.fileno(), 2)   # C-Level fd 2 → Datei (PyInstaller-Fehler)
    except Exception:
        pass  # Logging-Fehler dürfen den Start nicht verhindern
# ─────────────────────────────────────────────────────────────────────────────

# ── Windows: Konsolen-X-Button sauber behandeln ──────────────────────────────
# Werkzeug/Flask fängt CTRL_C_EVENT (Signal 2) über signal.SIGINT ab, aber
# CTRL_CLOSE_EVENT (Console-X-Button, Wert 2 im Win32-API) wird separat über
# SetConsoleCtrlHandler zugestellt und vom Werkzeug-Dev-Server nicht verarbeitet.
# Ohne Handler lässt Windows den Prozess nach ~5 s als "hängend" erscheinen und
# bietet nur „Prozess beenden" an – das X scheint wirkungslos.
# Lösung: Eigenen Handler registrieren, der sofort os._exit(0) ruft.
if getattr(sys, 'frozen', False) and os.name == 'nt' and '--scan' not in sys.argv \
        and '--service-run' not in sys.argv:
    import ctypes
    import ctypes.wintypes as _cwt
    _CTRL_CLOSE_EVENT = 2

    @ctypes.WINFUNCTYPE(_cwt.BOOL, _cwt.DWORD)
    def _win_ctrl_handler(ctrl_type):
        if ctrl_type == _CTRL_CLOSE_EVENT:
            os._exit(0)
        return False

    ctypes.windll.kernel32.SetConsoleCtrlHandler(_win_ctrl_handler, True)
# ─────────────────────────────────────────────────────────────────────────────

# ── Sidecar-Update: Override-Verzeichnis vor gebündelten Modulen laden ───────
import importlib.util


class _SidecarFinder:
    """Lädt .py-Dateien aus updates/ vor dem PyInstaller-FrozenImporter."""
    def __init__(self, base):
        self._base = base

    def find_spec(self, fullname, path, target=None):
        rel = fullname.replace('.', os.sep)
        candidate = os.path.join(self._base, rel + '.py')
        if os.path.isfile(candidate):
            return importlib.util.spec_from_file_location(fullname, candidate)
        # Package-__init__.py NICHT aus dem Sidecar laden: Pakete müssen aus dem
        # Bundle stammen damit gebündelte C-Extensions (z.B. unicodedata.pyd)
        # korrekt gefunden werden.
        return None


def _get_updates_dir():
    base = (os.path.dirname(sys.executable) if getattr(sys, 'frozen', False)
            else os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, 'updates')
    return p if os.path.isdir(p) else None


import json as _json


def _read_version_json(path: str) -> dict:
    try:
        with open(path, encoding='utf-8') as _f:
            return _json.load(_f)
    except Exception:
        return {}


# ── config.json laden / beim ersten Start automatisch anlegen ────────────────
# config.json liegt neben run.py bzw. neben der EXE und enthält dieselben
# Schlüssel wie die Umgebungsvariablen (SECRET_KEY, PORT, IDV_HTTPS, …).
# Werte werden nur gesetzt, wenn die Variable noch NICHT in der Umgebung steht
# → OS-Umgebungsvariablen, Docker-Secrets usw. haben immer Vorrang.
_config_file = os.path.join(_PROJECT_ROOT, 'config.json')
if not os.path.isfile(_config_file):
    # Erster Start ohne config.json und ohne SECRET_KEY-Env-Variable:
    # Datei automatisch mit zufälligem SECRET_KEY anlegen.
    if not os.environ.get('SECRET_KEY'):
        import secrets as _secrets
        # Hinweis zu SMTP/LDAP-Credentials:
        #   SMTP und LDAP werden über die Web-UI in der SQLite-Datenbank
        #   gespeichert (Tabellen app_settings bzw. ldap_config). Die
        #   config.json kann sie OPTIONAL überschreiben – hier im Auto-
        #   Template bewusst NICHT vorbelegt, damit die in schema.sql
        #   hinterlegten DB-Defaults greifen, solange kein Override nötig ist.
        #   Siehe config.json.example für Override-Beispiele.
        _auto_cfg = {
            "SECRET_KEY": _secrets.token_hex(32),
            "PORT": 5000,
            "IDV_HTTPS": 0,
            "IDV_SSL_CERT": "instance/certs/cert.pem",
            "IDV_SSL_KEY": "instance/certs/key.pem",
            "IDV_SSL_AUTOGEN": 1,
            "IDV_DB_PATH": "instance/idvault.db",
            "IDV_INSTANCE_PATH": "instance",
            # VULN-B: Sidecar-Update-Upload (Admin kann Python/Template-Dateien
            # als ZIP hochladen, die beim nächsten Start die gebündelten Module
            # überschreiben) – kann durch Setzen auf 0 komplett deaktiviert werden.
            "IDV_ALLOW_SIDECAR_UPDATES": 1,
            # VULN-F: Lokale Benutzer. Leere Liste = kein lokaler Login möglich
            # (nur LDAP). Pro Eintrag entweder 'password_hash' (empfohlen,
            # werkzeug-Format) oder 'password' (Klartext, optional – wird beim
            # Start automatisch gehasht). Beispiel siehe config.json.example.
            "IDV_LOCAL_USERS": [],
            # VULN-J: Login-Rate-Limit (Flask-Limiter-Syntax).
            "IDV_LOGIN_RATE_LIMIT": "5 per minute;30 per hour",
            # VULN-009: Rate-Limit für Admin-Uploads (ZIP, CSV).
            "IDV_UPLOAD_RATE_LIMIT": "10 per minute;60 per hour",
            "scanner": {
                "scan_paths": [],
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
                    "~$", ".tmp",
                    "$RECYCLE.BIN",
                    "System Volume Information",
                    "AppData"
                ],
                # Pfade bewusst relativ – die idv_scanner-Routine löst sie
                # gegen das Verzeichnis der config.json (= Projekt-Root) auf.
                # Vorteil: portable Installation und gut lesbare config.json.
                "db_path": "instance/idvault.db",
                "log_path": "instance/logs/idv_scanner.log",
                "hash_size_limit_mb": 500,
                "max_workers": 4,
                "move_detection": "name_and_hash",
                "scan_since": None,
                "read_file_owner": True
            },
            "teams": {
                "tenant_id": "",
                "client_id": "",
                "client_secret": "",
                "extensions": [
                    ".xls", ".xlsx", ".xlsm", ".xlsb", ".xltm", ".xltx",
                    ".accdb", ".mdb", ".accde", ".accdr",
                    ".ida", ".idv",
                    ".pbix", ".pbit",
                    ".dotm", ".pptm",
                    ".py", ".r", ".rmd", ".sql"
                ],
                "hash_size_limit_mb": 100,
                "download_for_ooxml": True,
                "move_detection": "name_and_hash",
                "teams": []
            },
            # OPTIONAL: Override der LDAP-Konfiguration aus der DB.
            #
            # Der Block unten ist mit Unterstrich-Präfix als INAKTIVES
            # Beispiel eingetragen – die App wertet nur den Schlüssel "ldap"
            # aus. Zum Aktivieren:
            #   1. "_ldap_beispiel" in "ldap" umbenennen.
            #   2. Felder anpassen (nur die gesetzten Keys überschreiben die DB,
            #      fehlende Keys bleiben aus /admin/ldap-config aktiv).
            #   3. bind_password entweder als Klartext oder als
            #      "ENV:VARNAME" referenzieren – letzteres ist für
            #      Produktionsumgebungen empfohlen.
            # Überschriebene Felder werden in der Web-UI read-only angezeigt.
            "_kommentar_ldap": (
                "Optionaler Override für /admin/ldap-config. "
                "Zum Aktivieren '_ldap_beispiel' in 'ldap' umbenennen. "
                "Siehe config.json.example für Details."
            ),
            "_ldap_beispiel": {
                "enabled": True,
                "server_url": "ldaps://ad.example.com",
                "port": 636,
                "base_dn": "DC=example,DC=com",
                "bind_dn": "CN=svc-idvault,OU=ServiceAccounts,DC=example,DC=com",
                "bind_password": "ENV:IDV_LDAP_BIND_PASSWORD",
                "user_attr": "sAMAccountName",
                "ssl_verify": True
            }
        }
        try:
            with open(_config_file, 'w', encoding='utf-8') as _f:
                _json.dump(_auto_cfg, _f, indent=2, ensure_ascii=False)
            print(f"  [config] Neue config.json angelegt: {_config_file}")
        except Exception:
            pass  # Schreibfehler dürfen den Start nicht verhindern

# ── Einmalige Migration: scanner/teams_config.json → config.json["teams"] ──
# Frühere Versionen hielten die Teams-Konfiguration in einer separaten Datei.
# Wenn sie noch existiert und in config.json kein "teams"-Block steht,
# übernehmen wir den Inhalt. Die alte Datei wird sicherheitshalber nur
# umbenannt (nicht gelöscht), damit ein Admin die Migration nachvollziehen
# kann.
try:
    _teams_legacy = os.path.join(_PROJECT_ROOT, 'scanner', 'teams_config.json')
    if os.path.isfile(_teams_legacy) and os.path.isfile(_config_file):
        _main_cfg = _read_version_json(_config_file)
        if isinstance(_main_cfg, dict) and "teams" not in _main_cfg:
            _legacy_cfg = _read_version_json(_teams_legacy)
            if isinstance(_legacy_cfg, dict):
                # Pfad-Defaults (db_path, log_path) NICHT mit-migrieren – die
                # werden zur Laufzeit aus dem Instance-Pfad abgeleitet.
                _persist_keys = {
                    "tenant_id", "client_id", "client_secret",
                    "hash_size_limit_mb", "download_for_ooxml",
                    "move_detection", "extensions", "teams",
                }
                _main_cfg["teams"] = {
                    k: v for k, v in _legacy_cfg.items() if k in _persist_keys
                }
                _tmp_path = _config_file + ".tmp"
                with open(_tmp_path, 'w', encoding='utf-8') as _mf:
                    _json.dump(_main_cfg, _mf, indent=2, ensure_ascii=False)
                os.replace(_tmp_path, _config_file)
                os.rename(_teams_legacy, _teams_legacy + ".migrated")
                print(
                    f"  [config] Teams-Konfiguration aus "
                    f"{_teams_legacy} migriert nach config.json['teams']; "
                    f"alte Datei umbenannt nach teams_config.json.migrated"
                )
except Exception as _mig_err:
    print(f"  [config] Teams-Migration übersprungen: {_mig_err}")

if os.path.isfile(_config_file):
    try:
        _cfg_data = _read_version_json(_config_file)
        for _cfg_k, _cfg_v in _cfg_data.items():
            # Sub-Sektionen NICHT als Env-Variable setzen – sie werden zur
            # Laufzeit direkt aus der config.json gelesen (scanner durch
            # idv_scanner.py, teams durch den Teams-Scanner, ldap als
            # optionaler Override der DB, alle via webapp.config_store).
            if _cfg_k in ("scanner", "teams", "ldap"):
                continue
            # Lokale Benutzer (VULN-F) und komplexe Werte müssen als JSON
            # serialisiert werden, sonst wird "[{'username': ...}]" als
            # String mit Python-Repr übergeben.
            if isinstance(_cfg_v, (list, dict)):
                os.environ.setdefault(_cfg_k, _json.dumps(_cfg_v))
            elif isinstance(_cfg_v, bool):
                os.environ.setdefault(_cfg_k, "1" if _cfg_v else "0")
            else:
                os.environ.setdefault(_cfg_k, str(_cfg_v))
    except Exception as _cfg_err:
        print(f"  [config] config.json konnte nicht geladen werden: {_cfg_err}")
# ─────────────────────────────────────────────────────────────────────────────

# Gebündelte version.json lesen (immer, unabhängig vom Sidecar)
if getattr(sys, 'frozen', False):
    _bundled_vf = os.path.join(sys._MEIPASS, 'version.json')
else:
    _bundled_vf = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'version.json')
_bundled_vi = _read_version_json(_bundled_vf)
if _bundled_vi.get('version'):
    os.environ['BUNDLED_VERSION'] = _bundled_vi['version']

_upd = _get_updates_dir()
if _upd:
    sys.meta_path.insert(0, _SidecarFinder(_upd))
    sys.path.insert(0, _upd)
    # Aktive Version (Sidecar) für create_app bereitstellen
    _sidecar_vi = _read_version_json(os.path.join(_upd, 'version.json'))
    if _sidecar_vi.get('version'):
        os.environ['IDV_ACTIVE_VERSION'] = _sidecar_vi['version']
    print(f"  [update] Override aktiv: {_upd}")

# IDV_INSTANCE_PATH vor create_app() setzen — damit der korrekte Pfad verwendet
# wird, auch wenn webapp/__init__.py aus dem Sidecar stammt und Flask einen
# falschen root_path ableitet.
os.environ.setdefault('IDV_INSTANCE_PATH', os.path.join(_PROJECT_ROOT, 'instance'))
# ─────────────────────────────────────────────────────────────────────────────

# --scan Modus: Die exe startet als Scanner-Subprocess (PyInstaller-Kompatibilität).
# Im Bundle existiert keine separate idv_scanner.py mehr – stattdessen ruft
# admin.py den gleichen Executable mit --scan auf.
if '--scan' in sys.argv:
    sys.argv = [a for a in sys.argv if a != '--scan']
    _crash_log = os.path.join(_PROJECT_ROOT, 'instance', 'logs', 'scanner_crash.log')
    os.makedirs(os.path.join(_PROJECT_ROOT, 'instance', 'logs'), exist_ok=True)
    try:
        import idv_scanner
        idv_scanner.main()
    except BaseException:
        import traceback
        with open(_crash_log, 'w', encoding='utf-8') as _f:
            traceback.print_exc(file=_f)
    sys.exit(0)

# ── Flask-App lazy aufbauen ──────────────────────────────────────────────────
# Beim Start als Windows-Dienst (argv enthält --service-run) MUSS
# servicemanager.StartServiceCtrlDispatcher() innerhalb von ~30 s nach dem
# EXE-Start erreicht werden — sonst killt SCM den Prozess mit Fehler 1053.
# In einer PyInstaller-Onefile-EXE verschlingt allein das Entpacken nach
# %TEMP%\_MEIxxxx schon mehrere Sekunden; create_app() lädt zusätzlich
# Flask, alle Blueprints, ldap3, cryptography, openpyxl und initialisiert
# die SQLite-DB – zusammen reicht das auf langsameren Maschinen (oder bei
# langsamem %TEMP%) nicht für die 30-Sekunden-Grenze.
#
# Deshalb: im Service-Modus wird der App-Aufbau erst innerhalb SvcDoRun()
# angestoßen – nach diesem Zeitpunkt hat pywin32 bereits SERVICE_RUNNING
# gemeldet und SCM wartet nicht mehr.
app = None  # wird in _build_app() gesetzt


def _build_app():
    """Baut die Flask-App einmalig auf und setzt den Jinja-Loader."""
    global app
    if app is not None:
        return app
    from webapp import create_app
    app = create_app()

    # ── Template-Loader in run.py verankern ──
    # run.py ist die einzige Datei die nicht durch den Sidecar überschrieben
    # wird. Der Loader wird hier nach create_app() final gesetzt — korrekt
    # unabhängig davon, von wo oder in welcher Version webapp/__init__.py
    # geladen wurde (z.B. ältere Version aus updates/ ohne
    # IDV_PROJECT_ROOT-Unterstützung).
    from jinja2 import ChoiceLoader, FileSystemLoader as _FSL

    if getattr(sys, 'frozen', False):
        _real_tpl = os.path.join(sys._MEIPASS, 'webapp', 'templates')
    else:
        _real_tpl = os.path.join(_PROJECT_ROOT, 'webapp', 'templates')
    _ovr_tpl = os.path.join(_PROJECT_ROOT, 'updates', 'templates')

    if _upd and os.path.isdir(_ovr_tpl):
        app.jinja_loader = ChoiceLoader([_FSL(_ovr_tpl), _FSL(_real_tpl)])
    else:
        app.jinja_loader = _FSL(_real_tpl)
    return app


# Im Service-Modus NICHT eager aufbauen – siehe Kommentar oben.
# Für gunicorn ("run:app") und den normalen EXE-Start bleibt das Verhalten
# gleich: app ist nach Import von run.py direkt nutzbar.
if '--service-run' not in sys.argv:
    _build_app()
# ─────────────────────────────────────────────────────────────────────────────

# Optional: Demo-Daten beim ersten Start laden
def _seed_if_empty(app):
    with app.app_context():
        from webapp.routes import get_db
        from db import insert_demo_data
        db = get_db()
        count = db.execute("SELECT COUNT(*) FROM idv_register").fetchone()[0]
        if count == 0:
            print("  → Keine IDVs gefunden – Demo-Daten werden eingefügt.")
            insert_demo_data(db)


def _run_server(service_mode: bool = False):
    """Startet den Flask-WSGI-Server.

    service_mode=True: Kein Konsolenoutput; os._exit statt sys.exit, damit
    der Prozess auch aus einem Daemon-Thread heraus zuverlässig beendet wird.
    """
    # Im Service-Modus wird die Flask-App bewusst erst hier aufgebaut (siehe
    # Kommentar bei _build_app) – nach diesem Zeitpunkt hat pywin32 bereits
    # SERVICE_RUNNING an SCM gemeldet, der 30-Sekunden-Timeout (Fehler 1053)
    # kann hier nicht mehr zuschlagen.
    _build_app()

    from ssl_utils import build_ssl_context, https_enabled

    debug = os.environ.get("DEBUG", "0") == "1"

    # ── Sicherheits-Startup-Checks (VULN-004 / VULN-005) ─────────────────────
    if app.config.get("SECRET_KEY_IS_DEFAULT"):
        msg = (
            "SICHERHEITS-ABBRUCH: SECRET_KEY ist nicht gesetzt. In der Produktion "
            "muss ein zufälliger Wert (≥ 32 Zeichen) bereitgestellt werden – "
            "entweder als Umgebungsvariable SECRET_KEY oder als Eintrag "
            "\"SECRET_KEY\" in der config.json neben run.py/der EXE. "
            "Die Umgebungsvariable hat Vorrang vor der config.json.\n"
            "Beispiel (PowerShell):  $env:SECRET_KEY = [Guid]::NewGuid().ToString('N')\n"
            "Beispiel (Bash):        export SECRET_KEY=$(openssl rand -hex 32)\n"
            "Beispiel (config.json): {\"SECRET_KEY\": \"<32+ zufällige Zeichen>\", ...}\n"
            "Zum lokalen Entwickeln DEBUG=1 setzen, um diesen Check zu umgehen."
        )
        if not debug:
            if not service_mode:
                print("\n" + "!" * 70)
                print(msg)
                print("!" * 70)
            os._exit(2)
        elif not service_mode:
            print("\n" + "!" * 70)
            print("  WARNUNG: SECRET_KEY nicht gesetzt – Dev-Fallback aktiv.")
            print("  Dieser Start ist NUR für die lokale Entwicklung zulässig.")
            print("!" * 70 + "\n")

    if debug and not service_mode:
        print("\n" + "!" * 70)
        print("  WARNUNG: DEBUG-Modus aktiv.")
        print("  Der Flask-Debugger liefert Stacktraces und eine interaktive")
        print("  Konsole. NIEMALS in Produktionsumgebungen verwenden.")
        print("!" * 70 + "\n")
    # ─────────────────────────────────────────────────────────────────────────

    _instance_path = os.environ.get(
        'IDV_INSTANCE_PATH', os.path.join(_PROJECT_ROOT, 'instance')
    )
    ssl_context = build_ssl_context(_instance_path)
    scheme = "https" if ssl_context is not None else "http"
    default_port = 5443 if ssl_context is not None else 5000
    port = int(os.environ.get("PORT", default_port))

    if not service_mode:
        print("=" * 55)
        print("  idvault – IDV-Register")
        print(f"  {scheme}://localhost:{port}")
        print(f"  DB: {app.config['DATABASE']}")
        if ssl_context is not None:
            print("  HTTPS aktiv – Zertifikat aus instance/certs/")
        elif https_enabled():
            print("  HTTPS angefordert, aber kein SSL-Kontext verfügbar.")
        _local_users = app.config.get("IDV_LOCAL_USERS") or {}
        if _local_users:
            print(f"  Lokale Benutzer (config.json): {', '.join(sorted(_local_users))}")
        else:
            print("  Keine lokalen Benutzer konfiguriert – nur DB-Konten/LDAP.")
        print("=" * 55)

    _seed_if_empty(app)
    # use_reloader=False: Im EXE-Bundle gibt es keine .py-Dateien zum Beobachten;
    # der Reloader würde nutzlos einen zweiten Prozess spawnen.
    app.run(host="0.0.0.0", port=port, debug=debug, ssl_context=ssl_context,
            use_reloader=not getattr(sys, 'frozen', False))


# ── Windows-Dienst-Framework (nur als EXE auf Windows) ──────────────────────
# Voraussetzung: pywin32 (bereits in requirements.txt).
#
# Befehle (als Administrator ausführen):
#   idvault.exe install    → Dienst registrieren (Name aus IDV_SERVICE_NAME
#                            oder Standard "idvault")
#   idvault.exe start      → Dienst starten
#   idvault.exe stop       → Dienst stoppen
#   idvault.exe restart    → Dienst neu starten
#   idvault.exe remove     → Dienst entfernen
#
# Nach "install" startet der SCM die EXE automatisch mit "--service-run".
# Dienstkonto für Netzlaufwerk-Scans:
#   - LOCAL SYSTEM: einfachste Option; Scanner-Benutzer separat in der
#     Web-UI unter Administration → Scanner-Einstellungen konfigurieren.
#   - Domain-Konto: Hat der Dienst direkten Zugriff auf die Scan-Pfade,
#     ist kein separater Scanner-Benutzer nötig. Für einen abweichenden
#     Run-As-Benutzer muss dem Dienstkonto SeAssignPrimaryTokenPrivilege
#     erteilt werden (secpol.msc → Lokale Richtlinien → Zuweisen von
#     Benutzerrechten → "Token auf Prozessebene ersetzen").
# ─────────────────────────────────────────────────────────────────────────────

def _make_service_class():
    """Erzeugt die ServiceFramework-Klasse mit dem konfigurierten Dienstnamen.

    Wird innerhalb einer Funktion definiert, damit _svc_name_ erst nach dem
    Laden der config.json (Modul-Ebene) aus der Umgebungsvariablen gelesen
    werden kann.
    """
    import win32service    # noqa – nur auf Windows verfügbar
    import win32event      # noqa
    import servicemanager  # noqa
    import win32serviceutil

    svc_name = os.environ.get('IDV_SERVICE_NAME', 'idvault').strip() or 'idvault'

    class _IdvaultService(win32serviceutil.ServiceFramework):
        _svc_name_         = svc_name
        _svc_display_name_ = 'idvault IDV-Register'
        _svc_description_  = 'IDV-Register Web-Anwendung'
        # Argument, das der SCM beim Start an die EXE übergibt:
        _exe_args_         = '--service-run'

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self._stop_evt = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self._stop_evt)

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ''),
            )

            # SCM startet den Dienst mit CWD=C:\Windows\System32. Das
            # bricht jede relative Pfadauflösung (config.json,
            # sqlite3.connect, open(...)). Vor dem Flask-Start einmal
            # auf das EXE-Verzeichnis wechseln, damit wir uns so verhalten
            # wie beim manuellen Aufruf aus dem Installationsverzeichnis.
            try:
                os.chdir(os.path.dirname(sys.executable))
            except OSError:
                pass

            # _run_server() läuft in einem Daemon-Thread. Ohne Absicherung
            # würde eine Exception dort nur in sys.stderr (→ Crash-Log)
            # landen und der Dienst bliebe laut SCM im Status "wird
            # ausgeführt", obwohl Flask nie gestartet wurde. Deshalb
            # Exceptions hier abfangen und das Stop-Event feuern, damit
            # der Dienst sauber in "Beendet" geht.
            def _server_thread():
                try:
                    _run_server(service_mode=True)
                except BaseException:
                    import traceback
                    traceback.print_exc()
                    try:
                        servicemanager.LogErrorMsg(
                            "idvault: _run_server() abgebrochen – "
                            "siehe instance/logs/idvault_crash.log"
                        )
                    except Exception:
                        pass
                    win32event.SetEvent(self._stop_evt)

            import threading as _t
            _t.Thread(target=_server_thread, daemon=True).start()
            win32event.WaitForSingleObject(self._stop_evt, win32event.INFINITE)
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STOPPED,
                (self._svc_name_, ''),
            )

            # SERVICE_STOPPED explizit melden, bevor os._exit den Prozess
            # hart beendet. Ohne diesen Report sieht SCM nur einen
            # abgestürzten Prozess und meldet Fehler 1067 ("Prozess wurde
            # unerwartet beendet").
            self.ReportServiceStatus(win32service.SERVICE_STOPPED)
            os._exit(0)

    return _IdvaultService


if __name__ == "__main__":
    # ── Windows-Dienst-Modus (nur als EXE) ───────────────────────────────────
    if os.name == 'nt' and getattr(sys, 'frozen', False):
        _first_arg = sys.argv[1].lower() if len(sys.argv) > 1 else ''

        if '--service-run' in sys.argv:
            # SCM hat die EXE mit --service-run gestartet → Dienst-Dispatcher
            try:
                import servicemanager as _sm
                _Svc = _make_service_class()
                _sm.Initialize()
                _sm.PrepareToHostSingle(_Svc)
                _sm.StartServiceCtrlDispatcher()
            except ImportError as _e:
                print(f"pywin32 nicht verfügbar – Dienst-Modus nicht möglich: {_e}")
            sys.exit(0)

        if _first_arg in ('install', 'remove', 'start', 'stop', 'restart',
                          'debug', 'update'):
            # Dienst-Verwaltung: install/remove/start/stop/restart
            try:
                import win32serviceutil as _wsu
                _Svc = _make_service_class()
                _wsu.HandleCommandLine(_Svc)
            except ImportError as _e:
                print(f"pywin32 nicht verfügbar – Dienst-Modus nicht möglich: {_e}")
            sys.exit(0)
    # ─────────────────────────────────────────────────────────────────────────

    _run_server()
