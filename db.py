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

from db_pragmas import apply_pragmas
from db_write_tx import write_tx


def _resource_path(relative: str) -> Path:
    """Gibt den korrekten Pfad zurück – auch im PyInstaller-Bundle."""
    if hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS) / relative
    return Path(__file__).parent / relative


# ---------------------------------------------------------------------------
# Verbindung & Initialisierung
# ---------------------------------------------------------------------------

def get_connection(db_path: str, *, role: str = "reader") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    apply_pragmas(conn, role=role)
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
    _migrate_risikoklasse(conn)
    return conn


def _migrate_risikoklasse(conn: sqlite3.Connection) -> None:
    """Entfernt risikoklasse_id und risikoklassen-Tabelle aus bestehenden Datenbanken."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(idv_register)").fetchall()}
    if "risikoklasse_id" in cols:
        conn.execute("ALTER TABLE idv_register DROP COLUMN risikoklasse_id")
        conn.commit()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "risikoklassen" in tables:
        conn.execute("DROP TABLE risikoklassen")
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

    def _body():
        for a in antworten:
            kid = a["kriterium_id"]
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

        # Entwicklungsart aus Wesentlichkeit ableiten:
        # 'idv' ↔ 'arbeitshilfe' wird automatisch umgeschaltet, sobald mindestens
        # ein aktives Kriterium erfüllt ist. Manuell gesetzte 'eigenprogrammierung'
        # oder 'auftragsprogrammierung' bleiben unangetastet.
        wesentlich = conn.execute("""
            SELECT EXISTS (
                SELECT 1 FROM idv_wesentlichkeit w
                JOIN wesentlichkeitskriterien k ON k.id = w.kriterium_id
                WHERE w.idv_db_id = ? AND w.erfuellt = 1 AND k.aktiv = 1
            )
        """, (idv_db_id,)).fetchone()[0]
        neue_art = "idv" if wesentlich else "arbeitshilfe"
        conn.execute("""
            UPDATE idv_register
            SET entwicklungsart = ?, aktualisiert_am = ?
            WHERE id = ?
              AND entwicklungsart IN ('idv', 'arbeitshilfe')
              AND entwicklungsart != ?
        """, (neue_art, now, idv_db_id, neue_art))

    if commit:
        with write_tx(conn):
            _body()
    else:
        _body()


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
        "entwicklungsart":           data.get("entwicklungsart", "arbeitshilfe"),
        "gp_id":                     data.get("gp_id"),
        "gp_freitext":               data.get("gp_freitext"),
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

    def _body():
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
        return new_id

    if commit:
        with write_tx(conn):
            new_id = _body()
    else:
        new_id = _body()
    return new_id


def update_idv(conn: sqlite3.Connection, idv_db_id: int,
               data: dict, geaendert_von_id: Optional[int] = None,
               commit: bool = True) -> bool:
    """Aktualisiert einen IDV-Eintrag und schreibt die Änderungen in die History.

    commit=False erlaubt es, mehrere Writes in eine umschliessende
    write_tx-Transaktion einzubetten (z. B. update_idv +
    save_idv_wesentlichkeit)."""
    now = datetime.now(timezone.utc).isoformat()

    old = conn.execute(
        "SELECT * FROM idv_register WHERE id = ?", (idv_db_id,)
    ).fetchone()
    if not old:
        return False

    # Änderungsprotokoll aufbauen
    tracked_fields = [
        "bezeichnung", "idv_typ", "entwicklungsart", "status",
        "fachverantwortlicher_id", "gp_id",
        "naechste_pruefung", "pruefintervall_monate",
        "teststatus",
    ]
    changes = {}
    for f in tracked_fields:
        if f in data and str(data[f]) != str(old[f]):
            changes[f] = {"alt": old[f], "neu": data[f]}

    # Update ausführen
    update_fields = {k: v for k, v in data.items() if k in [
        "bezeichnung", "kurzbeschreibung", "version", "idv_typ",
        "entwicklungsart",
        "gp_id", "gp_freitext",
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
        "erstellt_fuer", "schnittstellen_beschr",
        "teststatus",
        "letzte_aenderungsart", "letzte_aenderungsbegruendung",
    ]}
    update_fields["aktualisiert_am"] = now

    set_clause = ", ".join(f"{k} = :{k}" for k in update_fields)

    def _body():
        conn.execute(
            f"UPDATE idv_register SET {set_clause} WHERE id = :__id",
            {**update_fields, "__id": idv_db_id}
        )

        if changes:
            conn.execute("""
                INSERT INTO idv_history (idv_id, aktion, geaenderte_felder, durchgefuehrt_von_id)
                VALUES (?, 'geaendert', ?, ?)
            """, (idv_db_id, json.dumps(changes, ensure_ascii=False), geaendert_von_id))

    if commit:
        with write_tx(conn):
            _body()
    else:
        _body()
    return True


def change_status(conn: sqlite3.Connection, idv_db_id: int,
                  new_status: str, kommentar: str = "",
                  geaendert_von_id: Optional[int] = None):
    """Ändert den Workflow-Status eines IDV-Eintrags."""
    now = datetime.now(timezone.utc).isoformat()
    # Kommentar-Suffix (Datei-Hash) außerhalb der Transaktion ermitteln,
    # damit BEGIN IMMEDIATE möglichst kurz gehalten wird.
    if new_status == "Freigegeben":
        row = conn.execute(
            "SELECT f.file_hash FROM idv_register r "
            "LEFT JOIN idv_files f ON r.file_id = f.id WHERE r.id = ?",
            (idv_db_id,)
        ).fetchone()
        if row and row["file_hash"]:
            kommentar = (kommentar or "") + f" [Datei-Hash: {row['file_hash'][:16]}...]"

    with write_tx(conn):
        conn.execute("""
            UPDATE idv_register
            SET status = ?, status_geaendert_am = ?, status_geaendert_von_id = ?, aktualisiert_am = ?
            WHERE id = ?
        """, (new_status, now, geaendert_von_id, now, idv_db_id))
        conn.execute("""
            INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id)
            VALUES (?, 'status_geaendert', ?, ?)
        """, (idv_db_id, f"Status → {new_status}. {kommentar}", geaendert_von_id))


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
    """Legt umfassende Demo-Stammdaten und einen Beispiel-IDV-Bestand an.

    Alle Inserts sind idempotent (INSERT OR IGNORE) und referenzieren
    Fremdschlüssel über Subselects auf natürliche Schlüssel. Die Funktion
    kann daher gefahrlos mehrfach aufgerufen werden.
    """
    now = datetime.now(timezone.utc).isoformat()
    today = date.today()
    iso_today = today.isoformat()

    # -----------------------------------------------------------------------
    # 1. Organisationseinheiten: Vorstand + 8 Fachabteilungen (Hierarchie)
    # -----------------------------------------------------------------------
    conn.execute(
        "INSERT OR IGNORE INTO org_units (bezeichnung, parent_id) VALUES (?, NULL)",
        ("Vorstand",),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO org_units (bezeichnung, parent_id) "
        "VALUES (?, (SELECT id FROM org_units WHERE bezeichnung=?))",
        [
            ("Betriebswirtschaft/Controlling", "Vorstand"),
            ("Rechnungswesen",                 "Vorstand"),
            ("Kreditabteilung",                "Vorstand"),
            ("Meldewesen",                     "Vorstand"),
            ("Risikocontrolling",              "Vorstand"),
            ("IT & IT-Sicherheit",             "Vorstand"),
            ("Revision",                       "Vorstand"),
            ("Marktfolge Aktiv",               "Kreditabteilung"),
        ],
    )

    # -----------------------------------------------------------------------
    # 2. Personen
    # -----------------------------------------------------------------------
    conn.executemany(
        "INSERT OR IGNORE INTO persons "
        "(kuerzel, nachname, vorname, email, rolle, org_unit_id) "
        "VALUES (?,?,?,?,?, (SELECT id FROM org_units WHERE bezeichnung=?))",
        [
            ("IDV-KO",  "Mustermann", "Max",     "m.mustermann@volksbank.de", "IDV-Koordinator",     "IT & IT-Sicherheit"),
            ("FV-BWK",  "Beispiel",   "Anna",    "a.beispiel@volksbank.de",   "Fachverantwortlicher","Betriebswirtschaft/Controlling"),
            ("FV-KRE",  "Schmidt",    "Klaus",   "k.schmidt@volksbank.de",    "Fachverantwortlicher","Kreditabteilung"),
            ("FV-MEL",  "Meier",      "Lisa",    "l.meier@volksbank.de",      "Fachverantwortlicher","Meldewesen"),
            ("FV-RIS",  "Berger",     "Tobias",  "t.berger@volksbank.de",     "Fachverantwortlicher","Risikocontrolling"),
            ("IT-SI",   "Winter",     "Sabine",  "s.winter@volksbank.de",     "IT-Sicherheit",       "IT & IT-Sicherheit"),
            ("REV",     "Krüger",     "Hans",    "h.krueger@volksbank.de",    "Revision",            "Revision"),
            ("IDV-ENT", "Keller",     "Julia",   "j.keller@volksbank.de",     "IDV-Entwickler",      "IT & IT-Sicherheit"),
        ],
    )

    # -----------------------------------------------------------------------
    # 3. Geschäftsprozesse
    # -----------------------------------------------------------------------
    conn.executemany(
        "INSERT OR IGNORE INTO geschaeftsprozesse "
        "(gp_nummer, bezeichnung, bereich, ist_kritisch, ist_wesentlich, org_unit_id) "
        "VALUES (?,?,?,?,?, (SELECT id FROM org_units WHERE bezeichnung=?))",
        [
            ("GP-BWK-001", "Monatliche GuV-Berechnung",        "Steuerung",  1, 1, "Betriebswirtschaft/Controlling"),
            ("GP-BWK-002", "Quartalsreporting CIR",            "Steuerung",  0, 1, "Betriebswirtschaft/Controlling"),
            ("GP-KRE-001", "Kreditentscheidung Firmenkunden",  "Marktfolge", 1, 1, "Kreditabteilung"),
            ("GP-KRE-002", "Sicherheitenbewertung",            "Marktfolge", 1, 1, "Kreditabteilung"),
            ("GP-MEL-001", "Meldewesen EBA/Bundesbank COREP",  "Steuerung",  1, 1, "Meldewesen"),
            ("GP-MEL-002", "FINREP-Meldung",                   "Steuerung",  1, 1, "Meldewesen"),
            ("GP-RIS-001", "Zinsrisiko-Steuerung",             "Steuerung",  1, 1, "Risikocontrolling"),
            ("GP-RIS-002", "Stresstest-Berechnung",            "Steuerung",  1, 1, "Risikocontrolling"),
        ],
    )

    # -----------------------------------------------------------------------
    # 4. IDV-Register (8 Einträge, mix aus idv/arbeitshilfe/eigenprog.)
    # -----------------------------------------------------------------------
    # Spaltenreihenfolge:
    #   idv_id, bezeichnung, kurzbeschreibung, idv_typ, entwicklungsart,
    #   status, pruefintervall_monate, letzte_pruefung, naechste_pruefung,
    #   produktiv_seit, nutzungsfrequenz, nutzeranzahl,
    #   dokumentation_vorhanden, testkonzept_vorhanden, vier_augen_prinzip,
    #   enthaelt_personendaten, datenschutz_kategorie,
    #   gp_nummer, org_unit, fachv_kuerzel, entw_kuerzel, koord_kuerzel,
    #   plattform_bezeichnung
    idv_rows = [
        ("IDV-2024-001", "GuV-Auswertung Excel-Makro",
         "Monatliche GuV-Berechnung mit VBA-Makro – rechnungslegungsrelevant.",
         "Excel-Makro", "idv", "Genehmigt", 12,
         "2025-10-15", "2026-10-15", "2022-03-01",
         "monatlich", 5, 1, 1, 1, 0, "keine",
         "GP-BWK-001", "Betriebswirtschaft/Controlling", "FV-BWK", "IDV-ENT", "IDV-KO",
         "Microsoft Excel"),
        ("IDV-2024-002", "Sicherheiten-Bewertung Access-DB",
         "Access-Datenbank zur Bewertung von Kreditsicherheiten.",
         "Access-Datenbank", "idv", "Genehmigt", 12,
         "2025-11-20", "2026-11-20", "2023-06-01",
         "wöchentlich", 8, 1, 1, 1, 1, "allgemein",
         "GP-KRE-002", "Kreditabteilung", "FV-KRE", "IDV-ENT", "IDV-KO",
         "Microsoft Access"),
        ("IDV-2024-003", "Reporting-Arbeitshilfe Vorstand",
         "Excel-Arbeitshilfe zur Aufbereitung der Monatsberichte für den Vorstand.",
         "Excel-Tabelle", "arbeitshilfe", "Entwurf", 24,
         None, "2027-04-01", "2024-02-01",
         "monatlich", 3, 0, 0, 0, 0, "keine",
         "GP-BWK-002", "Betriebswirtschaft/Controlling", "FV-BWK", "FV-BWK", "IDV-KO",
         "Microsoft Excel"),
        ("IDV-2024-004", "EBA COREP Datenlieferung",
         "Python-Skript zur Aufbereitung der COREP-Meldedaten an die Bundesbank.",
         "Python-Skript", "idv", "Genehmigt", 6,
         "2025-12-05", "2026-06-05", "2024-01-15",
         "quartalsweise", 2, 1, 1, 1, 0, "keine",
         "GP-MEL-001", "Meldewesen", "FV-MEL", "IDV-ENT", "IDV-KO",
         "Python 3.11"),
        ("IDV-2025-001", "FINREP-Meldewesen",
         "Zentrales Python-Framework für FINREP-Meldungen, IT-Entwicklung.",
         "Python-Skript", "eigenprogrammierung", "In Prüfung", 12,
         None, "2026-07-01", "2025-02-10",
         "quartalsweise", 4, 1, 1, 0, 0, "keine",
         "GP-MEL-002", "Meldewesen", "FV-MEL", "IDV-ENT", "IDV-KO",
         "Python 3.11"),
        ("IDV-2025-002", "Zinsrisiko-Modell",
         "Excel-Modell zur Berechnung des Barwertrisikos (Zinsschock).",
         "Excel-Modell", "idv", "Genehmigt", 12,
         "2026-01-20", "2027-01-20", "2023-09-01",
         "monatlich", 3, 1, 1, 1, 0, "keine",
         "GP-RIS-001", "Risikocontrolling", "FV-RIS", "FV-RIS", "IDV-KO",
         "Microsoft Excel"),
        ("IDV-2025-003", "Stresstest-Szenarien",
         "Excel-Arbeitshilfe zur Zusammenstellung von Stresstest-Szenarien.",
         "Excel-Tabelle", "arbeitshilfe", "Entwurf", 24,
         None, "2027-05-01", "2025-05-20",
         "jährlich", 2, 0, 0, 0, 0, "keine",
         "GP-RIS-002", "Risikocontrolling", "FV-RIS", "FV-RIS", "IDV-KO",
         "Microsoft Excel"),
        ("IDV-2025-004", "Firmenkunden-Score",
         "SQL-basiertes Scoring für Firmenkundenkredite, zentrale IT-Entwicklung.",
         "SQL-Skript", "eigenprogrammierung", "In Prüfung", 12,
         None, "2026-08-15", "2025-08-01",
         "täglich", 12, 1, 0, 0, 1, "allgemein",
         "GP-KRE-001", "Kreditabteilung", "FV-KRE", "IDV-ENT", "IDV-KO",
         "Shell-Skripte"),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO idv_register ("
        " idv_id, bezeichnung, kurzbeschreibung, idv_typ, entwicklungsart,"
        " status, pruefintervall_monate, letzte_pruefung, naechste_pruefung,"
        " produktiv_seit, nutzungsfrequenz, nutzeranzahl,"
        " dokumentation_vorhanden, testkonzept_vorhanden, vier_augen_prinzip,"
        " enthaelt_personendaten, datenschutz_kategorie,"
        " gp_id, org_unit_id,"
        " fachverantwortlicher_id, idv_entwickler_id, idv_koordinator_id,"
        " plattform_id"
        ") VALUES ("
        " ?,?,?,?,?,"
        " ?,?,?,?,"
        " ?,?,?,"
        " ?,?,?,"
        " ?,?,"
        " (SELECT id FROM geschaeftsprozesse WHERE gp_nummer=?),"
        " (SELECT id FROM org_units WHERE bezeichnung=?),"
        " (SELECT id FROM persons WHERE kuerzel=?),"
        " (SELECT id FROM persons WHERE kuerzel=?),"
        " (SELECT id FROM persons WHERE kuerzel=?),"
        " (SELECT id FROM plattformen WHERE bezeichnung=?)"
        ")",
        idv_rows,
    )

    # -----------------------------------------------------------------------
    # 5. Prüfungen (4 Einträge)
    # -----------------------------------------------------------------------
    conn.executemany(
        "INSERT OR IGNORE INTO pruefungen ("
        " idv_id, pruefungsart, pruefungsdatum, pruefer_id, ergebnis,"
        " befunde, massnahmen_erforderlich, frist_massnahmen,"
        " abgeschlossen, abschlussdatum, naechste_pruefung, kommentar"
        ") VALUES ("
        " (SELECT id FROM idv_register WHERE idv_id=?),"
        " ?,?,"
        " (SELECT id FROM persons WHERE kuerzel=?),"
        " ?,?,?,?,?,?,?,?)",
        [
            ("IDV-2024-001", "Erstprüfung",  "2024-03-20", "IDV-KO",
             "Mit Befund",
             "Dokumentation unvollständig, Vier-Augen-Prinzip fehlte.",
             1, "2024-06-30", 1, "2024-06-25", "2025-10-15",
             "Erstfreigabe nach Nachbesserung erteilt."),
            ("IDV-2024-001", "Regelprüfung", "2025-10-15", "IDV-KO",
             "Ohne Befund",
             None,
             0, None, 1, "2025-10-15", "2026-10-15",
             "Jahresprüfung erfolgreich abgeschlossen."),
            ("IDV-2024-002", "Erstprüfung",  "2024-07-10", "REV",
             "Mit Befund",
             "Zugriffsschutz der Access-DB nicht ausreichend dokumentiert.",
             1, "2024-09-30", 1, "2024-09-28", "2025-11-20",
             "Maßnahme zur Verbesserung des Zugriffsschutzes umgesetzt."),
            ("IDV-2024-004", "Erstprüfung",  "2024-05-15", "IT-SI",
             "Kritischer Befund",
             "Python-Skript ohne Versionskontrolle, Logging unzureichend.",
             1, "2024-08-31", 0, None, "2025-12-05",
             "Maßnahmen in Bearbeitung (Git-Einführung)."),
        ],
    )

    # -----------------------------------------------------------------------
    # 6. Maßnahmen (3 Einträge, aus Prüfungsbefunden abgeleitet)
    # -----------------------------------------------------------------------
    conn.executemany(
        "INSERT OR IGNORE INTO massnahmen ("
        " idv_id, pruefung_id, titel, beschreibung,"
        " massnahmentyp, prioritaet, verantwortlicher_id,"
        " faellig_am, status, erledigt_am, erledigt_von_id"
        ") VALUES ("
        " (SELECT id FROM idv_register WHERE idv_id=?),"
        " (SELECT id FROM pruefungen WHERE idv_id=(SELECT id FROM idv_register WHERE idv_id=?) AND pruefungsart=?),"
        " ?,?,?,?,"
        " (SELECT id FROM persons WHERE kuerzel=?),"
        " ?,?,?,"
        " (SELECT id FROM persons WHERE kuerzel=?))",
        [
            ("IDV-2024-001", "IDV-2024-001", "Erstprüfung",
             "Dokumentation ergänzen",
             "Fachkonzept und Betriebshandbuch vollständig erstellen.",
             "Dokumentation", "Hoch", "FV-BWK",
             "2024-06-30", "Erledigt", "2024-06-20", "FV-BWK"),
            ("IDV-2024-002", "IDV-2024-002", "Erstprüfung",
             "Zugriffsschutz Access-DB",
             "Berechtigungskonzept mit IT-Sicherheit abstimmen und dokumentieren.",
             "Technisch", "Kritisch", "IT-SI",
             "2024-09-30", "Erledigt", "2024-09-15", "IT-SI"),
            ("IDV-2024-004", "IDV-2024-004", "Erstprüfung",
             "Git-Versionskontrolle einführen",
             "Python-Skript in zentrales Git-Repository migrieren.",
             "Technisch", "Mittel", "IDV-ENT",
             "2024-08-31", "In Bearbeitung", None, None),
        ],
    )

    # -----------------------------------------------------------------------
    # 7. Genehmigungen (4 Einträge, für die genehmigten IDVs)
    # -----------------------------------------------------------------------
    conn.executemany(
        "INSERT OR IGNORE INTO genehmigungen ("
        " idv_id, genehmigungsart, antragsteller_id, antragsdatum,"
        " genehmiger1_id, genehmigt1_am, genehmigt1_status, genehmigt1_kommentar,"
        " genehmiger2_id, genehmigt2_am, genehmigt2_status, genehmigt2_kommentar,"
        " gesamtstatus, abschlussdatum"
        ") VALUES ("
        " (SELECT id FROM idv_register WHERE idv_id=?),"
        " ?,"
        " (SELECT id FROM persons WHERE kuerzel=?), ?,"
        " (SELECT id FROM persons WHERE kuerzel=?), ?, ?, ?,"
        " (SELECT id FROM persons WHERE kuerzel=?), ?, ?, ?,"
        " ?, ?)",
        [
            ("IDV-2024-001", "Erstfreigabe", "FV-BWK", "2024-03-10",
             "IDV-KO", "2024-06-28", "Genehmigt", "Freigabe nach Nachbesserung.",
             "IT-SI", "2024-06-28", "Genehmigt", "Keine sicherheitskritischen Funde.",
             "Genehmigt", "2024-06-28"),
            ("IDV-2024-002", "Erstfreigabe", "FV-KRE", "2024-07-01",
             "IDV-KO", "2024-10-02", "Genehmigt", "Berechtigungskonzept umgesetzt.",
             "IT-SI", "2024-10-02", "Genehmigt", "Zugriffsschutz geprüft.",
             "Genehmigt", "2024-10-02"),
            ("IDV-2024-004", "Erstfreigabe", "FV-MEL", "2024-05-01",
             "IDV-KO", "2024-06-15", "Genehmigt", "Freigabe mit Auflage (Git-Einführung).",
             "IT-SI", "2024-06-15", "Genehmigt", "DORA-Anforderungen erfüllt.",
             "Genehmigt", "2024-06-15"),
            ("IDV-2025-002", "Erstfreigabe", "FV-RIS", "2025-11-15",
             "IDV-KO", "2026-01-22", "Genehmigt", "Zinsrisiko-Modell validiert.",
             "IT-SI", "2026-01-22", "Genehmigt", "Keine IT-sicherheitsrelevanten Befunde.",
             "Genehmigt", "2026-01-22"),
        ],
    )

    # -----------------------------------------------------------------------
    # 8. Fachliche Testfälle (3 Einträge)
    # -----------------------------------------------------------------------
    conn.executemany(
        "INSERT OR IGNORE INTO fachliche_testfaelle ("
        " idv_id, testfall_nr, beschreibung, parametrisierung, testdaten,"
        " erwartetes_ergebnis, erzieltes_ergebnis, bewertung, tester, testdatum"
        ") VALUES ("
        " (SELECT id FROM idv_register WHERE idv_id=?),"
        " ?,?,?,?,?,?,?,?,?)",
        [
            ("IDV-2024-001", 1,
             "GuV-Berechnung Monatsschluss März 2024",
             "Berichtsmonat=März 2024, Mandant=Volksbank",
             "Buchungsstände SAP März 2024",
             "GuV stimmt mit SAP-Kontensalden überein (Toleranz 0,01 EUR).",
             "Abweichung 0,00 EUR.", "Erledigt",
             "Anna Beispiel", "2024-04-02"),
            ("IDV-2024-001", 2,
             "Jahresabschluss-Simulation 2023",
             "Berichtsmonat=Dezember 2023, Szenario=Ist-Abschluss",
             "Testdaten Jahresabschluss 2023",
             "GuV-Summen konsistent mit Bilanz.",
             "Ergebnis korrekt.", "Erledigt",
             "Anna Beispiel", "2024-04-05"),
            ("IDV-2024-002", 1,
             "Sicherheitenbewertung Beispielkredit",
             "Kundennr=Muster-001, Sicherheit=Grundschuld",
             "Beispielkreditvertrag mit Grundschuld 250.000 EUR",
             "Beleihungswert 200.000 EUR (80 %).",
             "Beleihungswert korrekt berechnet.", "Erledigt",
             "Klaus Schmidt", "2024-07-05"),
        ],
    )

    # -----------------------------------------------------------------------
    # 9. IDV-Abhängigkeiten (2 Einträge)
    # -----------------------------------------------------------------------
    conn.executemany(
        "INSERT OR IGNORE INTO idv_abhaengigkeiten ("
        " quell_idv_id, ziel_idv_id, abhaengigkeitstyp, beschreibung"
        ") VALUES ("
        " (SELECT id FROM idv_register WHERE idv_id=?),"
        " (SELECT id FROM idv_register WHERE idv_id=?),"
        " ?, ?)",
        [
            ("IDV-2024-001", "IDV-2024-003", "Datenlieferant",
             "GuV-Auswertung liefert Werte an die Reporting-Arbeitshilfe."),
            ("IDV-2024-001", "IDV-2024-004", "Datenlieferant",
             "GuV-Zahlen fließen in die COREP-Datenlieferung ein."),
        ],
    )

    # -----------------------------------------------------------------------
    # 10. Wesentlichkeitsbewertung – Antworten je IDV auf die drei
    #     Beispielkriterien (schema.sql hat sie bereits angelegt).
    # -----------------------------------------------------------------------
    #  (idv_id, kriterium_bezeichnung, erfuellt, begruendung)
    wesentlichkeit_rows = [
        # IDV-2024-001 – rechnungslegungsrelevant, steuerungsrelevant
        ("IDV-2024-001", "Rechnungslegungs-Relevanz (GoB)", 1,
         "Generiert monatliche GuV-Positionen, die direkt in die Bilanz einfließen."),
        ("IDV-2024-001", "Risiko / Steuerungs-Relevanz im Sinne der MaRisk", 1,
         "Grundlage für Monatsberichte an den Vorstand."),
        ("IDV-2024-001", "Kritische oder wichtige Funktionen", 0, None),
        # IDV-2024-002 – Steuerungs-Relevanz
        ("IDV-2024-002", "Rechnungslegungs-Relevanz (GoB)", 0, None),
        ("IDV-2024-002", "Risiko / Steuerungs-Relevanz im Sinne der MaRisk", 1,
         "Sicherheitenwerte fließen in die Kreditrisikosteuerung ein."),
        ("IDV-2024-002", "Kritische oder wichtige Funktionen", 1,
         "Abhängigkeit der Kreditvergabe von der Sicherheitenbewertung."),
        # IDV-2024-003 – Arbeitshilfe, keine Wesentlichkeit
        ("IDV-2024-003", "Rechnungslegungs-Relevanz (GoB)", 0, None),
        ("IDV-2024-003", "Risiko / Steuerungs-Relevanz im Sinne der MaRisk", 0, None),
        ("IDV-2024-003", "Kritische oder wichtige Funktionen", 0, None),
        # IDV-2024-004 – Meldewesen
        ("IDV-2024-004", "Rechnungslegungs-Relevanz (GoB)", 0, None),
        ("IDV-2024-004", "Risiko / Steuerungs-Relevanz im Sinne der MaRisk", 1,
         "COREP-Meldung an Bundesbank, bankaufsichtsrechtlich zwingend."),
        ("IDV-2024-004", "Kritische oder wichtige Funktionen", 1,
         "Meldewesen ist als kritische Funktion klassifiziert (DORA Art. 28)."),
        # IDV-2025-001 – Eigenprogrammierung, Meldewesen
        ("IDV-2025-001", "Rechnungslegungs-Relevanz (GoB)", 1,
         "FINREP liefert Kennzahlen für den Konzernabschluss."),
        ("IDV-2025-001", "Risiko / Steuerungs-Relevanz im Sinne der MaRisk", 1,
         "FINREP ist bankaufsichtsrechtlich verpflichtend."),
        ("IDV-2025-001", "Kritische oder wichtige Funktionen", 1,
         "Meldewesen ist kritische Funktion."),
        # IDV-2025-002 – Zinsrisiko
        ("IDV-2025-002", "Rechnungslegungs-Relevanz (GoB)", 0, None),
        ("IDV-2025-002", "Risiko / Steuerungs-Relevanz im Sinne der MaRisk", 1,
         "Zinsrisiko-Modell ist Pflichtauswertung nach MaRisk."),
        ("IDV-2025-002", "Kritische oder wichtige Funktionen", 0, None),
        # IDV-2025-003 – Arbeitshilfe
        ("IDV-2025-003", "Rechnungslegungs-Relevanz (GoB)", 0, None),
        ("IDV-2025-003", "Risiko / Steuerungs-Relevanz im Sinne der MaRisk", 0, None),
        ("IDV-2025-003", "Kritische oder wichtige Funktionen", 0, None),
        # IDV-2025-004 – Firmenkunden-Score
        ("IDV-2025-004", "Rechnungslegungs-Relevanz (GoB)", 0, None),
        ("IDV-2025-004", "Risiko / Steuerungs-Relevanz im Sinne der MaRisk", 1,
         "Score fließt in die Kreditentscheidung und Risikosteuerung ein."),
        ("IDV-2025-004", "Kritische oder wichtige Funktionen", 1,
         "Kreditvergabe ist kritische Funktion."),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO idv_wesentlichkeit ("
        " idv_db_id, kriterium_id, erfuellt, begruendung, geaendert_am"
        ") VALUES ("
        " (SELECT id FROM idv_register WHERE idv_id=?),"
        " (SELECT id FROM wesentlichkeitskriterien WHERE bezeichnung=?),"
        " ?, ?, ?)",
        [(idv, krit, erf, begr, now) for (idv, krit, erf, begr) in wesentlichkeit_rows],
    )

    # Commit before returning so the caller's set_setting() (writer thread)
    # doesn't deadlock against this connection's open write transaction.
    conn.commit()

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
    with write_tx(conn):
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
        return cur.lastrowid


def update_fachlicher_testfall(conn: sqlite3.Connection, testfall_id: int, data: dict) -> None:
    """Aktualisiert einen vorhandenen fachlichen Testfall."""
    now = datetime.now(timezone.utc).isoformat()
    with write_tx(conn):
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


def delete_fachlicher_testfall(conn: sqlite3.Connection, testfall_id: int) -> None:
    """Löscht einen fachlichen Testfall."""
    with write_tx(conn):
        conn.execute("DELETE FROM fachliche_testfaelle WHERE id = ?", (testfall_id,))


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
    with write_tx(conn):
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


def delete_technischer_test(conn: sqlite3.Connection, idv_db_id: int) -> None:
    """Löscht den technischen Test einer IDV."""
    with write_tx(conn):
        conn.execute("DELETE FROM technischer_test WHERE idv_id = ?", (idv_db_id,))


# ---------------------------------------------------------------------------
# Scanner-Protokoll-Handler
# ---------------------------------------------------------------------------
# Werden von der Webapp aufgerufen, wenn sie eine NDJSON-Zeile des
# Scanner-Subprozesses ueber den stdout-Reader empfangen hat. Alle
# Handler laufen innerhalb des Webapp-Writer-Threads; sie kapseln
# jeweils genau eine atomare Transaktion (write_tx) und erwarten als
# Eingabe ein dict mit den Protokollfeldern.

_IDV_FILES_COLUMNS = (
    "file_hash", "full_path", "file_name", "extension", "share_root",
    "relative_path", "size_bytes", "created_at", "modified_at", "file_owner",
    "office_author", "office_last_author", "office_created", "office_modified",
    "has_macros", "has_external_links", "sheet_count", "named_ranges_count",
    "formula_count",
    "has_sheet_protection", "protected_sheets_count",
    "sheet_protection_has_pw", "workbook_protected",
    "ist_cognos_report", "cognos_report_name", "cognos_paket_pfad",
    "cognos_abfragen_anzahl", "cognos_datenpunkte_anzahl", "cognos_filter_anzahl",
    "cognos_seiten_anzahl", "cognos_parameter_anzahl", "cognos_namespace_version",
)


def apply_scan_run_start(conn: sqlite3.Connection, payload: dict) -> None:
    """Legt einen scan_runs-Eintrag an oder markiert ihn bei Resume als laufend.

    Erwartete Felder:
      * ``scan_run_id`` (erforderlich) – vom Webapp vorbelegter/gelesener Primaerschluessel
      * ``resume`` (bool) – True: UPDATE status='running', False: INSERT
      * ``started_at``, ``scan_paths`` – nur bei Neuanlage
    """
    with write_tx(conn):
        if payload.get("resume"):
            conn.execute(
                "UPDATE scan_runs SET scan_status='running' WHERE id=?",
                (payload["scan_run_id"],),
            )
        else:
            scan_paths = payload.get("scan_paths")
            if not isinstance(scan_paths, str):
                scan_paths = json.dumps(scan_paths or [], ensure_ascii=False)
            conn.execute(
                "INSERT INTO scan_runs (id, started_at, scan_paths, scan_status) "
                "VALUES (?, ?, ?, 'running')",
                (payload["scan_run_id"], payload["started_at"], scan_paths),
            )


def apply_scan_run_end(conn: sqlite3.Connection, payload: dict) -> None:
    """Schliesst den scan_runs-Eintrag ab (Status + Statistik).

    Erwartete Felder: ``scan_run_id``, ``finished_at``, ``status`` ('completed'|
    'cancelled'|'crashed'|'killed'), ``total``, ``new``, ``changed``, ``moved``,
    ``restored``, ``archived``, ``errors``.
    """
    with write_tx(conn):
        conn.execute(
            """
            UPDATE scan_runs SET
                finished_at = ?, total_files = ?, new_files = ?,
                changed_files = ?, moved_files = ?, restored_files = ?,
                archived_files = ?, errors = ?, scan_status = ?
            WHERE id = ?
            """,
            (
                payload["finished_at"],
                payload.get("total", 0),
                payload.get("new", 0),
                payload.get("changed", 0),
                payload.get("moved", 0),
                payload.get("restored", 0),
                payload.get("archived", 0),
                payload.get("errors", 0),
                payload.get("status", "completed"),
                payload["scan_run_id"],
            ),
        )


def apply_scanner_upsert_file(conn: sqlite3.Connection, payload: dict) -> None:
    """Spielt ein vom Scanner ermitteltes Ergebnis (new/changed/moved/restored/
    unchanged) atomar in ``idv_files`` + ``idv_file_history`` ein.

    Erwartete Felder:
      * ``action`` – 'insert' | 'update' | 'move'
      * ``scan_run_id``, ``now`` – Kontext des laufenden Scans
      * ``change_type`` – Text fuer History (new/changed/unchanged/moved/restored)
      * ``data`` – dict mit allen idv_files-Spalten (siehe ``_IDV_FILES_COLUMNS``)
      * ``file_id`` – bei update/move: die bestehende idv_files.id
      * ``old_hash`` – bei update/move fuer History
      * ``details`` – optional, JSON-String fuer History.details
    """
    action      = payload["action"]
    data        = payload["data"]
    scan_run_id = payload["scan_run_id"]
    now         = payload["now"]
    change_type = payload.get("change_type") or action
    details     = payload.get("details")

    source     = data.get("source") or "filesystem"
    sp_item_id = data.get("sharepoint_item_id")

    with write_tx(conn):
        if action == "insert":
            insert_data = {
                **{col: data.get(col) for col in _IDV_FILES_COLUMNS},
                "first_seen_at":      now,
                "last_seen_at":       now,
                "last_scan_run_id":   scan_run_id,
                "source":             source,
                "sharepoint_item_id": sp_item_id,
            }
            cols = ", ".join(insert_data.keys()) + ", status"
            placeholders = ", ".join(f":{k}" for k in insert_data.keys()) + ", 'active'"
            cur = conn.execute(
                f"INSERT INTO idv_files ({cols}) VALUES ({placeholders})",
                insert_data,
            )
            file_id = cur.lastrowid
            conn.execute(
                "INSERT INTO idv_file_history (file_id, scan_run_id, change_type, "
                "new_hash, changed_at) VALUES (?, ?, 'new', ?, ?)",
                (file_id, scan_run_id, data.get("file_hash"), now),
            )

        elif action == "move":
            file_id = payload["file_id"]
            conn.execute(
                """
                UPDATE idv_files SET
                    full_path = :full_path, share_root = :share_root,
                    relative_path = :relative_path,
                    source = :source, sharepoint_item_id = :sharepoint_item_id,
                    last_seen_at = :now, last_scan_run_id = :run_id
                WHERE id = :id
                """,
                {
                    "full_path":          data["full_path"],
                    "share_root":         data.get("share_root"),
                    "relative_path":      data.get("relative_path"),
                    "source":             source,
                    "sharepoint_item_id": sp_item_id,
                    "now":                now,
                    "run_id":             scan_run_id,
                    "id":                 file_id,
                },
            )
            conn.execute(
                "INSERT INTO idv_file_history (file_id, scan_run_id, change_type, "
                "old_hash, new_hash, changed_at, details) VALUES (?, ?, 'moved', ?, ?, ?, ?)",
                (file_id, scan_run_id, data.get("file_hash"), data.get("file_hash"),
                 now, details),
            )

        else:  # update (changed / unchanged / restored)
            file_id = payload["file_id"]
            update_data = {col: data.get(col) for col in _IDV_FILES_COLUMNS}
            update_data.update({
                "now":                now,
                "run_id":             scan_run_id,
                "id":                 file_id,
                "source":             source,
                "sharepoint_item_id": sp_item_id,
            })
            set_sql = ", ".join(f"{col} = :{col}" for col in _IDV_FILES_COLUMNS)
            conn.execute(
                f"UPDATE idv_files SET {set_sql}, "
                "source = :source, sharepoint_item_id = :sharepoint_item_id, "
                "last_seen_at = :now, last_scan_run_id = :run_id, status = 'active' "
                "WHERE id = :id",
                update_data,
            )
            conn.execute(
                "INSERT INTO idv_file_history (file_id, scan_run_id, change_type, "
                "old_hash, new_hash, changed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (file_id, scan_run_id, change_type,
                 payload.get("old_hash"), data.get("file_hash"), now),
            )


def apply_scanner_history(conn: sqlite3.Connection, payload: dict) -> None:
    """Standalone-History-Eintrag (z. B. 'archiviert' fuer einzelne Dateien).

    Erwartete Felder: ``file_id``, ``scan_run_id``, ``change_type``,
    ``changed_at``; optional ``old_hash``, ``new_hash``, ``details``.
    """
    with write_tx(conn):
        conn.execute(
            "INSERT INTO idv_file_history (file_id, scan_run_id, change_type, "
            "old_hash, new_hash, changed_at, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                payload["file_id"],
                payload["scan_run_id"],
                payload["change_type"],
                payload.get("old_hash"),
                payload.get("new_hash"),
                payload["changed_at"],
                payload.get("details"),
            ),
        )


def apply_scanner_archive_files(conn: sqlite3.Connection, payload: dict) -> None:
    """Archiviert eine Liste aktiver idv_files-Eintraege und schreibt
    einen 'archiviert'-History-Eintrag je Datei.

    Erwartete Felder: ``scan_run_id``, ``now``, ``file_ids`` (Liste[int]).
    """
    file_ids = payload.get("file_ids") or []
    if not file_ids:
        return
    scan_run_id = payload["scan_run_id"]
    now         = payload["now"]
    with write_tx(conn):
        placeholders = ",".join("?" * len(file_ids))
        conn.execute(
            f"UPDATE idv_files SET status = 'archiviert', last_seen_at = ? "
            f"WHERE id IN ({placeholders})",
            [now] + list(file_ids),
        )
        conn.executemany(
            "INSERT INTO idv_file_history (file_id, scan_run_id, change_type, changed_at) "
            "VALUES (?, ?, 'archiviert', ?)",
            [(fid, scan_run_id, now) for fid in file_ids],
        )


def apply_scanner_update_status(conn: sqlite3.Connection, payload: dict) -> None:
    """Wendet eine der vom Scanner emittierten ``bearbeitungsstatus``-
    Aktualisierungen an. Der Payload-Key ``kind`` waehlt die Variante:

    * ``auto_ignore_single`` – einzelne Datei (``full_path``) auf
      'Ignoriert' setzen, sofern noch 'Neu' und weder registriert noch
      verlinkt.
    * ``auto_classify_single`` – einzelne Datei (``full_path``,
      ``new_status``) klassifizieren unter denselben Schutzbedingungen.
    * ``auto_ignore_bulk_excel`` – alle aktiven 'Neu'-Excel-Dateien ohne
      Formeln/Makros (``extensions`` Liste) auf 'Ignoriert' setzen.
    * ``auto_classify_bulk_ah`` – AH-Praefix/Suffix → 'Nicht wesentlich'.
    * ``auto_classify_bulk_idv`` – IDV-Praefix/Suffix → 'Zur Registrierung'.
    """
    kind = payload["kind"]
    with write_tx(conn):
        if kind == "auto_ignore_single":
            conn.execute(
                "UPDATE idv_files SET bearbeitungsstatus = 'Ignoriert' "
                "WHERE full_path = ? AND status = 'active' "
                "  AND bearbeitungsstatus = 'Neu' "
                "  AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = idv_files.id)"
                "  AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = idv_files.id)",
                (payload["full_path"],),
            )
        elif kind == "auto_classify_single":
            conn.execute(
                "UPDATE idv_files SET bearbeitungsstatus = ? "
                "WHERE full_path = ? AND status = 'active' "
                "  AND bearbeitungsstatus = 'Neu' "
                "  AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = idv_files.id)"
                "  AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = idv_files.id)",
                (payload["new_status"], payload["full_path"]),
            )
        elif kind == "auto_ignore_bulk_excel":
            extensions = payload.get("extensions") or []
            if not extensions:
                return
            ext_placeholders = ",".join("?" * len(extensions))
            conn.execute(
                f"UPDATE idv_files "
                f"SET bearbeitungsstatus = 'Ignoriert' "
                f"WHERE status = 'active' "
                f"  AND bearbeitungsstatus = 'Neu' "
                f"  AND LOWER(extension) IN ({ext_placeholders}) "
                f"  AND (formula_count IS NULL OR formula_count = 0) "
                f"  AND (has_macros IS NULL OR has_macros = 0) "
                f"  AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = idv_files.id)"
                f"  AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = idv_files.id)",
                tuple(extensions),
            )
        elif kind == "auto_classify_bulk_ah":
            conn.execute(
                "UPDATE idv_files "
                "SET bearbeitungsstatus = 'Nicht wesentlich' "
                "WHERE status = 'active' "
                "  AND bearbeitungsstatus = 'Neu' "
                "  AND ("
                "      UPPER(SUBSTR(file_name, 1, 2)) = 'AH'"
                "      OR UPPER(SUBSTR(file_name,"
                "                     LENGTH(file_name) - LENGTH(extension) - 1,"
                "                     2)) = 'AH'"
                "  ) "
                "  AND NOT ("
                "      UPPER(SUBSTR(file_name, 1, 3)) = 'IDV'"
                "      OR UPPER(SUBSTR(file_name,"
                "                     LENGTH(file_name) - LENGTH(extension) - 2,"
                "                     3)) = 'IDV'"
                "  ) "
                "  AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = idv_files.id)"
                "  AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = idv_files.id)"
            )
        elif kind == "auto_classify_bulk_idv":
            conn.execute(
                "UPDATE idv_files "
                "SET bearbeitungsstatus = 'Zur Registrierung' "
                "WHERE status = 'active' "
                "  AND bearbeitungsstatus = 'Neu' "
                "  AND ("
                "      UPPER(SUBSTR(file_name, 1, 3)) = 'IDV'"
                "      OR UPPER(SUBSTR(file_name,"
                "                     LENGTH(file_name) - LENGTH(extension) - 2,"
                "                     3)) = 'IDV'"
                "  ) "
                "  AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = idv_files.id)"
                "  AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = idv_files.id)"
            )


def apply_scanner_save_delta_token(conn: sqlite3.Connection, payload: dict) -> None:
    """Speichert (oder aktualisiert) den Delta-Token fuer einen SharePoint-Drive.

    Erwartete Felder: ``drive_id``, ``delta_token``, ``now``.
    """
    with write_tx(conn):
        conn.execute(
            """
            INSERT INTO teams_delta_tokens (drive_id, delta_token, updated_at)
            VALUES (:drive_id, :delta_token, :now)
            ON CONFLICT(drive_id) DO UPDATE
                SET delta_token = excluded.delta_token,
                    updated_at  = excluded.updated_at
            """,
            {
                "drive_id":    payload["drive_id"],
                "delta_token": payload["delta_token"],
                "now":         payload["now"],
            },
        )


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
