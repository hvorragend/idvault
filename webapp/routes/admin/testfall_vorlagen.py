"""Admin-Sub-Modul: Testfall-Vorlagen pflegen (#319, Iteration 2).

CRUD-UI für die Vorlagen-Bibliothek ``testfall_vorlagen``. Die Seed-Vorlagen
aus Migration 0006 decken die häufigsten IDV-Typen bereits ab; über diese
Oberfläche können Administratoren eigene Vorlagen ergänzen, typ-Filter
anpassen und nicht mehr benötigte Vorlagen deaktivieren.

Rich-Text-Felder (Beschreibung, Parametrisierung, Testdaten, erwartetes
Ergebnis) werden durch ``sanitize_html`` bereinigt — Vorlagen landen
unverändert in QuillJS-Editoren der Prüfer (siehe webapp/routes/tests.py).
"""
from __future__ import annotations

from datetime import datetime, timezone

from flask import render_template, request, redirect, url_for, flash

from .. import admin_required, get_db
from ...db_writer import get_writer
from ...security import sanitize_html
from db_write_tx import write_tx
from . import bp


_ARTEN = ("fachlich", "technisch")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _idv_typen(db) -> list[str]:
    """Liefert die aktiven IDV-Typen aus der Klassifizierungstabelle.

    Fällt auf eine Default-Liste zurück, wenn noch keine Typen gepflegt
    sind — spiegelt die Seed-Vorlagen aus Migration 0006.
    """
    try:
        rows = db.execute(
            "SELECT wert FROM klassifizierungen "
            "WHERE bereich='idv_typ' AND (aktiv IS NULL OR aktiv=1) "
            "ORDER BY sort_order, wert"
        ).fetchall()
    except Exception:
        rows = []
    werte = [r["wert"] for r in rows if r["wert"]]
    if werte:
        return werte
    return [
        "Excel-Makro", "Excel-Tabelle", "Access-Datenbank",
        "SQL-Skript", "Python-Skript", "Power-BI-Bericht", "Cognos-Report",
    ]


def _clean_art(value: str) -> str | None:
    value = (value or "").strip()
    return value if value in _ARTEN else None


def _clean_typ(value: str, typen: list[str]) -> str | None:
    """Akzeptiert einen IDV-Typ aus der Whitelist oder leer (= typ-unabhängig)."""
    value = (value or "").strip()
    if not value:
        return None
    return value if value in typen else None


def _collect_form(typen: list[str]) -> dict:
    art = _clean_art(request.form.get("art"))
    return {
        "titel":               (request.form.get("titel") or "").strip(),
        "idv_typ":             _clean_typ(request.form.get("idv_typ"), typen),
        "art":                 art,
        "beschreibung":        sanitize_html(request.form.get("beschreibung") or ""),
        "parametrisierung":    sanitize_html(request.form.get("parametrisierung") or ""),
        "testdaten":           sanitize_html(request.form.get("testdaten") or ""),
        "erwartetes_ergebnis": sanitize_html(request.form.get("erwartetes_ergebnis") or ""),
    }


@bp.route("/testfall-vorlagen", methods=["GET"])
@admin_required
def list_testfall_vorlagen():
    db     = get_db()
    typen  = _idv_typen(db)
    vorlagen = db.execute("""
        SELECT id, titel, idv_typ, art, beschreibung, parametrisierung,
               testdaten, erwartetes_ergebnis, aktiv, created_at, updated_at
          FROM testfall_vorlagen
         ORDER BY aktiv DESC, art, (idv_typ IS NULL) ASC, idv_typ, titel
    """).fetchall()
    return render_template("admin/testfall_vorlagen.html",
                           vorlagen=vorlagen,
                           idv_typen=typen,
                           arten=_ARTEN)


@bp.route("/testfall-vorlagen/neu", methods=["POST"])
@admin_required
def new_testfall_vorlage():
    db    = get_db()
    typen = _idv_typen(db)
    data  = _collect_form(typen)

    if not data["titel"]:
        flash("Titel ist erforderlich.", "error")
        return redirect(url_for("admin.list_testfall_vorlagen"))
    if not data["art"]:
        flash("Art (fachlich/technisch) ist erforderlich.", "error")
        return redirect(url_for("admin.list_testfall_vorlagen"))

    now = _now()

    def _do(c):
        with write_tx(c):
            c.execute("""
                INSERT INTO testfall_vorlagen
                    (titel, idv_typ, art, beschreibung, parametrisierung,
                     testdaten, erwartetes_ergebnis, aktiv, created_at)
                VALUES (?,?,?,?,?,?,?,1,?)
            """, (data["titel"], data["idv_typ"], data["art"],
                  data["beschreibung"], data["parametrisierung"],
                  data["testdaten"], data["erwartetes_ergebnis"], now))

    try:
        get_writer().submit(_do, wait=True)
        flash(f"Vorlage '{data['titel']}' angelegt.", "success")
    except Exception as exc:
        flash(f"Vorlage konnte nicht angelegt werden: {exc}", "error")
    return redirect(url_for("admin.list_testfall_vorlagen"))


@bp.route("/testfall-vorlagen/<int:vorlage_id>/bearbeiten", methods=["POST"])
@admin_required
def edit_testfall_vorlage(vorlage_id):
    db    = get_db()
    typen = _idv_typen(db)

    row = db.execute(
        "SELECT id, titel FROM testfall_vorlagen WHERE id=?", (vorlage_id,)
    ).fetchone()
    if not row:
        flash("Vorlage nicht gefunden.", "error")
        return redirect(url_for("admin.list_testfall_vorlagen"))

    data = _collect_form(typen)
    if not data["titel"]:
        flash("Titel darf nicht leer sein.", "error")
        return redirect(url_for("admin.list_testfall_vorlagen"))
    if not data["art"]:
        flash("Art (fachlich/technisch) ist erforderlich.", "error")
        return redirect(url_for("admin.list_testfall_vorlagen"))

    aktiv = 1 if request.form.get("aktiv") else 0
    now   = _now()

    def _do(c):
        with write_tx(c):
            c.execute("""
                UPDATE testfall_vorlagen
                   SET titel=?, idv_typ=?, art=?, beschreibung=?,
                       parametrisierung=?, testdaten=?, erwartetes_ergebnis=?,
                       aktiv=?, updated_at=?
                 WHERE id=?
            """, (data["titel"], data["idv_typ"], data["art"],
                  data["beschreibung"], data["parametrisierung"],
                  data["testdaten"], data["erwartetes_ergebnis"],
                  aktiv, now, vorlage_id))

    try:
        get_writer().submit(_do, wait=True)
        flash("Vorlage gespeichert.", "success")
    except Exception as exc:
        flash(f"Speichern fehlgeschlagen: {exc}", "error")
    return redirect(url_for("admin.list_testfall_vorlagen"))


@bp.route("/testfall-vorlagen/<int:vorlage_id>/loeschen", methods=["POST"])
@admin_required
def delete_testfall_vorlage(vorlage_id):
    db = get_db()
    row = db.execute(
        "SELECT titel FROM testfall_vorlagen WHERE id=?", (vorlage_id,)
    ).fetchone()
    if not row:
        flash("Vorlage nicht gefunden.", "error")
        return redirect(url_for("admin.list_testfall_vorlagen"))

    def _do(c):
        with write_tx(c):
            c.execute("DELETE FROM testfall_vorlagen WHERE id=?", (vorlage_id,))

    try:
        get_writer().submit(_do, wait=True)
        flash(f"Vorlage '{row['titel']}' gelöscht.", "success")
    except Exception as exc:
        flash(f"Löschen fehlgeschlagen: {exc}", "error")
    return redirect(url_for("admin.list_testfall_vorlagen"))
