"""
Info / Hilfe – statische Erläuterungsseiten.

Enthält aktuell ausschließlich das Glossar zur regulatorischen
Begriffsabgrenzung (MaRisk AT 7.2, DORA) zwischen
Anwendungsentwicklung, Eigen- und Auftragsprogrammierung, IDV und
Arbeitshilfe.
"""

from flask import Blueprint, render_template
from . import login_required, get_db

bp = Blueprint("info", __name__, url_prefix="/hilfe")

_GLOSSAR_SETTINGS_KEYS = ["glossar_hintergrund_text", "glossar_info_unten"]


def _load_glossar_settings(db) -> dict:
    rows = db.execute(
        "SELECT key, value FROM app_settings WHERE key IN (?,?)",
        _GLOSSAR_SETTINGS_KEYS,
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


@bp.route("/glossar")
@login_required
def glossar():
    db = get_db()
    rows = db.execute("""
        SELECT id, begriff, entwickler, ort, fokus, beschreibung, im_register
        FROM glossar_eintraege
        WHERE aktiv = 1
        ORDER BY sort_order, id
    """).fetchall()
    settings = _load_glossar_settings(db)
    return render_template(
        "info/glossar.html",
        glossar=[dict(r) for r in rows],
        hintergrund_text=settings.get("glossar_hintergrund_text", ""),
        info_unten=settings.get("glossar_info_unten", ""),
    )
