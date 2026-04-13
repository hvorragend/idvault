"""
idvault – Startpunkt
====================
Entwicklung:  python run.py
Produktion:   gunicorn "run:app" --workers 2 --bind 0.0.0.0:5000

Umgebungsvariablen:
  IDV_DB_PATH    Pfad zur SQLite-Datenbank   (Standard: instance/idvault.db)
  SECRET_KEY     Flask Session Secret        (Pflicht in Produktion!)
  PORT           HTTP-Port                   (Standard: 5000)
  DEBUG          1 = Debug-Modus             (Standard: 0)
"""

import os
import sys

# Projektverzeichnis zum Pfad hinzufügen
sys.path.insert(0, os.path.dirname(__file__))

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
        pkg = os.path.join(self._base, rel, '__init__.py')
        if os.path.isfile(pkg):
            return importlib.util.spec_from_file_location(
                fullname, pkg,
                submodule_search_locations=[os.path.join(self._base, rel)]
            )
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
    except Exception:
        import traceback
        with open(_crash_log, 'w', encoding='utf-8') as _f:
            traceback.print_exc(file=_f)
    sys.exit(0)

from webapp import create_app

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


app = create_app()

if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "0") == "1"

    print("=" * 55)
    print("  idvault – IDV-Register")
    print(f"  http://localhost:{port}")
    print(f"  DB: {app.config['DATABASE']}")
    print("  Demo-Login: admin / idvault2025")
    print("=" * 55)

    _seed_if_empty(app)

    app.run(host="0.0.0.0", port=port, debug=debug)
