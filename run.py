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

# --scan Modus: Die exe startet als Scanner-Subprocess (PyInstaller-Kompatibilität).
# Im Bundle existiert keine separate idv_scanner.py mehr – stattdessen ruft
# admin.py den gleichen Executable mit --scan auf.
if '--scan' in sys.argv:
    sys.argv = [a for a in sys.argv if a != '--scan']
    import idv_scanner
    idv_scanner.main()
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
