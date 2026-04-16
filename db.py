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
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_register_db(db_path: str) -> sqlite3.Connection:
    """Initialisiert die Datenbank anhand von schema.sql."""
    conn = get_connection(db_path)
    schema_path = _resource_path("schema.sql")
    if not schema_path.exists():
        raise FileNotFoundError(f"schema.sql nicht gefunden: {schema_path}")
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
    """Gibt alle (aktiven) konfigurierbaren Wesentlichkeitskriterien zurück."""
    where = "WHERE aktiv = 1" if nur_aktive else ""
    return conn.execute(f"""
        SELECT id, bezeichnung, beschreibung, begruendung_pflicht, sort_order, aktiv
        FROM wesentlichkeitskriterien
        {where}
        ORDER BY sort_order, id
    """).fetchall()


def get_idv_wesentlichkeit(conn: sqlite3.Connection, idv_db_id: int) -> list:
    """
    Gibt alle aktiven Kriterien inkl. der IDV-spezifischen Antwort zurück.
    Inaktive Kriterien mit vorhandener Antwort werden ebenfalls geliefert
    (für die Detailansicht), aktive ohne Antwort erscheinen mit erfuellt=0.
    """
    return conn.execute("""
        SELECT k.id AS kriterium_id, k.bezeichnung, k.beschreibung,
               k.begruendung_pflicht, k.aktiv AS kriterium_aktiv,
               COALESCE(w.erfuellt, 0) AS erfuellt,
               w.begruendung
        FROM wesentlichkeitskriterien k
        LEFT JOIN idv_wesentlichkeit w
               ON w.idv_db_id = ? AND w.kriterium_id = k.id
        WHERE k.aktiv = 1
           OR w.idv_db_id IS NOT NULL          -- inaktiv aber bereits beantwortet
        ORDER BY k.aktiv DESC, k.sort_order, k.id
    """, (idv_db_id,)).fetchall()


def save_idv_wesentlichkeit(conn: sqlite3.Connection, idv_db_id: int,
                             antworten: list, commit: bool = True) -> None:
    """
    Speichert die Antworten einer IDV auf konfigurierbare Kriterien (UPSERT).
    antworten: [{kriterium_id, erfuellt, begruendung}]
    Bereits vorhandene Antworten zu inaktiven Kriterien bleiben unberührt.
    commit=False erlaubt dem Aufrufer, mehrere Operationen in einer Transaktion zu bündeln.
    """
    now = datetime.now(timezone.utc).isoformat()
    for a in antworten:
        conn.execute("""
            INSERT INTO idv_wesentlichkeit
                        (idv_db_id, kriterium_id, erfuellt, begruendung, geaendert_am)
            VALUES      (?, ?, ?, ?, ?)
            ON CONFLICT(idv_db_id, kriterium_id) DO UPDATE SET
                erfuellt     = excluded.erfuellt,
                begruendung  = excluded.begruendung,
                geaendert_am = excluded.geaendert_am
        """, (idv_db_id, a["kriterium_id"], int(a.get("erfuellt", 0)),
              a.get("begruendung") or None, now))
    if commit:
        conn.commit()


def _compute_dora(conn: sqlite3.Connection, gp_id, gda_wert) -> int:
    """Leitet dora_kritisch_wichtig aus GP-Klassifizierung und GDA ab."""
    if not gp_id:
        return 0
    gp = conn.execute(
        "SELECT ist_kritisch FROM geschaeftsprozesse WHERE id = ?", (gp_id,)
    ).fetchone()
    return 1 if (gp and gp["ist_kritisch"] and int(gda_wert or 1) == 4) else 0


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

    # DORA wird aus GP-Klassifizierung + GDA abgeleitet (nicht manuell gesetzt)
    dora = _compute_dora(conn, data.get("gp_id"), data.get("gda_wert", 1))

    fields = {
        "idv_id":                    idv_id,
        "bezeichnung":               data["bezeichnung"],
        "kurzbeschreibung":          data.get("kurzbeschreibung"),
        "version":                   data.get("version", "1.0"),
        "file_id":                   data.get("file_id"),
        "idv_typ":                   data.get("idv_typ", "unklassifiziert"),
        "steuerungsrelevant":        int(data.get("steuerungsrelevant", 0)),
        "steuerungsrelevanz_begr":   data.get("steuerungsrelevanz_begr"),
        "rechnungslegungsrelevant":  int(data.get("rechnungslegungsrelevant", 0)),
        "rechnungslegungsrelevanz_begr": data.get("rechnungslegungsrelevanz_begr"),
        "gda_wert":                  data.get("gda_wert", 1),
        "gp_id":                     data.get("gp_id"),
        "gp_freitext":               data.get("gp_freitext"),
        "dora_kritisch_wichtig":     dora,
        "dora_begruendung":          data.get("dora_begruendung"),
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

    # DORA aus GP + GDA ableiten
    gp_id   = data.get("gp_id", old["gp_id"])
    gda_wert = data.get("gda_wert", old["gda_wert"])
    data["dora_kritisch_wichtig"] = _compute_dora(conn, gp_id, gda_wert)

    # Änderungsprotokoll aufbauen
    tracked_fields = [
        "bezeichnung", "idv_typ", "gda_wert", "steuerungsrelevant",
        "rechnungslegungsrelevant", "dora_kritisch_wichtig", "status",
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
        "steuerungsrelevant", "steuerungsrelevanz_begr",
        "rechnungslegungsrelevant", "rechnungslegungsrelevanz_begr",
        "gda_wert", "gp_id", "gp_freitext",
        "dora_kritisch_wichtig", "dora_begruendung",
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
            AND (r.steuerungsrelevant=1 OR r.rechnungslegungsrelevant=1 OR r.dora_kritisch_wichtig=1
                 OR EXISTS(SELECT 1 FROM idv_wesentlichkeit iw WHERE iw.idv_db_id=r.id AND iw.erfuellt=1))
        """),
        "nicht_wesentlich":     scalar("""
            SELECT COUNT(*) FROM idv_register r WHERE status NOT IN ('Archiviert')
            AND NOT (r.steuerungsrelevant=1 OR r.rechnungslegungsrelevant=1 OR r.dora_kritisch_wichtig=1
                     OR EXISTS(SELECT 1 FROM idv_wesentlichkeit iw WHERE iw.idv_db_id=r.id AND iw.erfuellt=1))
        """),
        "kritisch_gda4":        scalar("SELECT COUNT(*) FROM idv_register WHERE gda_wert = 4 AND status NOT IN ('Archiviert')"),
        "steuerungsrelevant":   scalar("SELECT COUNT(*) FROM idv_register WHERE steuerungsrelevant = 1 AND status NOT IN ('Archiviert')"),
        "dora_kritisch":        scalar("SELECT COUNT(*) FROM idv_register WHERE dora_kritisch_wichtig = 1 AND status NOT IN ('Archiviert')"),
        "pruefung_ueberfaellig":scalar("SELECT COUNT(*) FROM idv_register WHERE naechste_pruefung < date('now') AND status NOT IN ('Archiviert','Abgekündigt')"),
        "pruefung_30_tage":     scalar("SELECT COUNT(*) FROM idv_register WHERE naechste_pruefung BETWEEN date('now') AND date('now','+30 days') AND status NOT IN ('Archiviert','Abgekündigt')"),
        "massnahmen_offen":     scalar("SELECT COUNT(*) FROM massnahmen WHERE status IN ('Offen','In Bearbeitung')"),
        "massnahmen_ueberfaellig": scalar("SELECT COUNT(*) FROM massnahmen WHERE faellig_am < date('now') AND status IN ('Offen','In Bearbeitung')"),
        "unvollstaendig":       unvollstaendig,
    }


def search_idv(conn: sqlite3.Connection, suchbegriff: str = "",
               status: str = "", gda_min: int = 0,
               steuerungsrelevant: Optional[bool] = None,
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
    if gda_min > 0:
        where.append("r.gda_wert >= ?")
        params.append(gda_min)
    if steuerungsrelevant is not None:
        where.append("r.steuerungsrelevant = ?")
        params.append(1 if steuerungsrelevant else 0)
    if org_unit_id:
        where.append("r.org_unit_id = ?")
        params.append(org_unit_id)

    sql = f"""
        SELECT * FROM v_idv_uebersicht r
        WHERE {' AND '.join(where)}
        ORDER BY r.gda_wert DESC, r.bezeichnung
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

    # Org-Einheiten
    conn.executemany(
        "INSERT OR IGNORE INTO org_units (bezeichnung, ebene) VALUES (?,?)",
        [
            ("Vorstand",                      "Vorstand"),
            ("Filialvertrieb",                "Bereich"),
            ("Kreditabteilung",               "Abteilung"),
            ("Betriebswirtschaft/Controlling","Abteilung"),
            ("IT & IT-Sicherheit",            "Abteilung"),
            ("Risikocontrolling",             "Abteilung"),
            ("Interne Revision",              "Abteilung"),
            ("Rechnungswesen/Buchhaltung",    "Abteilung"),
            ("Meldewesen",                    "Abteilung"),
        ]
    )

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

    # Demo-IDV
    demo = {
        "bezeichnung":             "GuV-Monatsabschluss Controlling",
        "kurzbeschreibung":        "Excel-Modell zur monatlichen Berechnung und Aufbereitung der "
                                   "Gewinn- und Verlustrechnung auf Filialebene. Datenquelle: OSPlus-Export.",
        "idv_typ":                 "Excel-Modell",
        "steuerungsrelevant":      1,
        "steuerungsrelevanz_begr": "Direkte Steuerungsrelevanz: Ergebnisse fließen in die monatliche "
                                   "Vorstandsberichterstattung ein.",
        "rechnungslegungsrelevant":1,
        "rechnungslegungsrelevanz_begr": "Grundlage für HGB-Monatsabschluss.",
        "gda_wert":                4,
        "gp_freitext":             "GP-BWK-001",
        "dora_kritisch_wichtig":   1,
        "dora_begruendung":        "Vollständige Abhängigkeit im kritischen Geschäftsprozess GuV-Berechnung.",
        "pruefintervall_monate":   6,
        "nutzungsfrequenz":        "monatlich",
        "nutzeranzahl":            3,
        "dokumentation_vorhanden": 1,
        "zugriffsschutz":          1,
        "zugriffsschutz_beschr":   "Schreibschutz für alle außer Controlling-Laufwerk.",
        "vier_augen_prinzip":      1,
    }

    idv_id = create_idv(conn, demo)
    print(f"Demo-IDV erstellt mit DB-ID: {idv_id}")

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
