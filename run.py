"""
idvault – Startpunkt
====================
Entwicklung:  python run.py
Produktion:   gunicorn "run:app" --workers 1 --bind 0.0.0.0:5000
              (oder cheroot/waitress mit genau 1 Prozess; siehe unten)

WICHTIG — Single-Process-Constraint:
  Ab der Einfuehrung des db_writer-Threads (webapp/db_writer.py) darf die
  App nur in *einem* Prozess laufen. Mehrere Worker-Prozesse haetten je
  ihren eigenen Writer-Thread und damit wieder konkurrierende Writer, was
  die database-is-locked-Race zurueckbringt.

  - gunicorn: --workers 1
  - waitress / cheroot: single-process (Default)
  - uwsgi:   --processes 1  (Threads statt Prozesse verwenden)

Konfiguration (config.json):
  Beim ersten Start wird config.json mit einem zufälligen SECRET_KEY
  automatisch angelegt. Die Datei enthält NUR Bootstrap-Werte, die bei
  Fehlkonfiguration den Start oder Login blockieren würden:

    SECRET_KEY, PORT, DEBUG, IDV_HTTPS, IDV_SSL_CERT, IDV_SSL_KEY,
    IDV_SSL_AUTOGEN, IDV_DB_PATH, IDV_INSTANCE_PATH, IDV_LOCAL_USERS,
    IDV_SERVICE_NAME

  Alles andere (SMTP, LDAP, Scanner, Teams, Rate-Limits, Pfad-Mappings,
  Sidecar-Update-Schalter) wird über die Web-UI in der SQLite-Datenbank
  (app_settings bzw. ldap_config) verwaltet.

  OS-Umgebungsvariablen werden nicht mehr ausgewertet – außer den
  prozessinternen Hilfsvariablen IDV_PROJECT_ROOT, IDV_ACTIVE_VERSION und
  BUNDLED_VERSION, die run.py selbst setzt.
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
# config.json liegt neben run.py bzw. neben der EXE und enthält NUR Bootstrap-
# Werte. Alle anderen Einstellungen werden über die Web-UI in der Datenbank
# (Tabelle app_settings bzw. ldap_config) gepflegt.
_config_file = os.path.join(_PROJECT_ROOT, 'config.json')
if not os.path.isfile(_config_file):
    import secrets as _secrets
    _auto_cfg = {
        "SECRET_KEY": _secrets.token_hex(32),
        "PORT": 5000,
        "DEBUG": 0,
        "IDV_HTTPS": 0,
        "IDV_SSL_CERT": "instance/certs/cert.pem",
        "IDV_SSL_KEY": "instance/certs/key.pem",
        "IDV_SSL_AUTOGEN": 1,
        "IDV_DB_PATH": "instance/idvault.db",
        "IDV_INSTANCE_PATH": "instance",
        "IDV_SERVICE_NAME": "idvault",
        # VULN-F: Lokale Benutzer. Leere Liste = kein lokaler Login möglich
        # (nur LDAP). Pro Eintrag entweder 'password_hash' (empfohlen,
        # werkzeug-Format) oder 'password' (Klartext, optional – wird beim
        # Start automatisch gehasht). Beispiel siehe config.json.example.
        "IDV_LOCAL_USERS": [],
    }
    try:
        with open(_config_file, 'w', encoding='utf-8') as _f:
            _json.dump(_auto_cfg, _f, indent=2, ensure_ascii=False)
        print(f"  [config] Neue config.json angelegt: {_config_file}")
    except Exception:
        pass  # Schreibfehler dürfen den Start nicht verhindern
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

# ─────────────────────────────────────────────────────────────────────────────

# --scan Modus: Die exe startet als Scanner-Subprocess (PyInstaller-Kompatibilität).
# Im Bundle existiert keine separate network_scanner.py mehr – stattdessen ruft
# admin.py den gleichen Executable mit --scan auf.
if '--scan' in sys.argv:
    sys.argv = [a for a in sys.argv if a != '--scan']
    _crash_log = os.path.join(_PROJECT_ROOT, 'instance', 'logs', 'scanner_crash.log')
    os.makedirs(os.path.join(_PROJECT_ROOT, 'instance', 'logs'), exist_ok=True)
    try:
        import network_scanner
        network_scanner.main()
    except BaseException:
        import traceback
        _tb = traceback.format_exc()
        with open(_crash_log, 'w', encoding='utf-8') as _f:
            _f.write(_tb)
        print(_tb, file=sys.stderr, flush=True)
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
            print("  → Keine Eigenentwicklungen gefunden – Demo-Daten werden eingefügt.")
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
    from webapp import config_store

    debug = config_store.get_bool("DEBUG", False)

    # ── Sicherheits-Startup-Checks (VULN-004 / VULN-005) ─────────────────────
    if app.config.get("SECRET_KEY_IS_DEFAULT"):
        msg = (
            "SICHERHEITS-ABBRUCH: SECRET_KEY ist nicht gesetzt. In der Produktion "
            "muss ein zufälliger Wert (≥ 32 Zeichen) in der config.json neben "
            "run.py bzw. der EXE hinterlegt werden.\n"
            "Beispiel (config.json): {\"SECRET_KEY\": \"<32+ zufällige Zeichen>\", ...}\n"
            "Zum lokalen Entwickeln \"DEBUG\": 1 in die config.json eintragen, "
            "um diesen Check zu umgehen."
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

    _raw_instance = config_store.get_str('IDV_INSTANCE_PATH',
                                         os.path.join(_PROJECT_ROOT, 'instance'))
    _instance_path = (
        _raw_instance if os.path.isabs(_raw_instance)
        else os.path.normpath(os.path.join(_PROJECT_ROOT, _raw_instance))
    )
    ssl_context = build_ssl_context(_instance_path)
    scheme = "https" if ssl_context is not None else "http"
    default_port = 5443 if ssl_context is not None else 5000
    port = config_store.get_int("PORT", default_port)

    if not service_mode:
        print("=" * 55)
        print("  idvault – Register für Eigenentwicklungen")
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
    else:
        # Im Dienst-Modus: dieselben Informationen nach instance/logs/idvault.log
        # (Flask-Logger) statt in die Konsole. Spart beim nächsten „Website
        # nicht erreichbar" das manuelle Starten der EXE zur Diagnose.
        # WARNING-Level, weil der File-Handler (webapp/__init__.py) auf
        # WARNING+ konfiguriert ist; für ops-relevante Lifecycle-Events
        # semantisch passend. [startup]-Präfix zum Filtern per grep.
        app.logger.warning("[startup] idvault Dienst-Modus: %s://0.0.0.0:%s", scheme, port)
        app.logger.warning("[startup] DATABASE=%s", app.config['DATABASE'])
        app.logger.warning("[startup] instance_path=%s", app.instance_path)
        app.logger.warning("[startup] CWD=%s  EXE=%s", os.getcwd(), sys.executable)
        if ssl_context is None and https_enabled():
            app.logger.warning(
                "[startup] HTTPS angefordert, aber kein SSL-Kontext verfügbar – "
                "falle auf HTTP zurück."
            )

    _seed_if_empty(app)
    if service_mode:
        app.logger.warning("[startup] Werkzeug-Server bindet an %s:%s …", "0.0.0.0", port)
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
#     Die Credentials werden vor dem Scan-Start via WNetAddConnection2
#     in der Dienst-Session registriert (kein Sonderprivileg nötig).
#   - Domain-Konto: Hat der Dienst direkten Zugriff auf die Scan-Pfade,
#     ist kein separater Scanner-Benutzer nötig.
# ─────────────────────────────────────────────────────────────────────────────

def _make_service_class():
    """Erzeugt die ServiceFramework-Klasse mit dem konfigurierten Dienstnamen.

    Wird innerhalb einer Funktion definiert, damit _svc_name_ erst nach dem
    Laden der config.json (Modul-Ebene) daraus gelesen werden kann.
    """
    import win32service    # noqa – nur auf Windows verfügbar
    import win32event      # noqa
    import servicemanager  # noqa
    import win32serviceutil
    from webapp import config_store

    svc_name = (config_store.get_str('IDV_SERVICE_NAME', 'idvault') or '').strip() or 'idvault'

    class _IdvaultService(win32serviceutil.ServiceFramework):
        _svc_name_         = svc_name
        _svc_display_name_ = 'idvault – Register für Eigenentwicklungen'
        _svc_description_  = 'idvault – Register für Eigenentwicklungen (Web-Anwendung)'
        # Argument, das der SCM beim Start an die EXE übergibt:
        _exe_args_         = '--service-run'

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self._stop_evt = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            try:
                servicemanager.LogInfoMsg("idvault: Stop-Anforderung empfangen")
            except Exception:
                pass
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)

            # Writer-Queue drainen, damit keine Writes bei Prozessende
            # verloren gehen. Muss *vor* dem Event-Signal passieren, weil
            # SvcDoRun() sofort nach dem Signal os._exit(0) ruft.
            try:
                from webapp.db_writer import stop_writer
                stop_writer()
            except Exception:
                try:
                    servicemanager.LogErrorMsg(
                        "idvault: db_writer-Drain fehlgeschlagen (siehe Crash-Log)"
                    )
                except Exception:
                    pass

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
            _exe_dir = os.path.dirname(sys.executable)
            try:
                os.chdir(_exe_dir)
            except OSError:
                pass

            try:
                servicemanager.LogInfoMsg(
                    f"idvault: SvcDoRun gestartet (CWD={os.getcwd()}, "
                    f"EXE={sys.executable}, "
                    f"Crash-Log={os.path.join(_exe_dir, 'instance', 'logs', 'idvault_crash.log')})"
                )
            except Exception:
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
            try:
                servicemanager.LogInfoMsg(
                    "idvault: Stop-Event empfangen – Dienst wird beendet"
                )
            except Exception:
                pass
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
