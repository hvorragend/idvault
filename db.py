"""
IDV-Register Datenbankschicht
=============================
Initialisierung, Migration und Basisfunktionen für das IDV-Register.
Wird von Scanner und Web-Frontend gemeinsam genutzt.
"""

import sqlite3
import json
import calendar
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional


DB_VERSION = 2   # Schema-Versionsnummer für spätere Migrationen

# ---------------------------------------------------------------------------
# Verbindung & Initialisierung
# ---------------------------------------------------------------------------

def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_register_db(db_path: str) -> sqlite3.Connection:
    """Initialisiert die Datenbank und führt alle Migrationen aus."""
    conn = get_connection(db_path)

    # Schema-Version verwalten
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _schema_version (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT NOT NULL
        )
    """)
    conn.commit()

    current = conn.execute(
        "SELECT MAX(version) as v FROM _schema_version"
    ).fetchone()["v"] or 0

    if current < 1:
        _migrate_v1(conn)
        conn.execute(
            "INSERT INTO _schema_version VALUES (1, ?)",
            (datetime.now(timezone.utc).isoformat(),)
        )
        conn.commit()

    if current < 2:
        _migrate_v2(conn)
        conn.execute(
            "INSERT INTO _schema_version VALUES (2, ?)",
            (datetime.now(timezone.utc).isoformat(),)
        )
        conn.commit()

    return conn


def _migrate_v1(conn: sqlite3.Connection):
    """Migration v1: Vollständiges IDV-Register-Schema."""
    schema_path = Path(__file__).parent / "schema.sql"
    if schema_path.exists():
        sql = schema_path.read_text(encoding="utf-8")
        conn.executescript(sql)
    else:
        raise FileNotFoundError(f"schema.sql nicht gefunden: {schema_path}")


def _migrate_v2(conn: sqlite3.Connection):
    """Migration v2: Erweiterte Personen-Felder (user_id, ad_name, password_hash) + App-Einstellungen."""
    # Neue Spalten in persons (idempotent via ALTER TABLE IF NOT EXISTS-Emulation)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(persons)")}
    if "user_id" not in cols:
        conn.execute("ALTER TABLE persons ADD COLUMN user_id TEXT")
    if "ad_name" not in cols:
        conn.execute("ALTER TABLE persons ADD COLUMN ad_name TEXT")
    if "password_hash" not in cols:
        conn.execute("ALTER TABLE persons ADD COLUMN password_hash TEXT")

    # Eindeutiger Index auf user_id (falls noch nicht vorhanden)
    conn.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_persons_user_id
            ON persons(user_id) WHERE user_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        INSERT OR IGNORE INTO app_settings (key, value) VALUES
            ('smtp_host',     ''),
            ('smtp_port',     '587'),
            ('smtp_user',     ''),
            ('smtp_password', ''),
            ('smtp_from',     ''),
            ('smtp_tls',      '1'),
            ('notify_new_file', '1');
    """)


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
               erfasser_id: Optional[int] = None) -> int:
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
        "steuerungsrelevant":        int(data.get("steuerungsrelevant", 0)),
        "steuerungsrelevanz_begr":   data.get("steuerungsrelevanz_begr"),
        "relevant_guv":              int(data.get("relevant_guv", 0)),
        "relevant_meldewesen":       int(data.get("relevant_meldewesen", 0)),
        "relevant_risikomanagement": int(data.get("relevant_risikomanagement", 0)),
        "rechnungslegungsrelevant":  int(data.get("rechnungslegungsrelevant", 0)),
        "rechnungslegungsrelevanz_begr": data.get("rechnungslegungsrelevanz_begr"),
        "gda_wert":                  data.get("gda_wert", 1),
        "gda_begruendung":           data.get("gda_begruendung"),
        "gp_id":                     data.get("gp_id"),
        "gp_freitext":               data.get("gp_freitext"),
        "dora_kritisch_wichtig":     int(data.get("dora_kritisch_wichtig", 0)),
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
        "bezeichnung", "idv_typ", "gda_wert", "steuerungsrelevant",
        "rechnungslegungsrelevant", "dora_kritisch_wichtig", "status",
        "fachverantwortlicher_id", "gp_id", "risikoklasse_id",
        "naechste_pruefung", "pruefintervall_monate"
    ]
    changes = {}
    for f in tracked_fields:
        if f in data and str(data[f]) != str(old[f]):
            changes[f] = {"alt": old[f], "neu": data[f]}

    # Update ausführen
    update_fields = {k: v for k, v in data.items() if k in [
        "bezeichnung", "kurzbeschreibung", "version", "idv_typ",
        "steuerungsrelevant", "steuerungsrelevanz_begr",
        "relevant_guv", "relevant_meldewesen", "relevant_risikomanagement",
        "rechnungslegungsrelevant", "rechnungslegungsrelevanz_begr",
        "gda_wert", "gda_begruendung", "gp_id", "gp_freitext",
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
        "pruefintervall_monate", "naechste_pruefung", "interne_notizen", "tags"
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
    conn.execute("""
        INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id)
        VALUES (?, 'status_geaendert', ?, ?)
    """, (idv_db_id, f"Status → {new_status}. {kommentar}", geaendert_von_id))
    conn.commit()


# ---------------------------------------------------------------------------
# Abfragen / Reports
# ---------------------------------------------------------------------------

def get_dashboard_stats(conn: sqlite3.Connection) -> dict:
    """Kennzahlen für das Dashboard."""
    def scalar(sql, *args):
        return conn.execute(sql, args).fetchone()[0] or 0

    return {
        "gesamt_aktiv":         scalar("SELECT COUNT(*) FROM idv_register WHERE status NOT IN ('Archiviert')"),
        "genehmigt":            scalar("SELECT COUNT(*) FROM idv_register WHERE status = 'Genehmigt'"),
        "entwurf":              scalar("SELECT COUNT(*) FROM idv_register WHERE status = 'Entwurf'"),
        "in_pruefung":          scalar("SELECT COUNT(*) FROM idv_register WHERE status = 'In Prüfung'"),
        "kritisch_gda4":        scalar("SELECT COUNT(*) FROM idv_register WHERE gda_wert = 4 AND status NOT IN ('Archiviert')"),
        "steuerungsrelevant":   scalar("SELECT COUNT(*) FROM idv_register WHERE steuerungsrelevant = 1 AND status NOT IN ('Archiviert')"),
        "dora_kritisch":        scalar("SELECT COUNT(*) FROM idv_register WHERE dora_kritisch_wichtig = 1 AND status NOT IN ('Archiviert')"),
        "pruefung_ueberfaellig":scalar("SELECT COUNT(*) FROM idv_register WHERE naechste_pruefung < date('now') AND status NOT IN ('Archiviert','Abgekündigt')"),
        "pruefung_30_tage":     scalar("SELECT COUNT(*) FROM idv_register WHERE naechste_pruefung BETWEEN date('now') AND date('now','+30 days') AND status NOT IN ('Archiviert','Abgekündigt')"),
        "massnahmen_offen":     scalar("SELECT COUNT(*) FROM massnahmen WHERE status IN ('Offen','In Bearbeitung')"),
        "massnahmen_ueberfaellig": scalar("SELECT COUNT(*) FROM massnahmen WHERE faellig_am < date('now') AND status IN ('Offen','In Bearbeitung')"),
        "unvollstaendig":       scalar("SELECT COUNT(*) FROM v_unvollstaendige_idvs"),
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
        "INSERT OR IGNORE INTO org_units (kuerzel, bezeichnung, ebene) VALUES (?,?,?)",
        [
            ("VOR",  "Vorstand",                     "Vorstand"),
            ("FIL",  "Filialvertrieb",               "Bereich"),
            ("KRE",  "Kreditabteilung",              "Abteilung"),
            ("BWK",  "Betriebswirtschaft/Controlling","Abteilung"),
            ("ITS",  "IT & IT-Sicherheit",           "Abteilung"),
            ("RIS",  "Risikocontrolling",            "Abteilung"),
            ("REV",  "Interne Revision",             "Abteilung"),
            ("BWL",  "Rechnungswesen/Buchhaltung",   "Abteilung"),
            ("MEL",  "Meldewesen",                   "Abteilung"),
        ]
    )

    # Personen
    conn.executemany(
        "INSERT OR IGNORE INTO persons (kuerzel, nachname, vorname, email, rolle, org_unit_id) "
        "VALUES (?,?,?,?,?, (SELECT id FROM org_units WHERE kuerzel=?))",
        [
            ("IDV-KO", "Mustermann", "Max", "m.mustermann@volksbank.de", "IDV-Koordinator", "ITS"),
            ("FV-BWK", "Beispiel",   "Anna","a.beispiel@volksbank.de",   "Fachverantwortlicher","BWK"),
            ("FV-KRE", "Schmidt",    "Klaus","k.schmidt@volksbank.de",   "Fachverantwortlicher","KRE"),
        ]
    )

    # Geschäftsprozesse
    conn.executemany(
        "INSERT OR IGNORE INTO geschaeftsprozesse "
        "(gp_nummer, bezeichnung, bereich, ist_kritisch, ist_wesentlich, org_unit_id) "
        "VALUES (?,?,?,?,?, (SELECT id FROM org_units WHERE kuerzel=?))",
        [
            ("GP-BWK-001","Monatliche GuV-Berechnung",      "Steuerung",  1,1,"BWK"),
            ("GP-KRE-001","Kreditentscheidung Firmenkunden","Marktfolge", 1,1,"KRE"),
            ("GP-MEL-001","Meldewesen EBA/Bundesbank",      "Steuerung",  1,1,"MEL"),
            ("GP-RIS-001","Zinsrisiko-Steuerung",           "Steuerung",  1,1,"RIS"),
        ]
    )

    # Plattformen
    conn.executemany(
        "INSERT OR IGNORE INTO plattformen (bezeichnung, typ, hersteller) VALUES (?,?,?)",
        [
            ("Microsoft Excel 2021",    "Desktop", "Microsoft"),
            ("Microsoft Access 2021",   "Desktop", "Microsoft"),
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
        "relevant_guv":            1,
        "rechnungslegungsrelevant":1,
        "rechnungslegungsrelevanz_begr": "Grundlage für HGB-Monatsabschluss.",
        "gda_wert":                4,
        "gda_begruendung":         "Prozess kann ohne diese IDV nicht durchgeführt werden. "
                                   "Kein manueller Alternativprozess vorhanden.",
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
