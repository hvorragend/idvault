"""archiviert_sichtbar: v_idv_uebersicht zeigt alle Statuses inkl. Archiviert

Revision ID: 0004_archiviert_sichtbar
Revises: 0003_freigabe_pools
Create Date: 2026-04-22
"""

from __future__ import annotations

from alembic import op


revision = "0004_archiviert_sichtbar"
down_revision = "0003_freigabe_pools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # v_idv_uebersicht ohne Status-Filter neu erstellen – die Filterung
    # auf nicht-archivierte Einträge übernimmt jetzt die Anwendungsschicht.
    bind.exec_driver_sql("DROP VIEW IF EXISTS v_idv_uebersicht")
    bind.exec_driver_sql("""
        CREATE VIEW v_idv_uebersicht AS
        SELECT
            r.id                        AS idv_db_id,
            r.idv_id,
            r.bezeichnung,
            r.idv_typ,
            r.status,
            CASE WHEN EXISTS (
                SELECT 1 FROM idv_wesentlichkeit iw
                WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1
            ) THEN 'Ja' ELSE 'Nein' END AS ist_wesentlich,
            gp.gp_nummer,
            gp.bezeichnung              AS geschaeftsprozess,
            ou.bezeichnung              AS org_einheit,
            p_fv.nachname || ', ' || p_fv.vorname AS fachverantwortlicher,
            p_en.nachname || ', ' || p_en.vorname AS entwickler,
            r.naechste_pruefung,
            CASE
                WHEN r.naechste_pruefung < date('now') THEN 'ÜBERFÄLLIG'
                WHEN r.naechste_pruefung < date('now', '+30 days') THEN 'BALD FÄLLIG'
                ELSE 'OK'
            END                         AS pruefstatus,
            r.abloesung_geplant,
            r.abloesung_zieldatum,
            f.file_name,
            f.full_path,
            f.modified_at               AS datei_geaendert,
            r.erstellt_am,
            r.aktualisiert_am
        FROM idv_register r
        LEFT JOIN geschaeftsprozesse   gp   ON r.gp_id = gp.id
        LEFT JOIN org_units            ou   ON r.org_unit_id = ou.id
        LEFT JOIN persons              p_fv ON r.fachverantwortlicher_id = p_fv.id
        LEFT JOIN persons              p_en ON r.idv_entwickler_id = p_en.id
        LEFT JOIN idv_files            f    ON r.file_id = f.id
    """)

    # v_kritische_idvs: archivierte Einträge explizit ausschließen
    bind.exec_driver_sql("DROP VIEW IF EXISTS v_kritische_idvs")
    bind.exec_driver_sql("""
        CREATE VIEW v_kritische_idvs AS
        SELECT * FROM v_idv_uebersicht
        WHERE ist_wesentlich = 'Ja'
          AND status != 'Archiviert'
        ORDER BY bezeichnung
    """)


def downgrade() -> None:
    bind = op.get_bind()

    bind.exec_driver_sql("DROP VIEW IF EXISTS v_idv_uebersicht")
    bind.exec_driver_sql("""
        CREATE VIEW v_idv_uebersicht AS
        SELECT
            r.id                        AS idv_db_id,
            r.idv_id,
            r.bezeichnung,
            r.idv_typ,
            r.status,
            CASE WHEN EXISTS (
                SELECT 1 FROM idv_wesentlichkeit iw
                WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1
            ) THEN 'Ja' ELSE 'Nein' END AS ist_wesentlich,
            gp.gp_nummer,
            gp.bezeichnung              AS geschaeftsprozess,
            ou.bezeichnung              AS org_einheit,
            p_fv.nachname || ', ' || p_fv.vorname AS fachverantwortlicher,
            p_en.nachname || ', ' || p_en.vorname AS entwickler,
            r.naechste_pruefung,
            CASE
                WHEN r.naechste_pruefung < date('now') THEN 'ÜBERFÄLLIG'
                WHEN r.naechste_pruefung < date('now', '+30 days') THEN 'BALD FÄLLIG'
                ELSE 'OK'
            END                         AS pruefstatus,
            r.abloesung_geplant,
            r.abloesung_zieldatum,
            f.file_name,
            f.full_path,
            f.modified_at               AS datei_geaendert,
            r.erstellt_am,
            r.aktualisiert_am
        FROM idv_register r
        LEFT JOIN geschaeftsprozesse   gp   ON r.gp_id = gp.id
        LEFT JOIN org_units            ou   ON r.org_unit_id = ou.id
        LEFT JOIN persons              p_fv ON r.fachverantwortlicher_id = p_fv.id
        LEFT JOIN persons              p_en ON r.idv_entwickler_id = p_en.id
        LEFT JOIN idv_files            f    ON r.file_id = f.id
        WHERE r.status NOT IN ('Archiviert')
    """)

    bind.exec_driver_sql("DROP VIEW IF EXISTS v_kritische_idvs")
    bind.exec_driver_sql("""
        CREATE VIEW v_kritische_idvs AS
        SELECT * FROM v_idv_uebersicht
        WHERE ist_wesentlich = 'Ja'
        ORDER BY bezeichnung
    """)
