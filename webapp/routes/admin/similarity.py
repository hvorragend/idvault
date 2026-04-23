"""Admin-Sub-Modul: Konfiguration der Ähnlichkeitsanalyse.

Der eigentliche Algorithmus liegt in ``webapp/similarity.py``. Diese Route
bietet nur die Admin-UI zum Anpassen von Gewichten, Schwelle, Algorithmus
und Rausch-Wortliste.
"""
from flask import render_template, request, redirect, url_for, flash

from .. import admin_required, get_db
from ... import similarity as _sim
from ...app_settings import set_json
from . import bp


def _parse_int(raw: str, default: int, *, lo: int = 0, hi: int = 100) -> int:
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


@bp.route("/aehnlichkeit", methods=["GET", "POST"])
@admin_required
def similarity_settings():
    db  = get_db()
    cfg = _sim.get_config(db)

    if request.method == "POST":
        algo = request.form.get("name_algorithm", cfg["name_algorithm"]).strip()
        if algo not in ("token_set", "partial", "jaccard"):
            algo = _sim.DEFAULT_CONFIG["name_algorithm"]

        noise_raw = request.form.get("noise_words", "")
        noise_list = [w.strip().lower() for w in noise_raw.replace(",", "\n").splitlines()]
        noise_list = sorted({w for w in noise_list if w})

        new_cfg = {
            "weight_type":    _parse_int(request.form.get("weight_type"),  cfg["weight_type"],  hi=100),
            "weight_owner":   _parse_int(request.form.get("weight_owner"), cfg["weight_owner"], hi=100),
            "weight_name":    _parse_int(request.form.get("weight_name"),  cfg["weight_name"],  hi=100),
            "threshold":      _parse_int(request.form.get("threshold"),    cfg["threshold"],    hi=100),
            "name_algorithm": algo,
            "noise_words":    noise_list,
            "max_candidates": _parse_int(request.form.get("max_candidates"), cfg["max_candidates"], lo=10, hi=10000),
            "max_results":    _parse_int(request.form.get("max_results"),    cfg["max_results"],    lo=1,  hi=500),
            "auto_assign_threshold": _parse_int(
                request.form.get("auto_assign_threshold"),
                cfg["auto_assign_threshold"], hi=100,
            ),
        }

        if request.form.get("reset") == "1":
            new_cfg = dict(_sim.DEFAULT_CONFIG)

        set_json(db, "similarity_config", new_cfg)
        flash("Ähnlichkeitsanalyse-Konfiguration gespeichert.", "success")
        return redirect(url_for("admin.similarity_settings"))

    return render_template(
        "admin/similarity_einstellungen.html",
        cfg=cfg,
        defaults=_sim.DEFAULT_CONFIG,
        rapidfuzz_available=_sim.rapidfuzz_available(),
    )
