"""
IDV-Register Datenbankschicht
=============================
Initialisierung, Migration und Basisfunktionen für das IDV-Register.
Wird von Scanner und Web-Frontend gemeinsam genutzt.
"""

import sys
import sqlite3
import json
import calendar
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional


def _resource_path(relative: str) -> Path:
    """Gibt den korrekten Pfad zurück – auch im PyInstaller-Bundle."""
    if hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS) / relative
    return Path(__file__).parent / relative


# ---------------------------------------------------------------------------
# Verbindung & Initialisierung
# ---------------------------------------------------------------------------

def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_register_db(db_path: str) -> sqlite3.Connection:
    """Initialisiert die Datenbank anhand von schema.sql."""
    conn = get_connection(db_path)
    schema_path = _resource_path("schema.sql")
    if not schema_path.exists():
        raise FileNotFoundError(f"schema.sql nicht gefunden: {schema_path}")
    # Views mit veralteten Spaltenreferenzen vor dem Re-Execute droppen,
    # damit schema.sql sie nicht in alter Form wiederherstellt. Die Views
    # werden nach der Migration neu angelegt (siehe _rebuild_core_views).
    for view in ("v_idv_uebersicht", "v_kritische_idvs",
                 "v_unvollstaendige_idvs", "v_prueffaelligkeiten"):
        try:
            conn.execute(f"DROP VIEW IF EXISTS {view}")
        except sqlite3.OperationalError:
            pass
    sql = schema_path.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
    _apply_incremental_migrations(conn)
    return conn


def _apply_incremental_migrations(conn: sqlite3.Connection) -> None:
    """Ergänzt fehlende Spalten in bestehenden Datenbanken (idempotent)."""
    existing_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(geschaeftsprozesse)").fetchall()
    }
    migrations = [
        ("schutzbedarf_a", "ALTER TABLE geschaeftsprozesse ADD COLUMN schutzbedarf_a TEXT"),
        ("schutzbedarf_c", "ALTER TABLE geschaeftsprozesse ADD COLUMN schutzbedarf_c TEXT"),
        ("schutzbedarf_i", "ALTER TABLE geschaeftsprozesse ADD COLUMN schutzbedarf_i TEXT"),
        ("schutzbedarf_n", "ALTER TABLE geschaeftsprozesse ADD COLUMN schutzbedarf_n TEXT"),
    ]
    for col, stmt in migrations:
        if col not in existing_cols:
            conn.execute(stmt)

    # Archivierung Originaldatei (Phase 3 im Freigabeverfahren, MaRisk AT 7.2):
    # Neue Spalten in idv_freigaben für revisionssichere Archivierung.
    existing_freigaben_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(idv_freigaben)").fetchall()
    }
    freigaben_migrations = [
        ("datei_verfuegbar",    "ALTER TABLE idv_freigaben ADD COLUMN datei_verfuegbar INTEGER"),
        ("archiv_datei_pfad",   "ALTER TABLE idv_freigaben ADD COLUMN archiv_datei_pfad TEXT"),
        ("archiv_datei_name",   "ALTER TABLE idv_freigaben ADD COLUMN archiv_datei_name TEXT"),
        ("archiv_datei_sha256", "ALTER TABLE idv_freigaben ADD COLUMN archiv_datei_sha256 TEXT"),
    ]
    for col, stmt in freigaben_migrations:
        if col not in existing_freigaben_cols:
            conn.execute(stmt)

    # SMTP-Versandlog: neue Tabelle für bestehende Installationen anlegen
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smtp_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at    TEXT    NOT NULL,
            recipients TEXT    NOT NULL,
            subject    TEXT    NOT NULL,
            success    INTEGER NOT NULL DEFAULT 0,
            error_msg  TEXT
        )
    """)

    # Daten-Migration: Status "Genehmigt" → "Freigegeben" (idempotent)
    conn.execute("UPDATE idv_register SET status = 'Freigegeben' WHERE status = 'Genehmigt'")
    conn.execute("UPDATE idv_register SET status = 'Freigegeben mit Auflagen' WHERE status = 'Genehmigt mit Auflagen'")
    conn.commit()

    _ensure_dynamic_wesentlichkeit_tables(conn)
    _migrate_dynamic_wesentlichkeit(conn)
    _rebuild_core_views(conn)


def _rebuild_core_views(conn: sqlite3.Connection) -> None:
    """Legt die Views v_idv_uebersicht, v_kritische_idvs und
    v_unvollstaendige_idvs mit der dynamischen Wesentlichkeits-Logik an.
    Wird auch gegen Bestandsinstallationen ausgeführt, bei denen im Bundle
    noch die alten View-Definitionen mit gda_wert/steuerungsrelevant etc.
    stecken.
    """
    for view in ("v_idv_uebersicht", "v_kritische_idvs",
                 "v_unvollstaendige_idvs"):
        try:
            conn.execute(f"DROP VIEW IF EXISTS {view}")
        except sqlite3.OperationalError:
            pass

    conn.execute("""
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
            rk.bezeichnung              AS risikoklasse,
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
        LEFT JOIN risikoklassen        rk   ON r.risikoklasse_id = rk.id
        LEFT JOIN geschaeftsprozesse   gp   ON r.gp_id = gp.id
        LEFT JOIN org_units            ou   ON r.org_unit_id = ou.id
        LEFT JOIN persons              p_fv ON r.fachverantwortlicher_id = p_fv.id
        LEFT JOIN persons              p_en ON r.idv_entwickler_id = p_en.id
        LEFT JOIN idv_files            f    ON r.file_id = f.id
        WHERE r.status NOT IN ('Archiviert')
    """)

    conn.execute("""
        CREATE VIEW v_kritische_idvs AS
        SELECT * FROM v_idv_uebersicht
        WHERE ist_wesentlich = 'Ja'
        ORDER BY risikoklasse
    """)

    conn.execute("""
        CREATE VIEW v_unvollstaendige_idvs AS
        SELECT
            r.idv_id,
            r.bezeichnung,
            r.status,
            CASE WHEN r.fachverantwortlicher_id IS NULL THEN 1 ELSE 0 END AS fehlt_fachverantwortlicher,
            CASE WHEN r.gp_id IS NULL AND r.gp_freitext IS NULL THEN 1 ELSE 0 END AS fehlt_geschaeftsprozess,
            CASE WHEN r.idv_typ = 'unklassifiziert' THEN 1 ELSE 0 END AS fehlt_typ,
            CASE WHEN EXISTS (
                SELECT 1 FROM idv_wesentlichkeit iw
                JOIN wesentlichkeitskriterien k ON k.id = iw.kriterium_id
                WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1
                  AND k.begruendung_pflicht = 1
                  AND (iw.begruendung IS NULL OR iw.begruendung = '')
            ) THEN 1 ELSE 0 END AS fehlt_wesentlichkeitsbegruendung,
            CASE WHEN r.risikoklasse_id IS NULL THEN 1 ELSE 0 END AS fehlt_risikoklasse,
            r.erstellt_am,
            r.aktualisiert_am
        FROM idv_register r
        WHERE r.status NOT IN ('Archiviert')
          AND (
            r.fachverantwortlicher_id IS NULL
            OR (r.gp_id IS NULL AND r.gp_freitext IS NULL)
            OR r.idv_typ = 'unklassifiziert'
            OR r.risikoklasse_id IS NULL
            OR EXISTS (
                SELECT 1 FROM idv_wesentlichkeit iw
                JOIN wesentlichkeitskriterien k ON k.id = iw.kriterium_id
                WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1
                  AND k.begruendung_pflicht = 1
                  AND (iw.begruendung IS NULL OR iw.begruendung = '')
            )
          )
    """)
    conn.commit()


def _ensure_dynamic_wesentlichkeit_tables(conn: sqlite3.Connection) -> None:
    """Legt die Tabellen und Seed-Daten für die dynamischen Wesentlichkeits-
    kriterien an. Wird auch gegen Bestands-Installationen ausgeführt, bei
    denen `schema.sql` aus dem alten PyInstaller-Bundle geladen wurde und
    die neuen Tabellen deshalb noch nicht enthält. Idempotent.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wesentlichkeitskriterien (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            bezeichnung         TEXT NOT NULL,
            beschreibung        TEXT,
            begruendung_pflicht INTEGER NOT NULL DEFAULT 0,
            sort_order          INTEGER NOT NULL DEFAULT 0,
            aktiv               INTEGER NOT NULL DEFAULT 1,
            erstellt_am         TEXT NOT NULL DEFAULT (datetime('now','utc'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wesentlichkeitskriterium_details (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            kriterium_id    INTEGER NOT NULL REFERENCES wesentlichkeitskriterien(id) ON DELETE CASCADE,
            bezeichnung     TEXT NOT NULL,
            sort_order      INTEGER NOT NULL DEFAULT 0,
            aktiv           INTEGER NOT NULL DEFAULT 1,
            erstellt_am     TEXT NOT NULL DEFAULT (datetime('now','utc')),
            UNIQUE (kriterium_id, bezeichnung)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wk_details_krit ON wesentlichkeitskriterium_details(kriterium_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS idv_wesentlichkeit (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            idv_db_id       INTEGER NOT NULL REFERENCES idv_register(id) ON DELETE CASCADE,
            kriterium_id    INTEGER NOT NULL REFERENCES wesentlichkeitskriterien(id),
            erfuellt        INTEGER NOT NULL DEFAULT 0,
            begruendung     TEXT,
            geaendert_am    TEXT NOT NULL DEFAULT (datetime('now','utc')),
            UNIQUE (idv_db_id, kriterium_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wesentl_idv ON idv_wesentlichkeit(idv_db_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS idv_wesentlichkeit_detail (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            idv_db_id       INTEGER NOT NULL REFERENCES idv_register(id) ON DELETE CASCADE,
            detail_id       INTEGER NOT NULL REFERENCES wesentlichkeitskriterium_details(id) ON DELETE CASCADE,
            UNIQUE (idv_db_id, detail_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wkd_idv ON idv_wesentlichkeit_detail(idv_db_id)")

    # Seed-Kriterien (idempotent: nur anlegen, wenn Bezeichnung noch nicht existiert)
    seeds = [
        ("Rechnungslegungs-Relevanz (GoB)",
         "Anwendung verarbeitet automatisierte Daten, die nach der Verarbeitung "
         "Eingang in die Buchführung finden, z. B. Generierung von "
         "Buchungsbelegen/ -listen, Import aus Schnittstellen etc. oder wenn "
         "anhand von Anwendungen Bilanznachweise (z. B. Berechnung von "
         "Rückstellungen) erstellt werden; allerdings nur, falls keine weiteren "
         "Nachweise vorhanden sind (s.a. IDW RS FAIT 1)",
         1,
         [
             "Generierung von Buchungsbelegen / -listen",
             "Import aus Schnittstellen",
             "Erstellung von Bilanznachweisen (z. B. Berechnung von Rückstellungen)",
         ]),
        ("Risiko / Steuerungs-Relevanz im Sinne der MaRisk",
         "Anwendung verarbeitet Daten, deren Ergebnisse für wesentliche "
         "geschäftspolitische Entscheidungen bzw. die Unternehmenssteuerung "
         "inklusive IKS-Maßnahmen zur Überwachung und Kontrolle der "
         "Geschäftstätigkeit herangezogen werden. Relevant sind dabei "
         "insbesondere Auswertungen, die zur Erfüllung von bankaufsichts"
         "rechtlichen Anforderungen der MaRisk Verwendung finden. Hierzu "
         "zählen beispielsweise Risikoberichte und weitere Auswertungen/"
         "Anwendungen, deren Erstellung auf Grund der Regelungen bzw. zur "
         "Erfüllung der Anforderungen der MaRisk zwingend erforderlich sind.",
         2,
         [
             "Risikobericht / Risikoauswertung",
             "Meldewesen / bankaufsichtsrechtliche Auswertung",
             "Grundlage für geschäftspolitische Entscheidungen",
         ]),
        ("Kritische oder wichtige Funktionen",
         "Mindestens eine kritische oder wichtige Funktion ist vollständig "
         "von dem IKT-Asset/der IKT-Dienstleistung abhängig "
         "(=Abhängigkeitsgrad 4).",
         3,
         []),
    ]
    for bezeichnung, beschreibung, sort_order, details in seeds:
        row = conn.execute(
            "SELECT id FROM wesentlichkeitskriterien WHERE bezeichnung = ?",
            (bezeichnung,),
        ).fetchone()
        if row:
            kid = row[0]
        else:
            cur = conn.execute(
                """INSERT INTO wesentlichkeitskriterien
                    (bezeichnung, beschreibung, begruendung_pflicht, sort_order, aktiv)
                   VALUES (?, ?, 1, ?, 1)""",
                (bezeichnung, beschreibung, sort_order),
            )
            kid = cur.lastrowid
        for order_idx, d_text in enumerate(details, start=1):
            conn.execute(
                """INSERT OR IGNORE INTO wesentlichkeitskriterium_details
                    (kriterium_id, bezeichnung, sort_order) VALUES (?, ?, ?)""",
                (kid, d_text, order_idx),
            )

    conn.commit()


def _migrate_dynamic_wesentlichkeit(conn: sqlite3.Connection) -> None:
    """Migriert hart-kodierte Wesentlichkeitsspalten in die dynamischen Tabellen
    `wesentlichkeitskriterien` / `idv_wesentlichkeit` und entfernt anschließend
    die Alt-Spalten inklusive GDA aus `idv_register`.

    Idempotent: läuft nur, wenn mindestens eine der Alt-Spalten noch existiert.
    """
    idv_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(idv_register)").fetchall()
    }
    legacy_cols = [
        "steuerungsrelevant", "steuerungsrelevanz_begr",
        "relevant_guv", "relevant_meldewesen", "relevant_risikomanagement",
        "rechnungslegungsrelevant", "rechnungslegungsrelevanz_begr",
        "gda_wert", "gda_begruendung",
        "dora_kritisch_wichtig", "dora_begruendung",
    ]
    present_legacy = [c for c in legacy_cols if c in idv_cols]
    if not present_legacy:
        return

    def ensure_criterion(bezeichnung: str, beschreibung: str,
                         sort_order: int) -> int:
        row = conn.execute(
            "SELECT id FROM wesentlichkeitskriterien WHERE bezeichnung = ?",
            (bezeichnung,),
        ).fetchone()
        if row:
            return row[0]
        cur = conn.execute(
            """INSERT INTO wesentlichkeitskriterien
                 (bezeichnung, beschreibung, begruendung_pflicht, sort_order, aktiv)
               VALUES (?, ?, 1, ?, 1)""",
            (bezeichnung, beschreibung, sort_order),
        )
        return cur.lastrowid

    kid_rl = ensure_criterion(
        "Rechnungslegungs-Relevanz (GoB)",
        "Anwendung verarbeitet automatisierte Daten, die nach der Verarbeitung "
        "Eingang in die Buchführung finden, z. B. Generierung von "
        "Buchungsbelegen/ -listen, Import aus Schnittstellen etc. oder wenn "
        "anhand von Anwendungen Bilanznachweise (z. B. Berechnung von "
        "Rückstellungen) erstellt werden; allerdings nur, falls keine weiteren "
        "Nachweise vorhanden sind (s.a. IDW RS FAIT 1)",
        1,
    )
    kid_st = ensure_criterion(
        "Risiko / Steuerungs-Relevanz im Sinne der MaRisk",
        "Anwendung verarbeitet Daten, deren Ergebnisse für wesentliche "
        "geschäftspolitische Entscheidungen bzw. die Unternehmenssteuerung "
        "inklusive IKS-Maßnahmen zur Überwachung und Kontrolle der "
        "Geschäftstätigkeit herangezogen werden. Relevant sind dabei "
        "insbesondere Auswertungen, die zur Erfüllung von bankaufsichts"
        "rechtlichen Anforderungen der MaRisk Verwendung finden. Hierzu "
        "zählen beispielsweise Risikoberichte und weitere Auswertungen/"
        "Anwendungen, deren Erstellung auf Grund der Regelungen bzw. zur "
        "Erfüllung der Anforderungen der MaRisk zwingend erforderlich sind.",
        2,
    )
    kid_dora = ensure_criterion(
        "Kritische oder wichtige Funktionen",
        "Mindestens eine kritische oder wichtige Funktion ist vollständig "
        "von dem IKT-Asset/der IKT-Dienstleistung abhängig "
        "(=Abhängigkeitsgrad 4).",
        3,
    )

    now = datetime.now(timezone.utc).isoformat()

    def migrate_flag(col_flag: str, col_begr: str, kid: int) -> None:
        if col_flag not in idv_cols:
            return
        begr_sel = f"r.{col_begr}" if col_begr in idv_cols else "NULL"
        conn.execute(
            f"""INSERT OR IGNORE INTO idv_wesentlichkeit
                  (idv_db_id, kriterium_id, erfuellt, begruendung, geaendert_am)
                SELECT r.id, ?, 1, {begr_sel}, ?
                  FROM idv_register r
                 WHERE r.{col_flag} = 1""",
            (kid, now),
        )

    migrate_flag("rechnungslegungsrelevant", "rechnungslegungsrelevanz_begr", kid_rl)
    migrate_flag("steuerungsrelevant",      "steuerungsrelevanz_begr",        kid_st)
    migrate_flag("dora_kritisch_wichtig",   "dora_begruendung",               kid_dora)

    # Alt-Views entfernen. Sie referenzieren in Bestandsinstallationen die
    # Legacy-Spalten und würden einen DROP COLUMN sonst blockieren. Die
    # neuen Views werden anschließend in _rebuild_core_views angelegt.
    for view in ("v_idv_uebersicht", "v_kritische_idvs",
                 "v_unvollstaendige_idvs"):
        try:
            conn.execute(f"DROP VIEW IF EXISTS {view}")
        except sqlite3.OperationalError:
            pass

    # Alt-Indizes entfernen (SQLite verhindert sonst den DROP COLUMN). Auch
    # eventuell in Bestandsdatenbanken vorhandene zusätzliche Indizes auf
    # den Legacy-Spalten werden hier ermittelt und gedroppt.
    for idx in ("idx_idv_gda", "idx_idv_steuerung"):
        try:
            conn.execute(f"DROP INDEX IF EXISTS {idx}")
        except sqlite3.OperationalError:
            pass

    for col in present_legacy:
        for idx_name, idx_sql in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='idv_register'"
        ).fetchall() or []:
            if idx_sql and col in idx_sql.lower():
                try:
                    conn.execute(f'DROP INDEX IF EXISTS "{idx_name}"')
                except sqlite3.OperationalError:
                    pass

    for col in present_legacy:
        try:
            conn.execute(f"ALTER TABLE idv_register DROP COLUMN {col}")
        except sqlite3.OperationalError as exc:
            # SQLite < 3.35 oder referenzierte Abhängigkeit: stehen lassen,
            # Anwendung liest diese Spalten nicht mehr.
            print(f"Warnung: Spalte {col} konnte nicht entfernt werden: {exc}")

    # Alte gda_stufen-Klassifizierungen zurückbauen – sie werden nicht mehr
    # verwendet.
    try:
        conn.execute("DELETE FROM klassifizierungen WHERE bereich = 'gda_stufen'")
    except sqlite3.OperationalError:
        pass

    conn.commit()


# ---------------------------------------------------------------------------
# Klassifizierungen-Hilfsfunktion
# ---------------------------------------------------------------------------

def get_klassifizierungen(conn: sqlite3.Connection, bereich: str) -> list:
    """Gibt alle aktiven Einträge eines Klassifizierungsbereichs zurück."""
    return conn.execute("""
        SELECT id, wert, COALESCE(bezeichnung, wert) AS bezeichnung,
               beschreibung, sort_order
        FROM klassifizierungen
        WHERE bereich = ? AND aktiv = 1
        ORDER BY sort_order, wert
    """, (bereich,)).fetchall()


# ---------------------------------------------------------------------------
# Wesentlichkeitskriterien-Hilfsfunktionen
# ---------------------------------------------------------------------------

def get_wesentlichkeitskriterien(conn: sqlite3.Connection, nur_aktive: bool = True) -> list:
    """Gibt alle (aktiven) konfigurierbaren Wesentlichkeitskriterien zurück
    inklusive ihrer Checkbox-Details."""
    where = "WHERE aktiv = 1" if nur_aktive else ""
    kriterien = conn.execute(f"""
        SELECT id, bezeichnung, beschreibung, begruendung_pflicht, sort_order, aktiv
        FROM wesentlichkeitskriterien
        {where}
        ORDER BY sort_order, id
    """).fetchall()

    # Details dazu laden (nur aktive)
    result = []
    for k in kriterien:
        details = conn.execute("""
            SELECT id, bezeichnung, sort_order, aktiv
            FROM wesentlichkeitskriterium_details
            WHERE kriterium_id = ? AND aktiv = 1
            ORDER BY sort_order, id
        """, (k["id"],)).fetchall()
        d = dict(k)
        d["details"] = [dict(r) for r in details]
        result.append(d)
    return result


def get_kriterium_details(conn: sqlite3.Connection, kriterium_id: int,
                           nur_aktive: bool = False) -> list:
    """Gibt alle Details (Checkboxen) zu einem Kriterium zurück."""
    where = "AND aktiv = 1" if nur_aktive else ""
    return conn.execute(f"""
        SELECT id, kriterium_id, bezeichnung, sort_order, aktiv
        FROM wesentlichkeitskriterium_details
        WHERE kriterium_id = ? {where}
        ORDER BY sort_order, id
    """, (kriterium_id,)).fetchall()


def get_idv_wesentlichkeit(conn: sqlite3.Connection, idv_db_id: int) -> list:
    """
    Gibt alle aktiven Kriterien inkl. der IDV-spezifischen Antwort und der
    für diese IDV angekreuzten Details zurück. Inaktive Kriterien mit
    vorhandener Antwort werden ebenfalls geliefert (für die Detailansicht);
    aktive ohne Antwort erscheinen mit erfuellt=0.
    """
    kriterien = conn.execute("""
        SELECT k.id AS kriterium_id, k.bezeichnung, k.beschreibung,
               k.begruendung_pflicht, k.aktiv AS kriterium_aktiv,
               COALESCE(w.erfuellt, 0) AS erfuellt,
               w.begruendung
        FROM wesentlichkeitskriterien k
        LEFT JOIN idv_wesentlichkeit w
               ON w.idv_db_id = ? AND w.kriterium_id = k.id
        WHERE k.aktiv = 1
           OR w.idv_db_id IS NOT NULL
        ORDER BY k.aktiv DESC, k.sort_order, k.id
    """, (idv_db_id,)).fetchall()

    # Detail-Auswahlen je Kriterium einsammeln
    gewaehlte = {
        row[0]
        for row in conn.execute("""
            SELECT detail_id FROM idv_wesentlichkeit_detail WHERE idv_db_id = ?
        """, (idv_db_id,)).fetchall()
    }

    result = []
    for k in kriterien:
        details = conn.execute("""
            SELECT id, bezeichnung, sort_order, aktiv
            FROM wesentlichkeitskriterium_details
            WHERE kriterium_id = ?
              AND (aktiv = 1 OR id IN (
                    SELECT detail_id FROM idv_wesentlichkeit_detail
                    WHERE idv_db_id = ?))
            ORDER BY sort_order, id
        """, (k["kriterium_id"], idv_db_id)).fetchall()
        d = dict(k)
        d["details"] = [
            {**dict(r), "gewaehlt": r["id"] in gewaehlte} for r in details
        ]
        result.append(d)
    return result


def save_idv_wesentlichkeit(conn: sqlite3.Connection, idv_db_id: int,
                             antworten: list, commit: bool = True) -> None:
    """
    Speichert die Antworten einer IDV auf konfigurierbare Kriterien (UPSERT)
    sowie die angekreuzten Detail-Checkboxen.
    antworten: [{kriterium_id, erfuellt, begruendung, detail_ids: [int]}]
    Bereits vorhandene Antworten zu inaktiven Kriterien bleiben unberührt.
    commit=False erlaubt mehrere Operationen in einer Transaktion.
    """
    now = datetime.now(timezone.utc).isoformat()
    touched_kids = []
    for a in antworten:
        kid = a["kriterium_id"]
        touched_kids.append(kid)
        conn.execute("""
            INSERT INTO idv_wesentlichkeit
                        (idv_db_id, kriterium_id, erfuellt, begruendung, geaendert_am)
            VALUES      (?, ?, ?, ?, ?)
            ON CONFLICT(idv_db_id, kriterium_id) DO UPDATE SET
                erfuellt     = excluded.erfuellt,
                begruendung  = excluded.begruendung,
                geaendert_am = excluded.geaendert_am
        """, (idv_db_id, kid, int(a.get("erfuellt", 0)),
              a.get("begruendung") or None, now))

        # Details dieses Kriteriums aktualisieren: alle alten Detail-Auswahlen
        # für die Kriterium-Details dieses Kriteriums löschen, neue eintragen.
        conn.execute("""
            DELETE FROM idv_wesentlichkeit_detail
            WHERE idv_db_id = ?
              AND detail_id IN (
                SELECT id FROM wesentlichkeitskriterium_details
                WHERE kriterium_id = ?
              )
        """, (idv_db_id, kid))
        for did in a.get("detail_ids", []) or []:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO idv_wesentlichkeit_detail
                        (idv_db_id, detail_id) VALUES (?, ?)
                """, (idv_db_id, int(did)))
            except (ValueError, TypeError):
                continue

    if commit:
        conn.commit()


# ---------------------------------------------------------------------------
# IDV-ID Generator
# ---------------------------------------------------------------------------

def generate_idv_id(conn: sqlite3.Connection) -> str:
    """Generiert die nächste IDV-ID im Format IDV-YYYY-NNN."""
    year = datetime.now().year
    prefix = f"IDV-{year}-"
    existing = conn.execute(
        "SELECT idv_id FROM idv_register WHERE idv_id LIKE ? ORDER BY idv_id DESC LIMIT 1",
        (f"{prefix}%",)
    ).fetchone()

    if existing:
        last_num = int(existing["idv_id"].split("-")[-1])
        return f"{prefix}{last_num + 1:03d}"
    return f"{prefix}001"


# ---------------------------------------------------------------------------
# IDV-Register CRUD
# ---------------------------------------------------------------------------

def create_idv(conn: sqlite3.Connection, data: dict,
               erfasser_id: Optional[int] = None,
               commit: bool = True) -> int:
    """Legt einen neuen IDV-Register-Eintrag an."""
    now = datetime.now(timezone.utc).isoformat()
    idv_id = generate_idv_id(conn)

    # Nächste Prüfung berechnen
    intervall = data.get("pruefintervall_monate", 12)
    naechste_pruefung = _add_months(date.today(), intervall).isoformat()

    fields = {
        "idv_id":                    idv_id,
        "bezeichnung":               data["bezeichnung"],
        "kurzbeschreibung":          data.get("kurzbeschreibung"),
        "version":                   data.get("version", "1.0"),
        "file_id":                   data.get("file_id"),
        "idv_typ":                   data.get("idv_typ", "unklassifiziert"),
        "gp_id":                     data.get("gp_id"),
        "gp_freitext":               data.get("gp_freitext"),
        "risikoklasse_id":           data.get("risikoklasse_id"),
        "risiko_verfuegbarkeit":     data.get("risiko_verfuegbarkeit"),
        "risiko_integritaet":        data.get("risiko_integritaet"),
        "risiko_vertraulichkeit":    data.get("risiko_vertraulichkeit"),
        "risiko_nachvollziehbarkeit":data.get("risiko_nachvollziehbarkeit"),
        "org_unit_id":               data.get("org_unit_id"),
        "fachverantwortlicher_id":   data.get("fachverantwortlicher_id"),
        "idv_entwickler_id":         data.get("idv_entwickler_id"),
        "idv_koordinator_id":        data.get("idv_koordinator_id"),
        "stellvertreter_id":         data.get("stellvertreter_id"),
        "plattform_id":              data.get("plattform_id"),
        "programmiersprache":        data.get("programmiersprache"),
        "datenbankanbindung":        int(data.get("datenbankanbindung", 0)),
        "datenbankanbindung_beschr": data.get("datenbankanbindung_beschr"),
        "netzwerkzugriff":           int(data.get("netzwerkzugriff", 0)),
        "enthaelt_personendaten":    int(data.get("enthaelt_personendaten", 0)),
        "datenschutz_kategorie":     data.get("datenschutz_kategorie", "keine"),
        "nutzungsfrequenz":          data.get("nutzungsfrequenz"),
        "nutzeranzahl":              data.get("nutzeranzahl"),
        "produktiv_seit":            data.get("produktiv_seit"),
        "dokumentation_vorhanden":   int(data.get("dokumentation_vorhanden", 0)),
        "dokumentation_pfad":        data.get("dokumentation_pfad"),
        "testkonzept_vorhanden":     int(data.get("testkonzept_vorhanden", 0)),
        "versionskontrolle":         int(data.get("versionskontrolle", 0)),
        "zugriffsschutz":            int(data.get("zugriffsschutz", 0)),
        "zugriffsschutz_beschr":     data.get("zugriffsschutz_beschr"),
        "vier_augen_prinzip":        int(data.get("vier_augen_prinzip", 0)),
        "abloesung_geplant":         int(data.get("abloesung_geplant", 0)),
        "abloesung_zieldatum":       data.get("abloesung_zieldatum"),
        "abloesung_durch":           data.get("abloesung_durch"),
        # Neue Felder
        "gobd_relevant":                 int(data.get("gobd_relevant", 0)),
        "erstellt_fuer":                 data.get("erstellt_fuer"),
        "schnittstellen_beschr":         data.get("schnittstellen_beschr"),
        "teststatus":                    data.get("teststatus", "Wertung ausstehend"),
        "vorgaenger_idv_id":             data.get("vorgaenger_idv_id"),
        "letzte_aenderungsart":          data.get("letzte_aenderungsart"),
        "letzte_aenderungsbegruendung":  data.get("letzte_aenderungsbegruendung"),
        "status":                    "Entwurf",
        "pruefintervall_monate":     intervall,
        "naechste_pruefung":         naechste_pruefung,
        "erfasst_von_id":            erfasser_id,
        "erstellt_am":               now,
        "aktualisiert_am":           now,
        "tags":                      json.dumps(data.get("tags", []), ensure_ascii=False),
        "interne_notizen":           data.get("interne_notizen"),
    }

    placeholders = ", ".join(f":{k}" for k in fields)
    cols         = ", ".join(fields.keys())
    cur = conn.execute(
        f"INSERT INTO idv_register ({cols}) VALUES ({placeholders})", fields
    )
    new_id = cur.lastrowid

    # Historien-Eintrag
    conn.execute("""
        INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id)
        VALUES (?, 'erstellt', ?, ?)
    """, (new_id, f"IDV {idv_id} erstellt", erfasser_id))

    # Scanner-Datei als registriert markieren
    if data.get("file_id"):
        conn.execute(
            "UPDATE idv_files SET bearbeitungsstatus = 'Registriert' WHERE id = ?",
            (data["file_id"],)
        )

    if commit:
        conn.commit()
    return new_id


def update_idv(conn: sqlite3.Connection, idv_db_id: int,
               data: dict, geaendert_von_id: Optional[int] = None) -> bool:
    """Aktualisiert einen IDV-Eintrag und schreibt die Änderungen in die History."""
    now = datetime.now(timezone.utc).isoformat()

    old = conn.execute(
        "SELECT * FROM idv_register WHERE id = ?", (idv_db_id,)
    ).fetchone()
    if not old:
        return False

    # Änderungsprotokoll aufbauen
    tracked_fields = [
        "bezeichnung", "idv_typ", "status",
        "fachverantwortlicher_id", "gp_id", "risikoklasse_id",
        "naechste_pruefung", "pruefintervall_monate", "gobd_relevant",
        "teststatus",
    ]
    changes = {}
    for f in tracked_fields:
        if f in data and str(data[f]) != str(old[f]):
            changes[f] = {"alt": old[f], "neu": data[f]}

    # Update ausführen
    update_fields = {k: v for k, v in data.items() if k in [
        "bezeichnung", "kurzbeschreibung", "version", "idv_typ",
        "gp_id", "gp_freitext",
        "risikoklasse_id", "risiko_verfuegbarkeit", "risiko_integritaet",
        "risiko_vertraulichkeit", "risiko_nachvollziehbarkeit",
        "org_unit_id", "fachverantwortlicher_id", "idv_entwickler_id",
        "idv_koordinator_id", "stellvertreter_id",
        "plattform_id", "programmiersprache", "datenbankanbindung",
        "datenbankanbindung_beschr", "netzwerkzugriff",
        "enthaelt_personendaten", "datenschutz_kategorie",
        "nutzungsfrequenz", "nutzeranzahl", "produktiv_seit",
        "dokumentation_vorhanden", "dokumentation_pfad",
        "testkonzept_vorhanden", "versionskontrolle",
        "zugriffsschutz", "zugriffsschutz_beschr", "vier_augen_prinzip",
        "abloesung_geplant", "abloesung_zieldatum", "abloesung_durch",
        "pruefintervall_monate", "naechste_pruefung", "interne_notizen", "tags",
        "gobd_relevant", "erstellt_fuer", "schnittstellen_beschr",
        "teststatus",
        "letzte_aenderungsart", "letzte_aenderungsbegruendung",
    ]}
    update_fields["aktualisiert_am"] = now

    set_clause = ", ".join(f"{k} = :{k}" for k in update_fields)
    conn.execute(
        f"UPDATE idv_register SET {set_clause} WHERE id = :__id",
        {**update_fields, "__id": idv_db_id}
    )

    if changes:
        conn.execute("""
            INSERT INTO idv_history (idv_id, aktion, geaenderte_felder, durchgefuehrt_von_id)
            VALUES (?, 'geaendert', ?, ?)
        """, (idv_db_id, json.dumps(changes, ensure_ascii=False), geaendert_von_id))

    conn.commit()
    return True


def change_status(conn: sqlite3.Connection, idv_db_id: int,
                  new_status: str, kommentar: str = "",
                  geaendert_von_id: Optional[int] = None):
    """Ändert den Workflow-Status eines IDV-Eintrags."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        UPDATE idv_register
        SET status = ?, status_geaendert_am = ?, status_geaendert_von_id = ?, aktualisiert_am = ?
        WHERE id = ?
    """, (new_status, now, geaendert_von_id, now, idv_db_id))
    if new_status == "Freigegeben":
        row = conn.execute(
            "SELECT f.file_hash FROM idv_register r "
            "LEFT JOIN idv_files f ON r.file_id = f.id WHERE r.id = ?",
            (idv_db_id,)
        ).fetchone()
        if row and row["file_hash"]:
            kommentar = (kommentar or "") + f" [Datei-Hash: {row['file_hash'][:16]}...]"

    conn.execute("""
        INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id)
        VALUES (?, 'status_geaendert', ?, ?)
    """, (idv_db_id, f"Status → {new_status}. {kommentar}", geaendert_von_id))
    conn.commit()


# ---------------------------------------------------------------------------
# Abfragen / Reports
# ---------------------------------------------------------------------------

def get_dashboard_stats(conn: sqlite3.Connection, person_id: Optional[int] = None) -> dict:
    """Kennzahlen für das Dashboard.

    person_id: wenn gesetzt, wird 'unvollstaendig' auf die IDVs des Nutzers eingeschränkt
               (für Rollen ohne vollständigen Lesezugriff).
    """
    def scalar(sql, *args):
        return conn.execute(sql, args).fetchone()[0] or 0

    # Eingeschränkte Nutzer sehen nur ihre eigenen unvollständigen IDVs
    if person_id is not None:
        unvollstaendig = conn.execute("""
            SELECT COUNT(*) FROM v_unvollstaendige_idvs v
            JOIN idv_register r ON r.idv_id = v.idv_id
            WHERE r.fachverantwortlicher_id = ?
               OR r.idv_entwickler_id       = ?
               OR r.idv_koordinator_id      = ?
               OR r.stellvertreter_id       = ?
        """, (person_id, person_id, person_id, person_id)).fetchone()[0] or 0
    else:
        unvollstaendig = scalar("SELECT COUNT(*) FROM v_unvollstaendige_idvs")

    return {
        "gesamt_aktiv":         scalar("SELECT COUNT(*) FROM idv_register WHERE status NOT IN ('Archiviert')"),
        "genehmigt":            scalar("SELECT COUNT(*) FROM idv_register WHERE status = 'Freigegeben'"),
        "entwurf":              scalar("SELECT COUNT(*) FROM idv_register WHERE status = 'Entwurf'"),
        "in_pruefung":          scalar("SELECT COUNT(*) FROM idv_register WHERE status = 'In Prüfung'"),
        "wesentlich":           scalar("""
            SELECT COUNT(*) FROM idv_register r WHERE status NOT IN ('Archiviert')
            AND EXISTS(SELECT 1 FROM idv_wesentlichkeit iw
                       WHERE iw.idv_db_id=r.id AND iw.erfuellt=1)
        """),
        "nicht_wesentlich":     scalar("""
            SELECT COUNT(*) FROM idv_register r WHERE status NOT IN ('Archiviert')
            AND NOT EXISTS(SELECT 1 FROM idv_wesentlichkeit iw
                           WHERE iw.idv_db_id=r.id AND iw.erfuellt=1)
        """),
        "pruefung_ueberfaellig":scalar("SELECT COUNT(*) FROM idv_register WHERE naechste_pruefung < date('now') AND status NOT IN ('Archiviert','Abgekündigt')"),
        "pruefung_30_tage":     scalar("SELECT COUNT(*) FROM idv_register WHERE naechste_pruefung BETWEEN date('now') AND date('now','+30 days') AND status NOT IN ('Archiviert','Abgekündigt')"),
        "massnahmen_offen":     scalar("SELECT COUNT(*) FROM massnahmen WHERE status IN ('Offen','In Bearbeitung')"),
        "massnahmen_ueberfaellig": scalar("SELECT COUNT(*) FROM massnahmen WHERE faellig_am < date('now') AND status IN ('Offen','In Bearbeitung')"),
        "unvollstaendig":       unvollstaendig,
    }


def search_idv(conn: sqlite3.Connection, suchbegriff: str = "",
               status: str = "",
               wesentlich: Optional[bool] = None,
               org_unit_id: Optional[int] = None) -> list:
    """Flexibler IDV-Suchaufruf."""
    where = ["r.status NOT IN ('Archiviert')"]
    params = []

    if suchbegriff:
        where.append("(r.bezeichnung LIKE ? OR r.idv_id LIKE ? OR r.kurzbeschreibung LIKE ?)")
        params += [f"%{suchbegriff}%"] * 3
    if status:
        where.append("r.status = ?")
        params.append(status)
    if wesentlich is True:
        where.append(
            "EXISTS(SELECT 1 FROM idv_wesentlichkeit iw "
            "WHERE iw.idv_db_id=r.id AND iw.erfuellt=1)"
        )
    elif wesentlich is False:
        where.append(
            "NOT EXISTS(SELECT 1 FROM idv_wesentlichkeit iw "
            "WHERE iw.idv_db_id=r.id AND iw.erfuellt=1)"
        )
    if org_unit_id:
        where.append("r.org_unit_id = ?")
        params.append(org_unit_id)

    sql = f"""
        SELECT v.* FROM v_idv_uebersicht v
        JOIN idv_register r ON r.id = v.idv_db_id
        WHERE {' AND '.join(where)}
        ORDER BY v.ist_wesentlich DESC, v.bezeichnung
    """
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _add_months(d: date, months: int) -> date:
    """Addiert Monate zu einem Datum."""
    month = d.month - 1 + months
    year  = d.year + month // 12
    month = month % 12 + 1
    day   = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


# ---------------------------------------------------------------------------
# Demodaten (für Tests / Ersteinrichtung)
# ---------------------------------------------------------------------------

def insert_demo_data(conn: sqlite3.Connection):
    """Legt Beispiel-Stammdaten und einen Demo-IDV an."""
    now = datetime.now(timezone.utc).isoformat()

    # Personen
    conn.executemany(
        "INSERT OR IGNORE INTO persons (kuerzel, nachname, vorname, email, rolle, org_unit_id) "
        "VALUES (?,?,?,?,?, (SELECT id FROM org_units WHERE bezeichnung=?))",
        [
            ("IDV-KO", "Mustermann", "Max", "m.mustermann@volksbank.de", "IDV-Koordinator",     "IT & IT-Sicherheit"),
            ("FV-BWK", "Beispiel",   "Anna","a.beispiel@volksbank.de",   "Fachverantwortlicher","Betriebswirtschaft/Controlling"),
            ("FV-KRE", "Schmidt",    "Klaus","k.schmidt@volksbank.de",   "Fachverantwortlicher","Kreditabteilung"),
        ]
    )

    # Geschäftsprozesse
    conn.executemany(
        "INSERT OR IGNORE INTO geschaeftsprozesse "
        "(gp_nummer, bezeichnung, bereich, ist_kritisch, ist_wesentlich, org_unit_id) "
        "VALUES (?,?,?,?,?, (SELECT id FROM org_units WHERE bezeichnung=?))",
        [
            ("GP-BWK-001","Monatliche GuV-Berechnung",      "Steuerung",  1,1,"Betriebswirtschaft/Controlling"),
            ("GP-KRE-001","Kreditentscheidung Firmenkunden","Marktfolge", 1,1,"Kreditabteilung"),
            ("GP-MEL-001","Meldewesen EBA/Bundesbank",      "Steuerung",  1,1,"Meldewesen"),
            ("GP-RIS-001","Zinsrisiko-Steuerung",           "Steuerung",  1,1,"Risikocontrolling"),
        ]
    )

    # Plattformen
    conn.executemany(
        "INSERT OR IGNORE INTO plattformen (bezeichnung, typ, hersteller) VALUES (?,?,?)",
        [
            ("Microsoft Excel",    "Desktop", "Microsoft"),
            ("Microsoft Access",   "Desktop", "Microsoft"),
            ("HCL Notes",   "Desktop", "HCL"),
            ("Business Intelligence",        "Desktop", "BI"),
            ("Shell-Skripte",        "Konsole", "Bank"),
            ("UiPath Studio",        "IDE", "UiPath"),
            ("Power BI Desktop",        "Desktop", "Microsoft"),
            ("Python 3.11",             "Server",  "PSF"),
        ]
    )

    stats = get_dashboard_stats(conn)
    print("\nDashboard-Statistik nach Demo-Import:")
    for k, v in stats.items():
        print(f"  {k:35s}: {v}")


# ---------------------------------------------------------------------------
# Testdokumentation – Fachliche Testfälle
# ---------------------------------------------------------------------------

def get_fachliche_testfaelle(conn: sqlite3.Connection, idv_db_id: int):
    """Gibt alle fachlichen Testfälle einer IDV zurück, sortiert nach Testfall-Nr."""
    return conn.execute(
        "SELECT * FROM fachliche_testfaelle WHERE idv_id = ? ORDER BY testfall_nr",
        (idv_db_id,),
    ).fetchall()


def get_fachlicher_testfall(conn: sqlite3.Connection, testfall_id: int):
    """Gibt einen einzelnen fachlichen Testfall zurück oder None."""
    return conn.execute(
        "SELECT * FROM fachliche_testfaelle WHERE id = ?", (testfall_id,)
    ).fetchone()


def create_fachlicher_testfall(conn: sqlite3.Connection, idv_db_id: int, data: dict) -> int:
    """Legt einen neuen fachlichen Testfall an. Gibt die neue DB-ID zurück."""
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT COALESCE(MAX(testfall_nr), 0) FROM fachliche_testfaelle WHERE idv_id = ?",
        (idv_db_id,),
    ).fetchone()
    next_nr = (row[0] or 0) + 1
    cur = conn.execute(
        """
        INSERT INTO fachliche_testfaelle
          (idv_id, testfall_nr, beschreibung, parametrisierung, testdaten,
           erwartetes_ergebnis, erzieltes_ergebnis, bewertung,
           massnahmen, tester, testdatum,
           nachweis_datei_pfad, nachweis_datei_name,
           erstellt_am, aktualisiert_am)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            idv_db_id, next_nr,
            data.get("beschreibung", ""),
            data.get("parametrisierung") or None,
            data.get("testdaten") or None,
            data.get("erwartetes_ergebnis") or None,
            data.get("erzieltes_ergebnis") or None,
            data.get("bewertung", "Offen"),
            data.get("massnahmen") or None,
            data.get("tester") or None,
            data.get("testdatum") or None,
            data.get("nachweis_datei_pfad") or None,
            data.get("nachweis_datei_name") or None,
            now, now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_fachlicher_testfall(conn: sqlite3.Connection, testfall_id: int, data: dict) -> None:
    """Aktualisiert einen vorhandenen fachlichen Testfall."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE fachliche_testfaelle SET
            beschreibung        = ?,
            parametrisierung    = ?,
            testdaten           = ?,
            erwartetes_ergebnis = ?,
            erzieltes_ergebnis  = ?,
            bewertung           = ?,
            massnahmen          = ?,
            tester              = ?,
            testdatum           = ?,
            nachweis_datei_pfad = ?,
            nachweis_datei_name = ?,
            aktualisiert_am     = ?
        WHERE id = ?
        """,
        (
            data.get("beschreibung", ""),
            data.get("parametrisierung") or None,
            data.get("testdaten") or None,
            data.get("erwartetes_ergebnis") or None,
            data.get("erzieltes_ergebnis") or None,
            data.get("bewertung", "Offen"),
            data.get("massnahmen") or None,
            data.get("tester") or None,
            data.get("testdatum") or None,
            data.get("nachweis_datei_pfad") or None,
            data.get("nachweis_datei_name") or None,
            now,
            testfall_id,
        ),
    )
    conn.commit()


def delete_fachlicher_testfall(conn: sqlite3.Connection, testfall_id: int) -> None:
    """Löscht einen fachlichen Testfall."""
    conn.execute("DELETE FROM fachliche_testfaelle WHERE id = ?", (testfall_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Testdokumentation – Technischer Test
# ---------------------------------------------------------------------------

def get_technischer_test(conn: sqlite3.Connection, idv_db_id: int):
    """Gibt den technischen Test einer IDV zurück oder None."""
    return conn.execute(
        "SELECT * FROM technischer_test WHERE idv_id = ?", (idv_db_id,)
    ).fetchone()


def save_technischer_test(conn: sqlite3.Connection, idv_db_id: int, data: dict) -> None:
    """Legt den technischen Test an oder aktualisiert ihn (UPSERT)."""
    now = datetime.now(timezone.utc).isoformat()
    existing = get_technischer_test(conn, idv_db_id)
    if existing:
        conn.execute(
            """
            UPDATE technischer_test SET
                ergebnis            = ?,
                kurzbeschreibung    = ?,
                pruefer             = ?,
                pruefungsdatum      = ?,
                nachweis_datei_pfad = ?,
                nachweis_datei_name = ?,
                aktualisiert_am     = ?
            WHERE idv_id = ?
            """,
            (
                data.get("ergebnis", "Offen"),
                data.get("kurzbeschreibung") or None,
                data.get("pruefer") or None,
                data.get("pruefungsdatum") or None,
                data.get("nachweis_datei_pfad") or None,
                data.get("nachweis_datei_name") or None,
                now,
                idv_db_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO technischer_test
              (idv_id, ergebnis, kurzbeschreibung, pruefer, pruefungsdatum,
               nachweis_datei_pfad, nachweis_datei_name,
               erstellt_am, aktualisiert_am)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                idv_db_id,
                data.get("ergebnis", "Offen"),
                data.get("kurzbeschreibung") or None,
                data.get("pruefer") or None,
                data.get("pruefungsdatum") or None,
                data.get("nachweis_datei_pfad") or None,
                data.get("nachweis_datei_name") or None,
                now, now,
            ),
        )
    conn.commit()


def delete_technischer_test(conn: sqlite3.Connection, idv_db_id: int) -> None:
    """Löscht den technischen Test einer IDV."""
    conn.execute("DELETE FROM technischer_test WHERE idv_id = ?", (idv_db_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="IDV-Register Datenbank initialisieren")
    parser.add_argument("--db",   default="idv_register.db", help="Pfad zur SQLite-DB")
    parser.add_argument("--demo", action="store_true",        help="Demodaten einfügen")
    args = parser.parse_args()

    print(f"Initialisiere Datenbank: {args.db}")
    conn = init_register_db(args.db)
    print("Schema erfolgreich angelegt.")

    if args.demo:
        print("\nFüge Demodaten ein …")
        insert_demo_data(conn)

    conn.close()
    print("\nFertig.")
