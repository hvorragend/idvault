"""Quick-Actions Blueprint – signierte Magic-Links aus E-Mail-CTAs.

GET /quick/freigabe/<freigabe_id>?token=<signed>
  Token wird validiert (7-Tage-TTL, HMAC-SHA256).
  Nicht eingeloggt → Ziel in Session merken, auf Login weiterleiten.
  Eingeloggt       → direkt auf die Freigabe-Seite weiterleiten.
"""
from flask import Blueprint, request, redirect, url_for, session, abort, current_app

bp = Blueprint("quick_actions", __name__, url_prefix="/quick")


@bp.route("/freigabe/<int:freigabe_id>")
def freigabe(freigabe_id: int):
    token = request.args.get("token", "")
    if not token:
        abort(400)

    from ..tokens import verify_freigabe_token
    payload = verify_freigabe_token(current_app.config["SECRET_KEY"], token)
    if payload is None or payload.get("f") != freigabe_id:
        abort(400)

    dest = url_for("freigaben.erledigt_seite", freigabe_id=freigabe_id)

    if not session.get("user_id"):
        session["_quick_next"] = dest
        return redirect(url_for("auth.login"))

    return redirect(dest)
