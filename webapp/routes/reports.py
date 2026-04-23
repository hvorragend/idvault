"""Berichte-Blueprint: Auswertungen nach OE, Ersteller, Scan-Pfad + grafische Visualisierung."""
import json
from datetime import date
from flask import Blueprint, render_template, request
from . import login_required, get_db

bp = Blueprint("reports", __name__, url_prefix="/berichte")


# ── Hilfs-Konstanten für den Visualisierungs-Tab ─────────────────────────────
# Reihenfolge + Farbpalette der Statuswerte — identisch zu den .status-* CSS-Klassen
# in base.html (Zeilen 128–166), damit Donut/Stacked-Bar und Badges konsistent sind.
STATUS_ORDER = [
    "Entwurf",
    "In Prüfung",
    "Freigegeben",
    "Freigegeben mit Auflagen",
    "Abgelehnt",
    "Abgekündigt",
]
STATUS_COLORS = {
    "Entwurf": "#94a3b8",
    "In Prüfung": "#60a5fa",
    "Freigegeben": "#22c55e",
    "Freigegeben mit Auflagen": "#eab308",
    "Abgelehnt": "#ef4444",
    "Abgekündigt": "#a855f7",
}
APPROVED_STATUSES = ("Freigegeben", "Freigegeben mit Auflagen")


def _month_range(n_months: int):
    """Liste der letzten n_months Monate als 'YYYY-MM'-Strings, aufsteigend sortiert."""
    today = date.today()
    y, m = today.year, today.month
    result = []
    for _ in range(n_months):
        result.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(result))


@bp.route("/")
@login_required
def index():
    db = get_db()

    # ── 1. Bericht nach Organisationseinheit ──────────────────────────────
    by_oe = db.execute("""
        SELECT
            ou.id                                AS oe_id,
            ou.bezeichnung                       AS oe_bezeichnung,
            COUNT(r.id)                          AS anzahl,
            SUM(CASE WHEN EXISTS(SELECT 1 FROM idv_wesentlichkeit iw
                                 WHERE iw.idv_db_id=r.id AND iw.erfuellt=1)
                     THEN 1 ELSE 0 END)          AS wesentlich,
            SUM(CASE WHEN r.status = 'Freigegeben' THEN 1 ELSE 0 END)      AS genehmigt,
            SUM(CASE WHEN r.status = 'Entwurf' THEN 1 ELSE 0 END)        AS entwurf,
            SUM(CASE WHEN r.naechste_pruefung < date('now')
                      AND r.status NOT IN ('Archiviert','Abgekündigt') THEN 1 ELSE 0 END) AS ueberfaellig
        FROM idv_register r
        LEFT JOIN org_units ou ON r.org_unit_id = ou.id
        WHERE r.status NOT IN ('Archiviert')
        GROUP BY ou.id, ou.bezeichnung
        ORDER BY anzahl DESC, ou.bezeichnung
    """).fetchall()

    # ── 2. Bericht nach Fachverantwortlichem ─────────────────────────────
    by_fv = db.execute("""
        SELECT
            p.id                                 AS person_id,
            p.nachname || ', ' || p.vorname      AS person,
            ou.bezeichnung                       AS oe_bezeichnung,
            COUNT(r.id)                          AS anzahl,
            SUM(CASE WHEN EXISTS(SELECT 1 FROM idv_wesentlichkeit iw
                                 WHERE iw.idv_db_id=r.id AND iw.erfuellt=1)
                     THEN 1 ELSE 0 END)          AS wesentlich,
            SUM(CASE WHEN r.status = 'Freigegeben' THEN 1 ELSE 0 END)      AS genehmigt,
            SUM(CASE WHEN r.naechste_pruefung < date('now')
                      AND r.status NOT IN ('Archiviert','Abgekündigt') THEN 1 ELSE 0 END) AS ueberfaellig
        FROM idv_register r
        LEFT JOIN persons  p  ON r.fachverantwortlicher_id = p.id
        LEFT JOIN org_units ou ON p.org_unit_id = ou.id
        WHERE r.status NOT IN ('Archiviert')
        GROUP BY p.id, p.nachname, p.vorname, ou.bezeichnung
        ORDER BY anzahl DESC, p.nachname
    """).fetchall()

    # ── 3. Bericht nach Scan-Verzeichnis (share_root = Teilscan) ─────────
    by_path = db.execute("""
        SELECT
            f.share_root,
            COUNT(DISTINCT f.id)                 AS dateien_gesamt,
            COUNT(DISTINCT r.id)                 AS registriert,
            COUNT(DISTINCT f.id) - COUNT(DISTINCT r.id) AS nicht_registriert,
            SUM(CASE WHEN f.has_macros = 1 THEN 1 ELSE 0 END)           AS mit_makros,
            MAX(f.last_seen_at)                  AS letzter_fund,
            MAX(sr.started_at)                   AS letzter_scan
        FROM idv_files f
        LEFT JOIN idv_register r  ON r.file_id = f.id
        LEFT JOIN scan_runs    sr ON f.last_scan_run_id = sr.id
        WHERE f.status = 'active'
          AND f.share_root IS NOT NULL
        GROUP BY f.share_root
        ORDER BY dateien_gesamt DESC
    """).fetchall()

    # ── 4. Scan-Lauf-Übersicht (letzte 20) ───────────────────────────────
    try:
        scan_laeufe = db.execute("""
            SELECT id, started_at, finished_at, scan_paths,
                   total_files, new_files, changed_files,
                   moved_files, restored_files, archived_files, errors
            FROM scan_runs
            ORDER BY started_at DESC
            LIMIT 20
        """).fetchall()
    except Exception:
        scan_laeufe = []

    def _parse_paths(raw):
        try:
            return json.loads(raw or "[]")
        except Exception:
            return [raw] if raw else []

    # ═══ Visualisierungs-Tab: Aggregationen für ApexCharts ═══════════════════
    chart_data = _build_chart_data(db)

    return render_template("reports/index.html",
        by_oe=by_oe,
        by_fv=by_fv,
        by_path=by_path,
        scan_laeufe=scan_laeufe,
        parse_paths=_parse_paths,
        chart_data=chart_data,
        kpi=chart_data["kpi"],
    )


def _build_chart_data(db) -> dict:
    """Aggregiert alle Zahlen für den Visualisierungs-Tab in ein JSON-serialisierbares dict."""

    # ── KPI-Kacheln ──────────────────────────────────────────────────────
    kpi_row = db.execute(f"""
        SELECT
            COUNT(*)                                                      AS gesamt,
            SUM(CASE WHEN status IN {APPROVED_STATUSES} THEN 1 ELSE 0 END) AS freigegeben,
            SUM(CASE WHEN naechste_pruefung < date('now')
                      AND status NOT IN ('Archiviert','Abgekündigt') THEN 1 ELSE 0 END) AS ueberfaellig,
            SUM(CASE WHEN EXISTS(SELECT 1 FROM idv_wesentlichkeit iw
                                 WHERE iw.idv_db_id = idv_register.id AND iw.erfuellt = 1)
                     THEN 1 ELSE 0 END)                                    AS wesentlich
        FROM idv_register
        WHERE status NOT IN ('Archiviert')
    """).fetchone()
    kpi = {
        "gesamt": kpi_row["gesamt"] or 0,
        "freigegeben": kpi_row["freigegeben"] or 0,
        "wesentlich": kpi_row["wesentlich"] or 0,
        "ueberfaellig": kpi_row["ueberfaellig"] or 0,
    }

    # ── Chart 1: OE × Status (gestapelter Balken) ────────────────────────
    rows = db.execute("""
        SELECT COALESCE(ou.bezeichnung, 'Keine OE') AS oe,
               r.status                             AS status,
               COUNT(*)                             AS n
        FROM idv_register r
        LEFT JOIN org_units ou ON r.org_unit_id = ou.id
        WHERE r.status NOT IN ('Archiviert')
        GROUP BY oe, r.status
    """).fetchall()
    oe_names: list[str] = []
    status_per_oe: dict[str, dict[str, int]] = {}
    for r in rows:
        oe = r["oe"]
        if oe not in status_per_oe:
            status_per_oe[oe] = {}
            oe_names.append(oe)
        status_per_oe[oe][r["status"] or "Entwurf"] = r["n"]
    # Nach Gesamtanzahl absteigend sortieren (größte OE oben im horizontalen Balken)
    oe_names.sort(key=lambda o: -sum(status_per_oe[o].values()))
    # Nur Status-Werte behalten, die tatsächlich vorkommen — in der definierten Reihenfolge
    used_statuses = [s for s in STATUS_ORDER if any(s in status_per_oe[o] for o in oe_names)]
    # Plus unbekannte Status-Werte (für Vollständigkeit), ohne definierten Farbwert
    for o in oe_names:
        for s in status_per_oe[o]:
            if s not in used_statuses:
                used_statuses.append(s)
    by_oe_status = {
        "oes": oe_names,
        "series": [
            {
                "name": s,
                "color": STATUS_COLORS.get(s, "#64748b"),
                "data": [status_per_oe[o].get(s, 0) for o in oe_names],
            }
            for s in used_statuses
        ],
    }

    # ── Chart 2: Status-Donut gesamt ─────────────────────────────────────
    rows = db.execute("""
        SELECT COALESCE(status, 'Entwurf') AS status, COUNT(*) AS n
        FROM idv_register
        WHERE status NOT IN ('Archiviert')
        GROUP BY status
        ORDER BY n DESC
    """).fetchall()
    status_donut = {
        "labels": [r["status"] for r in rows],
        "values": [r["n"] for r in rows],
        "colors": [STATUS_COLORS.get(r["status"], "#64748b") for r in rows],
    }

    # ── Chart 3: idv_typ (technisch) ─────────────────────────────────────
    rows = db.execute("""
        SELECT COALESCE(NULLIF(idv_typ, ''), 'unklassifiziert') AS typ, COUNT(*) AS n
        FROM idv_register
        WHERE status NOT IN ('Archiviert')
        GROUP BY typ
        ORDER BY n DESC
    """).fetchall()
    idv_typ_donut = {
        "labels": [r["typ"] for r in rows],
        "values": [r["n"] for r in rows],
    }

    # ── Chart 4: entwicklungsart (regulatorisch) ─────────────────────────
    rows = db.execute("""
        SELECT COALESCE(NULLIF(entwicklungsart, ''), 'unbekannt') AS art, COUNT(*) AS n
        FROM idv_register
        WHERE status NOT IN ('Archiviert')
        GROUP BY art
        ORDER BY n DESC
    """).fetchall()
    entwicklungsart_donut = {
        "labels": [r["art"] for r in rows],
        "values": [r["n"] for r in rows],
    }

    # ── Chart 5: Zeitverlauf (24 Monate) ─────────────────────────────────
    monate = _month_range(24)
    monat_idx = {m: i for i, m in enumerate(monate)}

    reg_rows = db.execute("""
        SELECT strftime('%Y-%m', erstellt_am) AS monat, COUNT(*) AS n
        FROM idv_register
        WHERE erstellt_am >= date('now','-24 months')
        GROUP BY monat
    """).fetchall()
    registriert_gesamt = [0] * len(monate)
    for r in reg_rows:
        i = monat_idx.get(r["monat"])
        if i is not None:
            registriert_gesamt[i] = r["n"]

    frei_rows = db.execute(f"""
        SELECT strftime('%Y-%m', status_geaendert_am) AS monat, COUNT(*) AS n
        FROM idv_register
        WHERE status IN {APPROVED_STATUSES}
          AND status_geaendert_am >= date('now','-24 months')
        GROUP BY monat
    """).fetchall()
    freigegeben_gesamt = [0] * len(monate)
    for r in frei_rows:
        i = monat_idx.get(r["monat"])
        if i is not None:
            freigegeben_gesamt[i] = r["n"]

    # Pro OE — nur Registrierungen (für Umschalter "Pro Fachbereich")
    per_oe_rows = db.execute("""
        SELECT COALESCE(ou.bezeichnung,'Keine OE')      AS oe,
               strftime('%Y-%m', r.erstellt_am)         AS monat,
               COUNT(*)                                  AS n
        FROM idv_register r
        LEFT JOIN org_units ou ON r.org_unit_id = ou.id
        WHERE r.erstellt_am >= date('now','-24 months')
        GROUP BY oe, monat
    """).fetchall()
    by_oe_ts: dict[str, list[int]] = {}
    for r in per_oe_rows:
        series = by_oe_ts.setdefault(r["oe"], [0] * len(monate))
        i = monat_idx.get(r["monat"])
        if i is not None:
            series[i] = r["n"]
    # Top 8 OEs nach Summe (sonst wird die Legende unlesbar); Rest aggregieren
    sorted_oes = sorted(by_oe_ts.items(), key=lambda kv: -sum(kv[1]))
    top_oes = sorted_oes[:8]
    rest_oes = sorted_oes[8:]
    verlauf_by_oe_series = [{"name": name, "data": data} for name, data in top_oes]
    if rest_oes:
        rest_sum = [0] * len(monate)
        for _, data in rest_oes:
            for i, v in enumerate(data):
                rest_sum[i] += v
        verlauf_by_oe_series.append({"name": f"Weitere ({len(rest_oes)})", "data": rest_sum})

    verlauf = {
        "monate": monate,
        "gesamt": {
            "registriert": registriert_gesamt,
            "freigegeben": freigegeben_gesamt,
        },
        "by_oe": verlauf_by_oe_series,
    }

    # ── Chart 6: Heatmap OE × idv_typ ────────────────────────────────────
    rows = db.execute("""
        SELECT COALESCE(ou.bezeichnung,'Keine OE')              AS oe,
               COALESCE(NULLIF(r.idv_typ,''),'unklassifiziert') AS typ,
               COUNT(*)                                          AS n
        FROM idv_register r
        LEFT JOIN org_units ou ON r.org_unit_id = ou.id
        WHERE r.status NOT IN ('Archiviert')
        GROUP BY oe, typ
    """).fetchall()
    oe_set: list[str] = []
    typ_set: list[str] = []
    grid: dict[tuple[str, str], int] = {}
    for r in rows:
        grid[(r["oe"], r["typ"])] = r["n"]
        if r["oe"] not in oe_set:
            oe_set.append(r["oe"])
        if r["typ"] not in typ_set:
            typ_set.append(r["typ"])
    # Achsen nach Volumen sortieren
    oe_set.sort(key=lambda o: -sum(grid.get((o, t), 0) for t in typ_set))
    typ_set.sort(key=lambda t: -sum(grid.get((o, t), 0) for o in oe_set))
    # ApexCharts-Heatmap erwartet Serien — je Zeile (typ) eine Serie mit {x: oe, y: n}
    heatmap = {
        "series": [
            {
                "name": t,
                "data": [{"x": o, "y": grid.get((o, t), 0)} for o in oe_set],
            }
            for t in typ_set
        ],
    }

    # ── Chart 7: Freigabe-Funnel ─────────────────────────────────────────
    status_counts = {row["status"]: row["n"] for row in db.execute("""
        SELECT COALESCE(status,'Entwurf') AS status, COUNT(*) AS n
        FROM idv_register
        WHERE status NOT IN ('Archiviert')
        GROUP BY status
    """).fetchall()}
    in_pruefung = status_counts.get("In Prüfung", 0)
    freigegeben_count = sum(status_counts.get(s, 0) for s in APPROVED_STATUSES)
    funnel = {
        "labels": ["Entwurf", "In Prüfung", "Freigegeben"],
        "values": [
            status_counts.get("Entwurf", 0),
            in_pruefung,
            freigegeben_count,
        ],
    }

    # ── Chart 8: Wesentlichkeits-Ampel je OE ─────────────────────────────
    rows = db.execute(f"""
        SELECT COALESCE(ou.bezeichnung,'Keine OE')                 AS oe,
               SUM(CASE WHEN EXISTS(SELECT 1 FROM idv_wesentlichkeit iw
                                    WHERE iw.idv_db_id=r.id AND iw.erfuellt=1)
                        THEN 1 ELSE 0 END)                          AS wesentlich,
               SUM(CASE WHEN EXISTS(SELECT 1 FROM idv_wesentlichkeit iw
                                    WHERE iw.idv_db_id=r.id AND iw.erfuellt=1)
                         AND r.naechste_pruefung < date('now')
                         AND r.status NOT IN ('Archiviert','Abgekündigt')
                        THEN 1 ELSE 0 END)                          AS ueberfaellig,
               SUM(CASE WHEN EXISTS(SELECT 1 FROM idv_wesentlichkeit iw
                                    WHERE iw.idv_db_id=r.id AND iw.erfuellt=1)
                         AND r.status IN {APPROVED_STATUSES}
                        THEN 1 ELSE 0 END)                          AS freigegeben
        FROM idv_register r
        LEFT JOIN org_units ou ON r.org_unit_id = ou.id
        WHERE r.status NOT IN ('Archiviert')
        GROUP BY oe
        HAVING wesentlich > 0
        ORDER BY wesentlich DESC
        LIMIT 15
    """).fetchall()
    ampel = {
        "oes": [r["oe"] for r in rows],
        "wesentlich": [r["wesentlich"] for r in rows],
        "freigegeben": [r["freigegeben"] for r in rows],
        "ueberfaellig": [r["ueberfaellig"] for r in rows],
    }

    return {
        "kpi": kpi,
        "by_oe_status": by_oe_status,
        "status_donut": status_donut,
        "idv_typ_donut": idv_typ_donut,
        "entwicklungsart_donut": entwicklungsart_donut,
        "verlauf": verlauf,
        "heatmap": heatmap,
        "funnel": funnel,
        "ampel": ampel,
    }
