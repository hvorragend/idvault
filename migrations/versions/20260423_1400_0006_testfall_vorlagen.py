"""testfall_vorlagen: Vorlagen-Bibliothek für Testfälle pro IDV-Typ

Revision ID: 0006_testfall_vorlagen
Revises: 0005_fund_pfad_profile
Create Date: 2026-04-23
"""

from __future__ import annotations

from alembic import op


revision = "0006_testfall_vorlagen"
down_revision = "0005_fund_pfad_profile"
branch_labels = None
depends_on = None


_SEED_VORLAGEN = [
    # (titel, idv_typ, art, beschreibung, parametrisierung, testdaten, erwartetes_ergebnis)
    (
        "Excel-Makro: Kernprüfung",
        "Excel-Makro",
        "fachlich",
        "<p>Prüfung der Arbeitsmappe inkl. VBA-Makros auf korrekte "
        "Berechnung gegen eine Referenz.</p><ul>"
        "<li>Signatur der VBA-Makros</li>"
        "<li>Externe Verknüpfungen (ggf. gebrochen)</li>"
        "<li>Formelintegrität auf Referenz-Blatt</li></ul>",
        "<p>Makros aktiv, Datei-Eigenschaften aus Scan: Makros vorhanden, "
        "Blattschutz <em>(ja/nein)</em>, externe Verknüpfungen <em>(ja/nein)</em>.</p>",
        "<p>Testdaten aus der Referenz-Mappe des Vorquartals.</p>",
        "<p>Ergebnisabweichung ≤ Rundungsfehler; keine Warnung zu externen Verknüpfungen.</p>",
    ),
    (
        "Excel-Tabelle: Formel-Review",
        "Excel-Tabelle",
        "fachlich",
        "<p>Review aller Formelzellen, Prüfung auf ungesicherten Blattschutz, "
        "benannte Bereiche und externe Verknüpfungen.</p>",
        "<p>Keine Makros, Blattschutz aktiv.</p>",
        "<p>Stichprobe aus dem produktiven Einsatzdatensatz.</p>",
        "<p>Alle Formelzellen nachvollziehbar; Blattschutz auf allen Ergebnisblättern aktiv.</p>",
    ),
    (
        "Access-Datenbank: Abfragen & Berichte",
        "Access-Datenbank",
        "fachlich",
        "<p>Prüfung der Kernabfragen, Berichte und Datenverbindungen.</p><ul>"
        "<li>Korrektheit der Abfrageergebnisse gegen Referenz</li>"
        "<li>Verknüpfungen zu Fremdtabellen (ODBC / CSV)</li>"
        "<li>Berechtigungen auf Datenquelle</li></ul>",
        "<p>Standardverbindung, lesende Rechte.</p>",
        "<p>Produktionsdaten zum Stichtag.</p>",
        "<p>Alle Kernabfragen liefern erwartete Zeilenanzahl und Summe.</p>",
    ),
    (
        "SQL-Skript: Ausführungsprüfung",
        "SQL-Skript",
        "fachlich",
        "<p>Fachliche Plausibilität der Abfrageergebnisse.</p><ul>"
        "<li>Kein implizites Row-Count-Limit</li>"
        "<li>Zeitraum-Parameter korrekt gesetzt</li>"
        "<li>Ergebnis gegen Referenz-Abfrage abgeglichen</li></ul>",
        "<p>Parametrisiert auf Stichtag; lesender DB-Zugriff.</p>",
        "<p>Produktions-DB, Lesezugriff auf relevante Schemata.</p>",
        "<p>Ergebnismenge stimmt mit Referenzabfrage überein (Abweichung 0).</p>",
    ),
    (
        "Python-Skript: End-to-End-Lauf",
        "Python-Skript",
        "fachlich",
        "<p>End-to-End-Ausführung des Skripts mit Referenzinput.</p><ul>"
        "<li>Abhängigkeiten dokumentiert (requirements.txt)</li>"
        "<li>Deterministisches Ergebnis</li>"
        "<li>Logging / Fehlerpfade geprüft</li></ul>",
        "<p>Python ≥ 3.10, requirements erfüllt.</p>",
        "<p>Referenzdatei aus letztem Stichtag.</p>",
        "<p>Ergebnisdatei identisch zur Referenz (Hash-Match).</p>",
    ),
    (
        "Power-BI-Bericht: KPI-Abgleich",
        "Power-BI-Bericht",
        "fachlich",
        "<p>KPI-Abgleich gegen Referenzsystem.</p><ul>"
        "<li>Datenquelle und Refresh-Logik</li>"
        "<li>Zentrale KPIs (Summen, Durchschnitte)</li>"
        "<li>Filterwirkung prüfen</li></ul>",
        "<p>Veröffentlichte Version; Datenquelle wie Produktion.</p>",
        "<p>Referenz-Dashboard zum Stichtag.</p>",
        "<p>Zentrale KPIs stimmen mit Referenz überein; Abweichung ≤ Toleranz.</p>",
    ),
    (
        "Cognos-Report: Report-Abgleich",
        "Cognos-Report",
        "fachlich",
        "<p>Abgleich des Cognos-Berichts gegen den Referenzlauf.</p>",
        "<p>Veröffentlichter Report, aktueller Parameter-Satz.</p>",
        "<p>Referenz-Export zum Stichtag.</p>",
        "<p>Berichtskennzahlen stimmen überein; keine Warnungen im Ausführungsprotokoll.</p>",
    ),
    # Technische Vorlagen (nur ein Feld — Kurzbeschreibung)
    (
        "Excel: technische Basisprüfung",
        None,  # wirkt für alle Excel-Varianten
        "technisch",
        "<p><strong>Technische Prüfpunkte:</strong></p><ul>"
        "<li>Makros signiert / keine unsignierten Module</li>"
        "<li>Externe Verknüpfungen dokumentiert / gebrochen</li>"
        "<li>Blattschutz auf Ergebnisblättern aktiv</li>"
        "<li>Formelzellen gegen versehentliches Überschreiben geschützt</li>"
        "<li>Keine Hardcoded-Pfade</li></ul>",
        "", "", "",
    ),
    (
        "Skript: technische Basisprüfung",
        None,
        "technisch",
        "<p><strong>Technische Prüfpunkte:</strong></p><ul>"
        "<li>Versionierung / Git vorhanden</li>"
        "<li>Abhängigkeiten gepinnt</li>"
        "<li>Keine Zugangsdaten im Code</li>"
        "<li>Logging auf stdout / Datei</li>"
        "<li>Fehlerpfade abgedeckt</li></ul>",
        "", "", "",
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS testfall_vorlagen (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            titel                TEXT    NOT NULL,
            idv_typ              TEXT,
            art                  TEXT    NOT NULL CHECK(art IN ('fachlich','technisch')),
            beschreibung         TEXT,
            parametrisierung     TEXT,
            testdaten            TEXT,
            erwartetes_ergebnis  TEXT,
            aktiv                INTEGER NOT NULL DEFAULT 1,
            created_at           TEXT    NOT NULL DEFAULT (datetime('now','utc')),
            updated_at           TEXT
        )
    """)
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_testfall_vorlagen_lookup "
        "ON testfall_vorlagen(aktiv, art, idv_typ)"
    )

    # Seed-Vorlagen nur einfügen, wenn die Tabelle leer ist (idempotent)
    existing = bind.exec_driver_sql(
        "SELECT COUNT(*) FROM testfall_vorlagen"
    ).fetchone()[0]
    if existing == 0:
        for titel, idv_typ, art, beschr, param, daten, erwartet in _SEED_VORLAGEN:
            bind.exec_driver_sql(
                "INSERT INTO testfall_vorlagen "
                "(titel, idv_typ, art, beschreibung, parametrisierung, "
                " testdaten, erwartetes_ergebnis) "
                "VALUES (?,?,?,?,?,?,?)",
                (titel, idv_typ, art, beschr, param, daten, erwartet),
            )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_testfall_vorlagen_lookup")
    bind.exec_driver_sql("DROP TABLE IF EXISTS testfall_vorlagen")
