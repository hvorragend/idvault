"""
idvault – Flask Web Application
================================
IDV-Register für MaRisk AT 7.2 / DORA-konforme Verwaltung
von Eigenentwicklungen (Individuelle Datenverarbeitung).
"""

import os
import sys
from pathlib import Path
from datetime import datetime, date
from flask import Flask
from .db_flask import get_db, close_db, init_app_db


def create_app(db_path: str = None) -> Flask:
    # PyInstaller-Kompatibilität: Im gefrorenen Bundle gibt es kein echtes
    # Package-Verzeichnis mehr – deshalb template_folder explizit setzen.
    if getattr(sys, 'frozen', False):
        _tpl = str(Path(sys._MEIPASS) / 'webapp' / 'templates')
        # Im Bundle zeigt app.instance_path auf den temporären _MEIPASS-Ordner.
        # Stattdessen: instance/ dauerhaft neben der .exe ablegen.
        _instance_default = os.path.join(os.path.dirname(sys.executable), 'instance')
    else:
        _tpl = 'templates'  # Flask-Default relativ zum Package
        _instance_default = None  # wird unten von app.instance_path geholt

    app = Flask(__name__, instance_relative_config=True, template_folder=_tpl)

    # Sidecar-Template-Override: templates aus updates/ haben Vorrang
    from jinja2 import ChoiceLoader, FileSystemLoader as _FSL
    if getattr(sys, 'frozen', False):
        _exe_dir = os.path.dirname(sys.executable)
    else:
        _exe_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _ovr_tpl = os.path.join(_exe_dir, 'updates', 'templates')
    if os.path.isdir(_ovr_tpl):
        app.jinja_loader = ChoiceLoader([_FSL(_ovr_tpl), app.jinja_loader])

    _instance_path = os.environ.get("IDV_INSTANCE_PATH",
                                    _instance_default or app.instance_path)
    upload_folder = os.path.join(_instance_path, "uploads", "freigaben")
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-change-in-production-!"),
        DATABASE=db_path or os.environ.get(
            "IDV_DB_PATH",
            os.path.join(_instance_path, "idvault.db")
        ),
        UPLOAD_FOLDER=upload_folder,
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,   # 32 MB max upload
        APP_NAME="idvault",
        APP_VERSION=os.environ.get('IDV_ACTIVE_VERSION') or os.environ.get('APP_VERSION', '0.1.0'),
    )

    os.makedirs(_instance_path, exist_ok=True)
    os.makedirs(upload_folder, exist_ok=True)

    # Datenbank
    init_app_db(app)

    # Blueprints registrieren
    from .routes.auth       import bp as auth_bp
    from .routes.dashboard  import bp as dash_bp
    from .routes.idv        import bp as idv_bp
    from .routes.reviews    import bp as rev_bp
    from .routes.measures   import bp as meas_bp
    from .routes.admin      import bp as admin_bp
    from .routes.scanner    import bp as scanner_bp
    from .routes.reports    import bp as reports_bp
    from .routes.freigaben  import bp as freigaben_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dash_bp)
    app.register_blueprint(idv_bp)
    app.register_blueprint(rev_bp)
    app.register_blueprint(meas_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(scanner_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(freigaben_bp)

    # Context Processor: Scanner-Eingang Badge-Count für alle Templates
    @app.context_processor
    def inject_scanner_badge():
        from flask import has_request_context
        if not has_request_context():
            return {}
        try:
            db = get_db()
            count = db.execute(
                "SELECT COUNT(*) FROM idv_files "
                "WHERE status='active' AND bearbeitungsstatus='Neu'"
            ).fetchone()[0]
        except Exception:
            count = 0
        return {"scanner_eingang_count": count}

    # Template-Filter
    @app.template_filter("datefmt")
    def datefmt(value, fmt="%d.%m.%Y"):
        if not value:
            return "–"
        try:
            if "T" in str(value):
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            else:
                dt = datetime.strptime(str(value)[:10], "%Y-%m-%d")
            return dt.strftime(fmt)
        except Exception:
            return str(value)

    @app.template_filter("mb")
    def to_mb(value):
        if value is None:
            return "–"
        return f"{value / 1024 / 1024:.2f} MB"

    @app.template_filter("yesno")
    def yesno(value, labels="Ja,Nein"):
        pos, neg = labels.split(",")
        return pos if value else neg

    return app
