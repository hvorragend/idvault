"""
idvault – Startpunkt
====================
Entwicklung:  python run.py
Produktion:   gunicorn "run:app" --workers 2 --bind 0.0.0.0:5000

Umgebungsvariablen:
  IDV_DB_PATH      Pfad zur SQLite-Datenbank (Standard: instance/idvault.db)
  SECRET_KEY       Flask Session Secret      (Pflicht in Produktion!)
  PORT             Netzwerkport              (Standard: 5000 / 5443 bei HTTPS)
  DEBUG            1 = Debug-Modus           (Standard: 0)
  IDV_HTTPS        1 = HTTPS aktivieren      (Standard: 0)
  IDV_SSL_CERT     Pfad zum Zertifikat (PEM) (Standard: instance/certs/cert.pem)
  IDV_SSL_KEY      Pfad zum priv. Schlüssel  (Standard: instance/certs/key.pem)
  IDV_SSL_AUTOGEN  1 = Selbstsigniertes Zertifikat erzeugen, falls fehlend
                                             (Standard: 1)
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
    _log_dir = os.path.join(_PROJECT_ROOT, 'instance')
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
if getattr(sys, 'frozen', False) and os.name == 'nt' and '--scan' not in sys.argv:
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
    _crash_log = os.path.join(os.path.dirname(sys.executable), 'scanner_crash.log')
    try:
        import idv_scanner
        idv_scanner.main()
    except BaseException:
        import traceback
        with open(_crash_log, 'w', encoding='utf-8') as _f:
            traceback.print_exc(file=_f)
    sys.exit(0)

from webapp import create_app

app = create_app()

# ── Template-Loader in run.py verankern ──────────────────────────────────────
# run.py ist die einzige Datei die nicht durch den Sidecar überschrieben wird.
# Der Jinja2-Loader wird hier nach create_app() final gesetzt — korrekt
# unabhängig davon, von wo oder in welcher Version webapp/__init__.py geladen
# wurde (z.B. ältere Version aus updates/ ohne IDV_PROJECT_ROOT-Unterstützung).
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


if __name__ == "__main__":
    from ssl_utils import build_ssl_context, https_enabled

    debug = os.environ.get("DEBUG", "0") == "1"

    # HTTPS optional: Zertifikats-Kontext vorbereiten (ggf. selbstsigniert).
    # Muss vor dem Port-Default ausgewertet werden, damit 5443 greift.
    # IDV_INSTANCE_PATH wurde oben bereits gesetzt (Default: <root>/instance).
    _instance_path = os.environ.get(
        'IDV_INSTANCE_PATH', os.path.join(_PROJECT_ROOT, 'instance')
    )
    ssl_context = build_ssl_context(_instance_path)
    scheme = "https" if ssl_context is not None else "http"
    default_port = 5443 if ssl_context is not None else 5000
    port = int(os.environ.get("PORT", default_port))

    print("=" * 55)
    print("  idvault – IDV-Register")
    print(f"  {scheme}://localhost:{port}")
    print(f"  DB: {app.config['DATABASE']}")
    if ssl_context is not None:
        print("  HTTPS aktiv – Zertifikat aus instance/certs/")
    elif https_enabled():
        # HTTPS gewünscht, Kontext aber nicht erstellbar (sollte nicht passieren,
        # build_ssl_context() würde in diesem Fall eine Exception werfen).
        print("  HTTPS angefordert, aber kein SSL-Kontext verfügbar.")
    print("  Demo-Login: admin / idvault2025")
    print("=" * 55)

    _seed_if_empty(app)

    app.run(host="0.0.0.0", port=port, debug=debug, ssl_context=ssl_context)
