"""
Info / Hilfe – statische Erläuterungsseiten.

Enthält aktuell ausschließlich das Glossar zur regulatorischen
Begriffsabgrenzung (MaRisk AT 7.2, BAIT, DORA) zwischen
Anwendungsentwicklung, Eigen- und Auftragsprogrammierung, IDV und
Arbeitshilfe.
"""

from flask import Blueprint, render_template
from . import login_required, get_db

bp = Blueprint("info", __name__, url_prefix="/hilfe")

_GLOSSAR_SETTINGS_KEYS = [
    "glossar_hintergrund_text",
    "glossar_wesentlichkeit_titel",
    "glossar_wesentlichkeit_einleitung",
    "glossar_wesentlichkeit_kriterien",
    "glossar_wesentlichkeit_schluss",
]


def _load_glossar_settings(db) -> dict:
    rows = db.execute(
        "SELECT key, value FROM app_settings WHERE key IN ({})".format(
            ",".join("?" * len(_GLOSSAR_SETTINGS_KEYS))
        ),
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
    kriterien = [
        line.strip()
        for line in (settings.get("glossar_wesentlichkeit_kriterien") or "").splitlines()
        if line.strip()
    ]
    return render_template(
        "info/glossar.html",
        glossar=[dict(r) for r in rows],
        hintergrund_text=settings.get("glossar_hintergrund_text", ""),
        wesentlichkeit_titel=settings.get("glossar_wesentlichkeit_titel", ""),
        wesentlichkeit_einleitung=settings.get("glossar_wesentlichkeit_einleitung", ""),
        wesentlichkeit_kriterien=kriterien,
        wesentlichkeit_schluss=settings.get("glossar_wesentlichkeit_schluss", ""),
    )
