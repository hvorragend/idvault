"""Prüfer-Excel-Export: IDV-Register, Maßnahmen, Prüfungen, Nachweise.

Erzeugt eine mehrseitige Arbeitsmappe mit Deckblatt und bedingter
Formatierung (Ampel für Fristen). Wird von ``admin.export_excel`` aufgerufen.
"""
from __future__ import annotations

import io
import sqlite3
from datetime import date, datetime
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo


# ---------------------------------------------------------------------------
# Formatierungs-Helfer
# ---------------------------------------------------------------------------

_HEADER_FILL  = PatternFill("solid", fgColor="1F3A5F")
_HEADER_FONT  = Font(color="FFFFFF", bold=True)
_TITLE_FONT   = Font(size=16, bold=True, color="1F3A5F")

_AMPEL_ROT    = PatternFill("solid", fgColor="F8D7DA")
_AMPEL_GELB   = PatternFill("solid", fgColor="FFF3CD")
_AMPEL_GRUEN  = PatternFill("solid", fgColor="D1E7DD")
_AMPEL_GRAU   = PatternFill("solid", fgColor="E9ECEF")


def _parse_iso(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _faelligkeit_fill(faellig: Optional[str], erledigt: bool) -> Optional[PatternFill]:
    """Ampel-Fill je nach Fälligkeits-/Erledigungs-Status."""
    if erledigt:
        return _AMPEL_GRAU
    d = _parse_iso(faellig)
    if not d:
        return None
    heute = date.today()
    delta = (d - heute).days
    if delta < 0:
        return _AMPEL_ROT
    if delta <= 30:
        return _AMPEL_GELB
    return _AMPEL_GRUEN


def _write_header(ws, labels: list[tuple[str, int]], row: int = 1) -> None:
    for col_idx, (label, width) in enumerate(labels, start=1):
        cell = ws.cell(row=row, column=col_idx, value=label)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(vertical="center", horizontal="left")
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.row_dimensions[row].height = 22
    ws.freeze_panes = ws.cell(row=row + 1, column=1)


def _autofilter(ws, n_cols: int, n_rows: int) -> None:
    if n_rows < 1:
        return
    ws.auto_filter.ref = f"A1:{get_column_letter(n_cols)}{n_rows + 1}"


# ---------------------------------------------------------------------------
# Sheet-Builder
# ---------------------------------------------------------------------------

def _sheet_deckblatt(wb: Workbook, db: sqlite3.Connection) -> None:
    ws = wb.active
    ws.title = "Deckblatt"

    ws["A1"] = "IDV-Register – Prüfer-Export"
    ws["A1"].font = _TITLE_FONT
    ws["A2"] = f"Stichtag: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ws["A2"].font = Font(italic=True, color="475569")

    aktive_idvs = db.execute(
        "SELECT COUNT(*) FROM idv_register WHERE status NOT IN ('Außer Betrieb','Abgelöst')"
    ).fetchone()[0]
    wesentliche = db.execute("""
        SELECT COUNT(*) FROM idv_register r
         WHERE r.status NOT IN ('Außer Betrieb','Abgelöst')
           AND EXISTS (SELECT 1 FROM idv_wesentlichkeit w
                        JOIN wesentlichkeitskriterien k ON k.id = w.kriterium_id
                        WHERE w.idv_db_id = r.id AND w.erfuellt = 1 AND k.aktiv = 1)
    """).fetchone()[0]
    massnahmen_offen = db.execute(
        "SELECT COUNT(*) FROM massnahmen WHERE status IN ('Offen','In Bearbeitung')"
    ).fetchone()[0]
    massnahmen_ueberfaellig = db.execute(
        "SELECT COUNT(*) FROM massnahmen"
        " WHERE status IN ('Offen','In Bearbeitung') AND faellig_am < date('now')"
    ).fetchone()[0]
    pruefungen_faellig = db.execute("""
        SELECT COUNT(*) FROM idv_register
         WHERE status NOT IN ('Außer Betrieb','Abgelöst')
           AND naechste_pruefung IS NOT NULL
           AND naechste_pruefung < date('now','+30 day')
    """).fetchone()[0]

    rows = [
        ("Aktive IDVs",                   aktive_idvs),
        ("Davon wesentlich",              wesentliche),
        ("Offene Maßnahmen",              massnahmen_offen),
        ("Davon überfällig",              massnahmen_ueberfaellig),
        ("Prüfungen fällig (≤30 Tage)",   pruefungen_faellig),
    ]
    for i, (label, value) in enumerate(rows, start=4):
        ws.cell(row=i, column=1, value=label).font = Font(bold=True)
        ws.cell(row=i, column=2, value=value)

    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 14

    ws["A11"] = ("Arbeitsmappe enthält: Register, Maßnahmen, Prüfungen, Nachweise (Freigaben). "
                 "Farben in den Fristen-Spalten: rot = überfällig, gelb = fällig innerhalb 30 Tagen, "
                 "grün = mehr als 30 Tage, grau = erledigt/abgeschlossen.")
    ws["A11"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("A11:D14")


def _sheet_register(wb: Workbook, db: sqlite3.Connection) -> None:
    ws = wb.create_sheet("Register")
    header = [
        ("IDV-ID",                 14),
        ("Bezeichnung",            38),
        ("Status",                 14),
        ("Entwicklungsart",        18),
        ("IDV-Typ",                18),
        ("Wesentlich",             12),
        ("Organisationseinheit",   24),
        ("Fachverantwortlicher",   24),
        ("Entwickler",             24),
        ("Koordinator",            24),
        ("Version",                10),
        ("Produktiv seit",         14),
        ("Letzte Prüfung",         16),
        ("Nächste Prüfung",        16),
        ("Prüfintervall (Mon.)",   18),
    ]
    _write_header(ws, header)

    rows = db.execute("""
        SELECT r.idv_id, r.bezeichnung, r.status, r.entwicklungsart, r.idv_typ,
               EXISTS (
                   SELECT 1 FROM idv_wesentlichkeit w
                   JOIN wesentlichkeitskriterien k ON k.id = w.kriterium_id
                   WHERE w.idv_db_id = r.id AND w.erfuellt = 1 AND k.aktiv = 1
               ) AS wesentlich,
               ou.bezeichnung AS oe,
               (pf.nachname || ', ' || pf.vorname)   AS fv,
               (pe.nachname || ', ' || pe.vorname)   AS entwickler,
               (pk.nachname || ', ' || pk.vorname)   AS koordinator,
               r.version, r.produktiv_seit,
               (SELECT MAX(pruefungsdatum) FROM pruefungen p WHERE p.idv_id = r.id
                  AND p.abgeschlossen = 1) AS letzte_pruefung,
               r.naechste_pruefung, r.pruefintervall_monate
          FROM idv_register r
          LEFT JOIN org_units ou ON ou.id = r.org_unit_id
          LEFT JOIN persons  pf  ON pf.id = r.fachverantwortlicher_id
          LEFT JOIN persons  pe  ON pe.id = r.idv_entwickler_id
          LEFT JOIN persons  pk  ON pk.id = r.idv_koordinator_id
         ORDER BY r.idv_id
    """).fetchall()

    for i, r in enumerate(rows, start=2):
        ws.cell(row=i, column=1,  value=r["idv_id"])
        ws.cell(row=i, column=2,  value=r["bezeichnung"])
        ws.cell(row=i, column=3,  value=r["status"])
        ws.cell(row=i, column=4,  value=r["entwicklungsart"])
        ws.cell(row=i, column=5,  value=r["idv_typ"])
        ws.cell(row=i, column=6,  value="Ja" if r["wesentlich"] else "")
        ws.cell(row=i, column=7,  value=r["oe"])
        ws.cell(row=i, column=8,  value=r["fv"])
        ws.cell(row=i, column=9,  value=r["entwickler"])
        ws.cell(row=i, column=10, value=r["koordinator"])
        ws.cell(row=i, column=11, value=r["version"])
        ws.cell(row=i, column=12, value=r["produktiv_seit"])
        ws.cell(row=i, column=13, value=r["letzte_pruefung"])
        c_faelligkeit = ws.cell(row=i, column=14, value=r["naechste_pruefung"])
        fill = _faelligkeit_fill(r["naechste_pruefung"], erledigt=False)
        if fill:
            c_faelligkeit.fill = fill
        ws.cell(row=i, column=15, value=r["pruefintervall_monate"])

    _autofilter(ws, len(header), len(rows))


def _sheet_massnahmen(wb: Workbook, db: sqlite3.Connection) -> None:
    ws = wb.create_sheet("Maßnahmen")
    header = [
        ("IDV-ID",              14),
        ("IDV-Bezeichnung",     32),
        ("Titel",               38),
        ("Typ",                 18),
        ("Priorität",           12),
        ("Status",              16),
        ("Verantwortlicher",    24),
        ("Fällig am",           14),
        ("Erledigt am",         14),
    ]
    _write_header(ws, header)

    rows = db.execute("""
        SELECT r.idv_id, r.bezeichnung,
               m.titel, m.massnahmentyp, m.prioritaet, m.status,
               (p.nachname || ', ' || p.vorname) AS verantwortlicher,
               m.faellig_am, m.erledigt_am
          FROM massnahmen m
          JOIN idv_register r ON r.id = m.idv_id
          LEFT JOIN persons p ON p.id = m.verantwortlicher_id
         ORDER BY m.faellig_am IS NULL, m.faellig_am, r.idv_id
    """).fetchall()

    for i, r in enumerate(rows, start=2):
        erledigt = r["status"] == "Erledigt"
        ws.cell(row=i, column=1, value=r["idv_id"])
        ws.cell(row=i, column=2, value=r["bezeichnung"])
        ws.cell(row=i, column=3, value=r["titel"])
        ws.cell(row=i, column=4, value=r["massnahmentyp"])
        ws.cell(row=i, column=5, value=r["prioritaet"])
        ws.cell(row=i, column=6, value=r["status"])
        ws.cell(row=i, column=7, value=r["verantwortlicher"])
        c_faellig = ws.cell(row=i, column=8, value=r["faellig_am"])
        fill = _faelligkeit_fill(r["faellig_am"], erledigt=erledigt)
        if fill:
            c_faellig.fill = fill
        ws.cell(row=i, column=9, value=r["erledigt_am"])

    _autofilter(ws, len(header), len(rows))


def _sheet_pruefungen(wb: Workbook, db: sqlite3.Connection) -> None:
    ws = wb.create_sheet("Prüfungen")
    header = [
        ("IDV-ID",             14),
        ("IDV-Bezeichnung",    32),
        ("Prüfungsart",        18),
        ("Prüfungsdatum",      14),
        ("Prüfer",             24),
        ("Ergebnis",           18),
        ("Maßn. erforderlich", 18),
        ("Frist Maßnahmen",    16),
        ("Abgeschlossen",      14),
        ("Nächste Prüfung",    16),
    ]
    _write_header(ws, header)

    rows = db.execute("""
        SELECT r.idv_id, r.bezeichnung,
               p.pruefungsart, p.pruefungsdatum,
               (pe.nachname || ', ' || pe.vorname) AS pruefer,
               p.ergebnis, p.massnahmen_erforderlich, p.frist_massnahmen,
               p.abgeschlossen, p.naechste_pruefung
          FROM pruefungen p
          JOIN idv_register r ON r.id = p.idv_id
          LEFT JOIN persons  pe ON pe.id = p.pruefer_id
         ORDER BY p.pruefungsdatum DESC, r.idv_id
    """).fetchall()

    for i, r in enumerate(rows, start=2):
        abgeschlossen = bool(r["abgeschlossen"])
        ws.cell(row=i, column=1,  value=r["idv_id"])
        ws.cell(row=i, column=2,  value=r["bezeichnung"])
        ws.cell(row=i, column=3,  value=r["pruefungsart"])
        ws.cell(row=i, column=4,  value=r["pruefungsdatum"])
        ws.cell(row=i, column=5,  value=r["pruefer"])
        c_erg = ws.cell(row=i, column=6, value=r["ergebnis"])
        if (r["ergebnis"] or "").startswith("Kritisch") or r["ergebnis"] == "Nicht bestanden":
            c_erg.fill = _AMPEL_ROT
        ws.cell(row=i, column=7,  value="Ja" if r["massnahmen_erforderlich"] else "Nein")
        c_frist = ws.cell(row=i, column=8, value=r["frist_massnahmen"])
        fill = _faelligkeit_fill(r["frist_massnahmen"], erledigt=abgeschlossen)
        if fill:
            c_frist.fill = fill
        ws.cell(row=i, column=9,  value="Ja" if abgeschlossen else "Nein")
        ws.cell(row=i, column=10, value=r["naechste_pruefung"])

    _autofilter(ws, len(header), len(rows))


def _sheet_nachweise(wb: Workbook, db: sqlite3.Connection) -> None:
    ws = wb.create_sheet("Nachweise")
    header = [
        ("IDV-ID",           14),
        ("IDV-Bezeichnung",  32),
        ("Schritt",          30),
        ("Status",           16),
        ("Beauftragt am",    16),
        ("Beauftragt von",   22),
        ("Zugewiesen an",    22),
        ("Durchgeführt am",  16),
        ("Durchgeführt von", 22),
        ("Archiv-SHA256",    24),
    ]
    _write_header(ws, header)

    rows = db.execute("""
        SELECT r.idv_id, r.bezeichnung,
               f.schritt, f.status,
               f.beauftragt_am,
               (pb.nachname || ', ' || pb.vorname) AS beauftragt_von,
               (pz.nachname || ', ' || pz.vorname) AS zugewiesen_an,
               f.durchgefuehrt_am,
               (pd.nachname || ', ' || pd.vorname) AS durchgefuehrt_von,
               f.archiv_datei_sha256
          FROM idv_freigaben f
          JOIN idv_register r  ON r.id = f.idv_id
          LEFT JOIN persons pb ON pb.id = f.beauftragt_von_id
          LEFT JOIN persons pz ON pz.id = f.zugewiesen_an_id
          LEFT JOIN persons pd ON pd.id = f.durchgefuehrt_von_id
         ORDER BY r.idv_id, f.beauftragt_am
    """).fetchall()

    for i, r in enumerate(rows, start=2):
        ws.cell(row=i, column=1,  value=r["idv_id"])
        ws.cell(row=i, column=2,  value=r["bezeichnung"])
        ws.cell(row=i, column=3,  value=r["schritt"])
        c_status = ws.cell(row=i, column=4, value=r["status"])
        if r["status"] == "Erledigt":
            c_status.fill = _AMPEL_GRUEN
        elif r["status"] in ("Nicht erledigt", "Abgebrochen"):
            c_status.fill = _AMPEL_ROT
        elif r["status"] == "Ausstehend":
            c_status.fill = _AMPEL_GELB
        ws.cell(row=i, column=5,  value=(r["beauftragt_am"] or "")[:10])
        ws.cell(row=i, column=6,  value=r["beauftragt_von"])
        ws.cell(row=i, column=7,  value=r["zugewiesen_an"])
        ws.cell(row=i, column=8,  value=(r["durchgefuehrt_am"] or "")[:10])
        ws.cell(row=i, column=9,  value=r["durchgefuehrt_von"])
        ws.cell(row=i, column=10, value=r["archiv_datei_sha256"])

    _autofilter(ws, len(header), len(rows))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_register_workbook(db: sqlite3.Connection) -> Workbook:
    """Baut die vollständige Prüfer-Arbeitsmappe auf."""
    wb = Workbook()
    _sheet_deckblatt(wb, db)
    _sheet_register(wb, db)
    _sheet_massnahmen(wb, db)
    _sheet_pruefungen(wb, db)
    _sheet_nachweise(wb, db)
    return wb


def register_excel_bytes(db: sqlite3.Connection) -> bytes:
    """Serialisiert die Arbeitsmappe für ``send_file`` bzw. Response-Body."""
    wb = build_register_workbook(db)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
