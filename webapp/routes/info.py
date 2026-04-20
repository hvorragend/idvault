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
    return render_template("info/glossar.html", glossar=[dict(r) for r in rows])
