"""
IDV-Register Datenbankschicht
=============================
Initialisierung, Migration und Basisfunktionen für das IDV-Register.
Wird von Scanner und Web-Frontend gemeinsam genutzt.

Schema-Änderungen werden über Alembic-Migrationen verwaltet
(siehe ``alembic/versions/``); ``init_register_db()`` ruft beim Start
``alembic upgrade head`` auf.
"""

import sys
import sqlite3
import json
import calendar
import re
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


def _alembic_config(db_path: str):
    """Erzeugt eine Alembic-Config, die auf ``db_path`` zeigt.

    Import lokal gehalten, damit Module, die ``db.get_connection`` nutzen
    (Scanner-Subprozess, Writer-Thread), nicht unnötig alembic/sqlalchemy
    laden müssen.
    """
    from alembic.config import Config

    ini_path = _resource_path("alembic.ini")
    if not ini_path.exists():
        raise FileNotFoundError(f"alembic.ini nicht gefunden: {ini_path}")

    cfg = Config(str(ini_path))
    # script_location absolut setzen: PyInstaller legt migrations/ unter
    # _MEIPASS/migrations ab, nicht am CWD. Der Ordnername ist bewusst
    # nicht ``alembic/`` – der Projektroot steht beim direkten Start von
    # run.py in sys.path[0], ein gleichnamiger lokaler Ordner würde das
    # installierte alembic-Package überschatten.
    cfg.set_main_option("script_location", str(_resource_path("migrations")))
    # SQLite-URL für den aktuellen DB-Pfad – der alembic.ini-Default
    # (instance/idvault.db) ist nur ein Platzhalter für Offline-Aufrufe.
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def init_register_db(db_path: str) -> sqlite3.Connection:
    """Initialisiert die Datenbank über Alembic und liefert eine Connection.

    - Legt das Verzeichnis für die SQLite-Datei bei Bedarf an.
    - Fährt ``alembic upgrade head`` (idempotent).
    - Gleicht Schema-Additionen ohne eigene Migration an (Pre-Release).
    - Gibt eine Anwendungs-Connection (mit den Standard-PRAGMAs) zurück.
    """
    from alembic import command

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(_alembic_config(db_path), "head")
    conn = get_connection(db_path)
    _ensure_runtime_schema(conn)
    return conn


_RUNTIME_SCHEMA_DDL = (
    # Akzeptanz „kein Zell-/Blattschutz" – Fachverantwortlicher bestätigt
    # bewusst pro (IDV, Datei). Pre-Release ohne eigene Alembic-Revision.
    """
    CREATE TABLE IF NOT EXISTS idv_zellschutz_akzeptanz (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        idv_db_id           INTEGER NOT NULL REFERENCES idv_register(id)  ON DELETE CASCADE,
        file_id             INTEGER NOT NULL REFERENCES idv_files(id)      ON DELETE CASCADE,
        freigabe_id         INTEGER          REFERENCES idv_freigaben(id)  ON DELETE SET NULL,
        akzeptiert_von_id   INTEGER          REFERENCES persons(id),
        akzeptiert_am       TEXT NOT NULL DEFAULT (datetime('now','utc')),
        begruendung         TEXT,
        UNIQUE(idv_db_id, file_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_zellschutz_akz_idv  ON idv_zellschutz_akzeptanz(idv_db_id)",
    "CREATE INDEX IF NOT EXISTS idx_zellschutz_akz_file ON idv_zellschutz_akzeptanz(file_id)",
    # Zuordnungs-Vorschläge aus der Auto-Zuordnung (mittlere Konfidenz):
    # werden dem Owner im Self-Service zur Bestätigung/Ablehnung angezeigt.
    """
    CREATE TABLE IF NOT EXISTS idv_match_suggestions (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id              INTEGER NOT NULL REFERENCES idv_files(id)    ON DELETE CASCADE,
        idv_db_id            INTEGER NOT NULL REFERENCES idv_register(id) ON DELETE CASCADE,
        score                INTEGER NOT NULL,
        created_at           TEXT NOT NULL DEFAULT (datetime('now','utc')),
        decision             TEXT,           -- NULL=offen, 'confirmed', 'rejected'
        decided_at           TEXT,
        decided_by_person_id INTEGER         REFERENCES persons(id),
        UNIQUE(file_id, idv_db_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_match_sugg_file ON idv_match_suggestions(file_id)",
    "CREATE INDEX IF NOT EXISTS idx_match_sugg_idv  ON idv_match_suggestions(idv_db_id)",
    "CREATE INDEX IF NOT EXISTS idx_match_sugg_open ON idv_match_suggestions(decision) WHERE decision IS NULL",
    # Konfigurierbare Regeln für die Auto-Klassifizierung nach Dateiname
    # (Issue #345). Vorher: hartkodierte AH/IDV-Bulks. Jetzt: Prefix/Suffix/
    # Contains/Regex, optional pro OE, mit Reihenfolge (kleinste sort_order
    # gewinnt).
    """
    CREATE TABLE IF NOT EXISTS auto_classify_rules (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        bezeichnung   TEXT NOT NULL,
        pattern_type  TEXT NOT NULL CHECK (pattern_type IN ('prefix','suffix','contains','regex')),
        pattern       TEXT NOT NULL,
        action        TEXT NOT NULL CHECK (action IN ('Zur Registrierung','Nicht wesentlich','Ignoriert')),
        oe_id         INTEGER REFERENCES org_units(id) ON DELETE SET NULL,
        enabled       INTEGER NOT NULL DEFAULT 1,
        sort_order    INTEGER NOT NULL DEFAULT 100,
        created_at    TEXT NOT NULL DEFAULT (datetime('now','utc'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_auto_classify_rules_enabled ON auto_classify_rules(enabled, sort_order)",
)


# Default-Regeln, die bei leerem ``auto_classify_rules``-Inhalt einmalig
# eingespielt werden: deckt das bisherige hartkodierte Verhalten ab
# (Dateinamen-Tags „(IDV)" / „(AH)"). Die Reihenfolge stellt sicher, dass
# IDV Vorrang vor AH hat (kleinere sort_order = höhere Priorität).
_DEFAULT_CLASSIFY_RULES = (
    {
        "bezeichnung": "Dateinamen-Tag „(IDV)“ → Zur Registrierung",
        "pattern_type": "contains",
        "pattern": "(IDV)",
        "action": "Zur Registrierung",
        "sort_order": 100,
    },
    {
        "bezeichnung": "Dateinamen-Tag „(AH)“ → Nicht wesentlich",
        "pattern_type": "contains",
        "pattern": "(AH)",
        "action": "Nicht wesentlich",
        "sort_order": 110,
    },
)


def load_auto_classify_rules(conn: sqlite3.Connection,
                              only_enabled: bool = True) -> list[dict]:
    """Lädt die Auto-Klassifizierungs-Regeln in Prioritäts-Reihenfolge."""
    where = "WHERE enabled = 1" if only_enabled else ""
    rows = conn.execute(f"""
        SELECT id, bezeichnung, pattern_type, pattern, action, oe_id,
               enabled, sort_order
          FROM auto_classify_rules
          {where}
         ORDER BY sort_order, id
    """).fetchall()
    return [dict(r) for r in rows]


def _pattern_matches(file_name: str, pattern_type: str, pattern: str) -> bool:
    """Einzel-Match gegen einen File-Stem (ohne Extension).

    Tags wie ``(IDV)`` arbeiten case-insensitiv auf dem Stem — das entspricht
    dem bisherigen Verhalten in ``scanner/network_scanner._classify_by_filename``.
    Regex-Patterns werden unverändert auf den vollen Dateinamen angewandt,
    damit Autoren explizite Anker (``^…$``) nutzen können.
    """
    if not pattern:
        return False
    stem = Path(file_name).stem
    if pattern_type == "prefix":
        return stem.upper().startswith(pattern.upper())
    if pattern_type == "suffix":
        return stem.upper().endswith(pattern.upper())
    if pattern_type == "contains":
        return pattern.upper() in stem.upper()
    if pattern_type == "regex":
        try:
            return re.search(pattern, file_name) is not None
        except re.error:
            return False
    return False


def evaluate_classify_rules(rules: list[dict], file_name: str,
                             file_oe_id: Optional[int] = None
                             ) -> Optional[dict]:
    """Liefert die **erste** passende Regel für ``file_name`` oder None.

    ``rules`` wird vom Aufrufer einmal via ``load_auto_classify_rules`` geladen
    und dann gegen viele Dateinamen ausgewertet — die Reihenfolge bestimmt die
    Priorität. Regeln mit ``oe_id`` greifen nur, wenn ``file_oe_id`` passt;
    Regeln ohne ``oe_id`` gelten global.
    """
    if not rules:
        return None
    for r in rules:
        rule_oe = r.get("oe_id")
        if rule_oe is not None and rule_oe != file_oe_id:
            continue
        if _pattern_matches(file_name, r["pattern_type"], r["pattern"]):
            return r
    return None


def validate_regex_pattern(pattern: str) -> Optional[str]:
    """Gibt None zurück, wenn ``pattern`` eine gültige Regex ist, sonst die
    Fehlermeldung. Für das Admin-UI."""
    try:
        re.compile(pattern)
        return None
    except re.error as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# Versions-Serien-Fingerprint (Issue #359)
# ---------------------------------------------------------------------------
#
# Dritter Auto-Link-Pfad neben SHA-256-Hashdublette und Similarity-Score:
# wiederkehrende Versionen derselben Datei (Reports, Kalkulationen …) werden
# einer bereits registrierten IDV zugeschlagen, auch wenn sich der Hash je
# Ausgabe aendert.
#
# Fingerprint = ``lower(ordner) + "|" + lower(masked_stem)``, wobei
# ``masked_stem`` den Datei-Stem (ohne Extension) mit maskierten Versions-
# und Zeitstempel-Mustern enthaelt. Die Reihenfolge der Masken ist relevant
# — spezifischere Muster laufen zuerst, damit sie nicht von den generischen
# Mustern aufgefressen werden (z.B. ISO-Datum vor Jahres-Maske).
_VERSION_FP_PATTERNS: tuple = (
    # ISO-Datum 2024-06-15 — muss vor der Jahres-Maske laufen
    (re.compile(r"\d{4}-\d{2}-\d{2}"),          "####-##-##"),
    # Jahr 20xx (eigenstaendig, nicht Teil einer laengeren Ziffernfolge)
    (re.compile(r"(?<!\d)20\d{2}(?!\d)"),       "####"),
    # Quartal Q1..Q4 (case-insensitiv)
    (re.compile(r"(?i)Q[1-4](?!\d)"),           "Q#"),
    # Versions-Suffix v1, v10, v123 (case-insensitiv)
    (re.compile(r"(?i)v\d+(?!\d)"),             "v#"),
    # Dreistellige Sequenz mit Unterstrich: _001, _042, _123
    (re.compile(r"_\d{3}(?!\d)"),               "_###"),
    # Monat 01..12 als eigenstaendiges Token (von Nicht-Ziffern umschlossen)
    (re.compile(r"(?<!\d)(0[1-9]|1[0-2])(?!\d)"), "##"),
)


def compute_version_fingerprint(full_path: Optional[str],
                                 file_name: Optional[str]) -> Optional[str]:
    """Berechnet den Versions-Serien-Fingerprint einer Datei.

    Der Fingerprint identifiziert wiederkehrende Versionen derselben Datei
    in einem Ordner (quartals-/monatsweise abgelegte Reports o.ae.) und wird
    neben der SHA-256-Hashdublette und dem Similarity-Score als dritter Auto-
    Link-Pfad in ``auto_zuordnen`` verwendet.

    Algorithmus (siehe Issue #359):
      1. Ordner aus ``full_path`` extrahieren (Separator-tolerant, lowercase).
      2. Stem = ``file_name`` ohne letzte Extension.
      3. Versions-/Zeitstempel-Muster im Stem maskieren:
         - ISO-Datum  ``\\d{4}-\\d{2}-\\d{2}``    -> ``####-##-##``
         - Jahr       ``20\\d{2}``                -> ``####``
         - Quartal    ``Q[1-4]``                 -> ``Q#``
         - Version    ``v\\d+``                   -> ``v#``
         - Sequenz    ``_\\d{3}``                 -> ``_###``
         - Monat      ``01..12`` (eigenstaendig)  -> ``##``
      4. Fingerprint = ``lower(folder) + "|" + lower(masked_stem)``.

    Liefert ``None``, wenn nach der Maskierung weniger als 3 nicht-maskierte
    Zeichen im Stem uebrig bleiben (sonst wuerde der Fingerprint beliebige
    Versions-Dateien desselben Ordners kollabieren lassen — im Extremfall
    wuerde dadurch jede Versionsdatei derselben IDV zugeordnet).

    Case-insensitiv, aber **pfadsensitiv**: dieselbe Serie in einem anderen
    Ordner ist absichtlich ein anderer Fingerprint. Eine Umstrukturierung
    wird damit nicht automatisch nachgezogen.
    """
    if not full_path or not file_name:
        return None
    # 1. Ordner bestimmen (separator-tolerant: full_path kann UNC-/Windows-
    # oder POSIX-Pfad enthalten).
    idx = max(full_path.rfind("\\"), full_path.rfind("/"))
    folder = full_path[:idx] if idx >= 0 else ""
    folder = folder.rstrip("\\/")
    # 2. Stem aus dem Dateinamen (nicht aus dem Pfad — der Scanner uebergibt
    # bewusst das Datei-Feld, damit Sonderfaelle wie Pfade ohne Extension den
    # Stem nicht verfaelschen).
    dot = file_name.rfind(".")
    stem = file_name[:dot] if dot > 0 else file_name
    if not stem.strip():
        return None
    # 3. Masken in fester Reihenfolge anwenden
    masked = stem
    for rx, repl in _VERSION_FP_PATTERNS:
        masked = rx.sub(repl, masked)
    # 4. Fallback-Guard: weniger als 3 nicht-Masken-Zeichen -> zu unspezifisch
    non_mask = masked.replace("#", "")
    if len(non_mask.strip()) < 3:
        return None
    return folder.lower() + "|" + masked.lower()


def _ensure_version_fingerprint_column(conn: sqlite3.Connection) -> None:
    """Legt ``idv_files.version_fingerprint`` + Index runtime an und backfillt
    Bestandsdateien mit leerem Fingerprint.

    SQLite unterstuetzt kein ``ADD COLUMN IF NOT EXISTS`` — die Existenz wird
    deshalb ueber ``PRAGMA table_info`` geprueft. Der Backfill laeuft einmalig
    pro App-Start fuer alle Eintraege, deren Fingerprint noch NULL ist (neu
    angelegte Spalte, importierte DB oder Dateien, deren Fingerprint beim
    fruehen Scan aus irgendeinem Grund nicht gesetzt wurde).
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(idv_files)")}
    if "version_fingerprint" not in cols:
        conn.execute("ALTER TABLE idv_files ADD COLUMN version_fingerprint TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_files_version_fp "
        "ON idv_files(version_fingerprint)"
    )
    # Backfill: Bestandsdateien mit leerem Fingerprint nachtraeglich befuellen.
    # Laeuft nur ueber die ohnehin wenigen NULL-Zeilen — nach dem ersten Lauf
    # bleibt das SELECT leer.
    rows = conn.execute(
        "SELECT id, full_path, file_name FROM idv_files "
        "WHERE version_fingerprint IS NULL"
    ).fetchall()
    for r in rows:
        fp = compute_version_fingerprint(r["full_path"], r["file_name"])
        if fp is None:
            continue
        conn.execute(
            "UPDATE idv_files SET version_fingerprint = ? WHERE id = ?",
            (fp, r["id"]),
        )


def _ensure_runtime_schema(conn: sqlite3.Connection) -> None:
    """Idempotente Schema-Ergänzungen, die nach dem Alembic-Upgrade laufen.

    Nur für Pre-Release-Ergänzungen, bei denen keine eigene Migration
    geschrieben wird. Jedes Statement muss ``IF NOT EXISTS`` verwenden,
    damit frische DBs (vom Initial-Schema aufgesetzt) nicht in Konflikt
    geraten.
    """
    for stmt in _RUNTIME_SCHEMA_DDL:
        conn.execute(stmt)

    _ensure_version_fingerprint_column(conn)

    # Default-Regeln für die Auto-Klassifizierung nachrüsten: nur wenn die
    # Tabelle noch leer ist (echter Erstlauf); bestehende Installationen,
    # die die Regeln bereits bearbeitet oder entfernt haben, bleiben unberührt.
    count = conn.execute(
        "SELECT COUNT(*) FROM auto_classify_rules"
    ).fetchone()[0]
    if count == 0:
        for r in _DEFAULT_CLASSIFY_RULES:
            conn.execute(
                "INSERT INTO auto_classify_rules "
                "(bezeichnung, pattern_type, pattern, action, sort_order) "
                "VALUES (?,?,?,?,?)",
                (r["bezeichnung"], r["pattern_type"], r["pattern"],
                 r["action"], r["sort_order"]),
            )

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
               bearbeiter_name: str = "",
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
        "datenschutz_kategorie":     data.get("datenschutz_kategorie", "keine"),
        "nutzungsfrequenz":          data.get("nutzungsfrequenz"),
        "nutzeranzahl":              data.get("nutzeranzahl"),
        "produktiv_seit":            data.get("produktiv_seit"),
        "dokumentation_vorhanden":   int(data.get("dokumentation_vorhanden", 0)),
        "dokumentation_pfad":        data.get("dokumentation_pfad"),
        "testkonzept_vorhanden":     int(data.get("testkonzept_vorhanden", 0)),
        "versionskontrolle":         int(data.get("versionskontrolle", 0)),
        "anwenderdokumentation":     int(data.get("anwenderdokumentation", 0)),
        "datenschutz_beachtet":      int(data.get("datenschutz_beachtet", 0)),
        "zellschutz_formeln":        int(data.get("zellschutz_formeln", 0)),
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
            INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name)
            VALUES (?, 'erstellt', ?, ?, ?)
        """, (new_id, f"IDV {idv_id} erstellt", erfasser_id, bearbeiter_name or None))

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
               bearbeiter_name: str = "",
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
        "bezeichnung", "version", "idv_typ", "entwicklungsart",
        "fachverantwortlicher_id", "idv_entwickler_id", "idv_koordinator_id",
        "stellvertreter_id", "org_unit_id", "gp_id",
        "naechste_pruefung", "pruefintervall_monate", "teststatus",
        "anwenderdokumentation", "datenschutz_beachtet", "zellschutz_formeln",
        "plattform_id", "nutzungsfrequenz",
    ]

    def _norm(v):
        if v is None or v == "":
            return None
        return v

    changes = {}
    for f in tracked_fields:
        if f in data and _norm(data[f]) != _norm(old[f]):
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
        "datenschutz_kategorie",
        "nutzungsfrequenz", "nutzeranzahl", "produktiv_seit",
        "dokumentation_vorhanden", "dokumentation_pfad",
        "testkonzept_vorhanden", "versionskontrolle",
        "anwenderdokumentation", "datenschutz_beachtet", "zellschutz_formeln",
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
                INSERT INTO idv_history (idv_id, aktion, geaenderte_felder, durchgefuehrt_von_id, bearbeiter_name)
                VALUES (?, 'geaendert', ?, ?, ?)
            """, (idv_db_id, json.dumps(changes, ensure_ascii=False), geaendert_von_id, bearbeiter_name or None))

    if commit:
        with write_tx(conn):
            _body()
    else:
        _body()
    return True


def change_status(conn: sqlite3.Connection, idv_db_id: int,
                  new_status: str, kommentar: str = "",
                  geaendert_von_id: Optional[int] = None,
                  bearbeiter_name: str = ""):
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
            INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name)
            VALUES (?, 'status_geaendert', ?, ?, ?)
        """, (idv_db_id, f"Status → {new_status}. {kommentar}", geaendert_von_id, bearbeiter_name or None))


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
        "freigegeben":          scalar("SELECT COUNT(*) FROM idv_register WHERE status IN ('Freigegeben','Freigegeben mit Auflagen')"),
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


# ---------------------------------------------------------------------------
# Vollständigkeits-Score (Issue #348)
# ---------------------------------------------------------------------------
#
# Gewichtete 0–100-Bewertung je IDV. Die Kernpflichtfelder aus
# ``v_unvollstaendige_idvs`` (Fachverantwortlicher, Geschäftsprozess, Typ,
# Wesentlichkeits-Begründung) decken zusammen 60 Punkte ab – dieselbe
# Basis, auf der die Qualitätssicherungs-View aufsetzt. Die übrigen
# 40 Punkte belohnen weitere Pflege (Kurzbeschreibung, Entwickler,
# Org.-Einheit, Plattform, Nutzungsfrequenz, Datenschutz-Kategorie),
# sodass 100 % nur bei echter Nachpflege erreicht werden.
COMPLETENESS_WEIGHTS = {
    "bezeichnung":                 5,
    "fachverantwortlicher_id":    20,
    "idv_typ_klassifiziert":      15,
    "geschaeftsprozess":          10,
    "wesentlichkeit_begruendet":  15,
    "kurzbeschreibung":            5,
    "idv_entwickler_id":           5,
    "org_unit_id":                 5,
    "plattform_id":                5,
    "nutzungsfrequenz":            5,
    "datenschutz_kategorie":       5,
    "naechste_pruefung":           5,
}
assert sum(COMPLETENESS_WEIGHTS.values()) == 100  # Invariante für die Skala


def idv_incomplete_owners(conn: sqlite3.Connection, limit: int = 10) -> list:
    """Aggregiert unvollständige IDVs pro Verantwortlichem (Fachverantwortlicher).

    Stützt sich auf ``v_unvollstaendige_idvs``, damit das Dashboard und
    die Detailansicht dieselbe Definition von „unvollständig" teilen.
    """
    rows = conn.execute("""
        SELECT p.id          AS person_id,
               p.nachname    AS nachname,
               p.vorname     AS vorname,
               p.email       AS email,
               COUNT(*)      AS anzahl
          FROM v_unvollstaendige_idvs v
          JOIN idv_register r ON r.idv_id = v.idv_id
          JOIN persons p      ON p.id     = r.fachverantwortlicher_id
         WHERE p.aktiv = 1
         GROUP BY p.id, p.nachname, p.vorname, p.email
         ORDER BY anzahl DESC, nachname ASC
         LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def idv_completeness_score(conn: sqlite3.Connection, idv_db_id: int) -> dict:
    """Liefert den Vollständigkeits-Score (0–100) einer IDV zusammen mit
    einer Liste fehlender Pflegepunkte.

    Rückgabe::

        {"score": 0..100, "missing": ["Fachverantwortlicher", ...]}

    Die View ``v_unvollstaendige_idvs`` flaggt eine IDV ganz als
    „unvollständig", sobald eines der Kern-Pflichtfelder fehlt. Diese
    Funktion gibt stattdessen einen kontinuierlichen Score zurück, damit
    eine Schnell-Anlage (Issue #348) bei Teil-Erfassung einen Teil-Score
    bekommen kann – und der Nachpflegefortschritt sichtbar wird.
    """
    row = conn.execute("""
        SELECT r.id, r.bezeichnung, r.fachverantwortlicher_id, r.idv_typ,
               r.gp_id, r.gp_freitext, r.kurzbeschreibung,
               r.idv_entwickler_id, r.org_unit_id, r.plattform_id,
               r.nutzungsfrequenz, r.datenschutz_kategorie,
               r.naechste_pruefung
        FROM idv_register r WHERE r.id = ?
    """, (idv_db_id,)).fetchone()
    if row is None:
        return {"score": 0, "missing": ["IDV nicht gefunden"]}

    # Wesentlichkeits-Begründungen: fehlt die Begründung bei einem
    # erfüllten pflichtigen Kriterium, gilt das Feld als offen
    # (konsistent zur v_unvollstaendige_idvs-Logik).
    fehlende_begr = conn.execute("""
        SELECT 1 FROM idv_wesentlichkeit iw
        JOIN wesentlichkeitskriterien k ON k.id = iw.kriterium_id
        WHERE iw.idv_db_id = ?
          AND iw.erfuellt = 1
          AND k.begruendung_pflicht = 1
          AND (iw.begruendung IS NULL OR TRIM(iw.begruendung) = '')
        LIMIT 1
    """, (idv_db_id,)).fetchone()

    checks = {
        "bezeichnung":                 bool(row["bezeichnung"] and str(row["bezeichnung"]).strip()),
        "fachverantwortlicher_id":     row["fachverantwortlicher_id"] is not None,
        "idv_typ_klassifiziert":       (row["idv_typ"] or "") != "unklassifiziert" and bool(row["idv_typ"]),
        "geschaeftsprozess":           row["gp_id"] is not None or bool(row["gp_freitext"]),
        "wesentlichkeit_begruendet":   fehlende_begr is None,
        "kurzbeschreibung":            bool(row["kurzbeschreibung"] and str(row["kurzbeschreibung"]).strip()),
        "idv_entwickler_id":           row["idv_entwickler_id"] is not None,
        "org_unit_id":                 row["org_unit_id"] is not None,
        "plattform_id":                row["plattform_id"] is not None,
        "nutzungsfrequenz":            bool(row["nutzungsfrequenz"]),
        "datenschutz_kategorie":       bool(row["datenschutz_kategorie"]) and row["datenschutz_kategorie"] != "keine",
        "naechste_pruefung":           bool(row["naechste_pruefung"]),
    }
    labels = {
        "bezeichnung":                 "Bezeichnung",
        "fachverantwortlicher_id":     "Fachverantwortlicher",
        "idv_typ_klassifiziert":       "Typ (klassifiziert)",
        "geschaeftsprozess":           "Geschäftsprozess",
        "wesentlichkeit_begruendet":   "Wesentlichkeits-Begründung",
        "kurzbeschreibung":            "Kurzbeschreibung",
        "idv_entwickler_id":           "Entwickler",
        "org_unit_id":                 "Organisations-Einheit",
        "plattform_id":                "Plattform",
        "nutzungsfrequenz":            "Nutzungsfrequenz",
        "datenschutz_kategorie":       "Datenschutz-Kategorie",
        "naechste_pruefung":           "Nächste Prüfung",
    }
    score = 0
    missing = []
    for key, weight in COMPLETENESS_WEIGHTS.items():
        if checks.get(key):
            score += weight
        else:
            missing.append(labels[key])
    # Kappen auf 100 (Invariante, aber defensiv falls Gewichte mal driften)
    return {"score": min(100, max(0, score)), "missing": missing}


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
    #   dokumentation_vorhanden, testkonzept_vorhanden,
    #   anwenderdokumentation, datenschutz_beachtet, zellschutz_formeln,
    #   datenschutz_kategorie,
    #   gp_nummer, org_unit, fachv_kuerzel, entw_kuerzel, koord_kuerzel,
    #   plattform_bezeichnung
    idv_rows = [
        ("IDV-2024-001", "GuV-Auswertung Excel-Makro",
         "Monatliche GuV-Berechnung mit VBA-Makro – rechnungslegungsrelevant.",
         "Excel-Makro", "idv", "Freigegeben", 12,
         "2025-10-15", "2026-10-15", "2022-03-01",
         "monatlich", 5, 1, 1, 0, 0, 0, "keine",
         "GP-BWK-001", "Betriebswirtschaft/Controlling", "FV-BWK", "IDV-ENT", "IDV-KO",
         "Microsoft Excel"),
        ("IDV-2024-002", "Sicherheiten-Bewertung Access-DB",
         "Access-Datenbank zur Bewertung von Kreditsicherheiten.",
         "Access-Datenbank", "idv", "Freigegeben", 12,
         "2025-11-20", "2026-11-20", "2023-06-01",
         "wöchentlich", 8, 1, 1, 0, 0, 0, "allgemein",
         "GP-KRE-002", "Kreditabteilung", "FV-KRE", "IDV-ENT", "IDV-KO",
         "Microsoft Access"),
        ("IDV-2024-003", "Reporting-Arbeitshilfe Vorstand",
         "Excel-Arbeitshilfe zur Aufbereitung der Monatsberichte für den Vorstand.",
         "Excel-Tabelle", "arbeitshilfe", "Entwurf", 24,
         None, "2027-04-01", "2024-02-01",
         "monatlich", 3, 0, 0, 0, 0, 0, "keine",
         "GP-BWK-002", "Betriebswirtschaft/Controlling", "FV-BWK", "FV-BWK", "IDV-KO",
         "Microsoft Excel"),
        ("IDV-2024-004", "EBA COREP Datenlieferung",
         "Python-Skript zur Aufbereitung der COREP-Meldedaten an die Bundesbank.",
         "Python-Skript", "idv", "Freigegeben", 6,
         "2025-12-05", "2026-06-05", "2024-01-15",
         "quartalsweise", 2, 1, 1, 0, 0, 0, "keine",
         "GP-MEL-001", "Meldewesen", "FV-MEL", "IDV-ENT", "IDV-KO",
         "Python 3.11"),
        ("IDV-2025-001", "FINREP-Meldewesen",
         "Zentrales Python-Framework für FINREP-Meldungen, IT-Entwicklung.",
         "Python-Skript", "eigenprogrammierung", "In Prüfung", 12,
         None, "2026-07-01", "2025-02-10",
         "quartalsweise", 4, 1, 1, 0, 0, 0, "keine",
         "GP-MEL-002", "Meldewesen", "FV-MEL", "IDV-ENT", "IDV-KO",
         "Python 3.11"),
        ("IDV-2025-002", "Zinsrisiko-Modell",
         "Excel-Modell zur Berechnung des Barwertrisikos (Zinsschock).",
         "Excel-Modell", "idv", "Freigegeben", 12,
         "2026-01-20", "2027-01-20", "2023-09-01",
         "monatlich", 3, 1, 1, 0, 0, 0, "keine",
         "GP-RIS-001", "Risikocontrolling", "FV-RIS", "FV-RIS", "IDV-KO",
         "Microsoft Excel"),
        ("IDV-2025-003", "Stresstest-Szenarien",
         "Excel-Arbeitshilfe zur Zusammenstellung von Stresstest-Szenarien.",
         "Excel-Tabelle", "arbeitshilfe", "Entwurf", 24,
         None, "2027-05-01", "2025-05-20",
         "jährlich", 2, 0, 0, 0, 0, 0, "keine",
         "GP-RIS-002", "Risikocontrolling", "FV-RIS", "FV-RIS", "IDV-KO",
         "Microsoft Excel"),
        ("IDV-2025-004", "Firmenkunden-Score",
         "SQL-basiertes Scoring für Firmenkundenkredite, zentrale IT-Entwicklung.",
         "SQL-Skript", "eigenprogrammierung", "In Prüfung", 12,
         None, "2026-08-15", "2025-08-01",
         "täglich", 12, 1, 0, 0, 0, 0, "allgemein",
         "GP-KRE-001", "Kreditabteilung", "FV-KRE", "IDV-ENT", "IDV-KO",
         "Shell-Skripte"),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO idv_register ("
        " idv_id, bezeichnung, kurzbeschreibung, idv_typ, entwicklungsart,"
        " status, pruefintervall_monate, letzte_pruefung, naechste_pruefung,"
        " produktiv_seit, nutzungsfrequenz, nutzeranzahl,"
        " dokumentation_vorhanden, testkonzept_vorhanden,"
        " anwenderdokumentation, datenschutz_beachtet, zellschutz_formeln,"
        " datenschutz_kategorie,"
        " gp_id, org_unit_id,"
        " fachverantwortlicher_id, idv_entwickler_id, idv_koordinator_id,"
        " plattform_id"
        ") VALUES ("
        " ?,?,?,?,?,"
        " ?,?,?,?,"
        " ?,?,?,"
        " ?,?,"
        " ?,?,?,"
        " ?,"
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
             "IDV-KO", "2024-06-28", "Freigegeben", "Freigabe nach Nachbesserung.",
             "IT-SI", "2024-06-28", "Freigegeben", "Keine sicherheitskritischen Funde.",
             "Freigegeben", "2024-06-28"),
            ("IDV-2024-002", "Erstfreigabe", "FV-KRE", "2024-07-01",
             "IDV-KO", "2024-10-02", "Freigegeben", "Berechtigungskonzept umgesetzt.",
             "IT-SI", "2024-10-02", "Freigegeben", "Zugriffsschutz geprüft.",
             "Freigegeben", "2024-10-02"),
            ("IDV-2024-004", "Erstfreigabe", "FV-MEL", "2024-05-01",
             "IDV-KO", "2024-06-15", "Freigegeben", "Freigabe mit Auflage (Git-Einführung).",
             "IT-SI", "2024-06-15", "Freigegeben", "DORA-Anforderungen erfüllt.",
             "Freigegeben", "2024-06-15"),
            ("IDV-2025-002", "Erstfreigabe", "FV-RIS", "2025-11-15",
             "IDV-KO", "2026-01-22", "Freigegeben", "Zinsrisiko-Modell validiert.",
             "IT-SI", "2026-01-22", "Freigegeben", "Keine IT-sicherheitsrelevanten Befunde.",
             "Freigegeben", "2026-01-22"),
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
    # Versions-Serien-Fingerprint (Issue #359). Wird vom Scanner via
    # ``compute_version_fingerprint`` befuellt und beim INSERT/UPDATE/MOVE
    # mitgefuehrt.
    "version_fingerprint",
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

    ``archived_files`` wird per ``MAX`` mit dem vorhandenen Wert zusammen-
    gefuehrt: die Archivierung nicht mehr gesehener Dateien laeuft inzwischen
    als eigenes OP_ARCHIVE_UNSEEN-Event *vor* OP_END_RUN und hat die Spalte
    bereits korrekt gesetzt. Der Scanner kann die endgueltige Zahl nicht mehr
    selbst ermitteln (er liest nur – die Webapp schreibt) und sendet daher
    ``archived=0``. ``MAX`` verhindert, dass dieser Wert den bereits vom
    Archive-Handler gesetzten Zaehler ueberschreibt.
    """
    with write_tx(conn):
        conn.execute(
            """
            UPDATE scan_runs SET
                finished_at = ?, total_files = ?, new_files = ?,
                changed_files = ?, moved_files = ?, restored_files = ?,
                archived_files = MAX(COALESCE(archived_files, 0), ?),
                errors = ?, scan_status = ?
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
                    version_fingerprint = :version_fingerprint,
                    source = :source, sharepoint_item_id = :sharepoint_item_id,
                    last_seen_at = :now, last_scan_run_id = :run_id
                WHERE id = :id
                """,
                {
                    "full_path":           data["full_path"],
                    "share_root":          data.get("share_root"),
                    "relative_path":       data.get("relative_path"),
                    "version_fingerprint": data.get("version_fingerprint"),
                    "source":              source,
                    "sharepoint_item_id":  sp_item_id,
                    "now":                 now,
                    "run_id":              scan_run_id,
                    "id":                  file_id,
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


def apply_scanner_upsert_file_batch(conn: sqlite3.Connection, payloads: list) -> None:
    """Verarbeitet einen Batch von upsert_file-Events in einer einzigen Transaktion.

    Statt jede Datei einzeln zu committen, werden bis zu UPSERT_BATCH_SIZE
    Eintraege gebuendelt. INSERT-Payloads landen per Einzelaufruf (fuer
    ``lastrowid``), die History-Zeilen dann per ``executemany``. UPDATE- und
    MOVE-Payloads nutzen ``executemany`` sowohl fuer idv_files als auch fuer
    idv_file_history.
    """
    if not payloads:
        return

    inserts = [p for p in payloads if p.get("action") == "insert"]
    moves   = [p for p in payloads if p.get("action") == "move"]
    updates = [p for p in payloads if p.get("action") not in ("insert", "move")]

    with write_tx(conn):
        if inserts:
            hist_rows: list = []
            for p in inserts:
                data   = p["data"]
                now    = p["now"]
                run_id = p["scan_run_id"]
                source = data.get("source") or "filesystem"
                row = {
                    **{col: data.get(col) for col in _IDV_FILES_COLUMNS},
                    "first_seen_at":      now,
                    "last_seen_at":       now,
                    "last_scan_run_id":   run_id,
                    "source":             source,
                    "sharepoint_item_id": data.get("sharepoint_item_id"),
                }
                cols = ", ".join(row.keys()) + ", status"
                plh  = ", ".join(f":{k}" for k in row.keys()) + ", 'active'"
                cur  = conn.execute(f"INSERT INTO idv_files ({cols}) VALUES ({plh})", row)
                hist_rows.append((cur.lastrowid, run_id, data.get("file_hash"), now))
            conn.executemany(
                "INSERT INTO idv_file_history "
                "(file_id, scan_run_id, change_type, new_hash, changed_at) "
                "VALUES (?, ?, 'new', ?, ?)",
                hist_rows,
            )

        if moves:
            move_hist: list = []
            for p in moves:
                file_id = p["file_id"]
                data    = p["data"]
                now     = p["now"]
                run_id  = p["scan_run_id"]
                source  = data.get("source") or "filesystem"
                conn.execute(
                    "UPDATE idv_files SET "
                    "full_path = :fp, share_root = :sr, relative_path = :rp, "
                    "version_fingerprint = :vfp, "
                    "source = :src, sharepoint_item_id = :spid, "
                    "last_seen_at = :now, last_scan_run_id = :rid "
                    "WHERE id = :id",
                    {
                        "fp":   data["full_path"],
                        "sr":   data.get("share_root"),
                        "rp":   data.get("relative_path"),
                        "vfp":  data.get("version_fingerprint"),
                        "src":  source,
                        "spid": data.get("sharepoint_item_id"),
                        "now":  now,
                        "rid":  run_id,
                        "id":   file_id,
                    },
                )
                move_hist.append((
                    file_id, run_id,
                    data.get("file_hash"), data.get("file_hash"),
                    now, p.get("details"),
                ))
            conn.executemany(
                "INSERT INTO idv_file_history "
                "(file_id, scan_run_id, change_type, old_hash, new_hash, changed_at, details) "
                "VALUES (?, ?, 'moved', ?, ?, ?, ?)",
                move_hist,
            )

        if updates:
            set_sql   = ", ".join(f"{col} = :{col}" for col in _IDV_FILES_COLUMNS)
            upd_rows:  list = []
            hist_rows2: list = []
            for p in updates:
                data        = p["data"]
                now         = p["now"]
                run_id      = p["scan_run_id"]
                file_id     = p["file_id"]
                change_type = p.get("change_type") or p["action"]
                source      = data.get("source") or "filesystem"
                row = {col: data.get(col) for col in _IDV_FILES_COLUMNS}
                row.update({
                    "now":                now,
                    "run_id":             run_id,
                    "id":                 file_id,
                    "source":             source,
                    "sharepoint_item_id": data.get("sharepoint_item_id"),
                })
                upd_rows.append(row)
                hist_rows2.append((
                    file_id, run_id, change_type,
                    p.get("old_hash"), data.get("file_hash"), now,
                ))
            conn.executemany(
                f"UPDATE idv_files SET {set_sql}, "
                "source = :source, sharepoint_item_id = :sharepoint_item_id, "
                "last_seen_at = :now, last_scan_run_id = :run_id, status = 'active' "
                "WHERE id = :id",
                upd_rows,
            )
            conn.executemany(
                "INSERT INTO idv_file_history "
                "(file_id, scan_run_id, change_type, old_hash, new_hash, changed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                hist_rows2,
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


def apply_scanner_archive_unseen(conn: sqlite3.Connection, payload: dict) -> None:
    """Archiviert alle aktiven idv_files, die im aktuellen Scan-Lauf nicht mehr
    gesehen wurden. Die Auswahl erfolgt auf der Writer-Connection, damit sie
    alle zuvor in der Queue stehenden OP_UPSERT_FILE-Schreibvorgaenge bereits
    beruecksichtigt (verhindert stale-read-Race: der Scanner nutzt eine eigene
    Reader-Connection und wuerde sonst gerade aktualisierte Dateien faelsch-
    licherweise als 'nicht gesehen' einstufen).

    Erwartete Felder:
      * ``scan_run_id`` (erforderlich)
      * ``now`` (erforderlich) – ISO-Zeitstempel
      * ``scan_since`` (optional) – nur Dateien mit ``modified_at >= scan_since``
      * ``scan_paths`` (optional, Liste[str]) – nur Dateien unterhalb dieser
        Pfade werden archiviert (bereits gemappt auf DB-Konventionen)
    """
    scan_run_id = payload["scan_run_id"]
    now         = payload["now"]
    scan_since  = payload.get("scan_since")
    scan_paths  = payload.get("scan_paths") or []

    conditions = ["status = 'active'", "last_scan_run_id != ?"]
    params: list = [scan_run_id]
    if scan_since:
        conditions.append("modified_at >= ?")
        params.append(scan_since)
    if scan_paths:
        path_conds = " OR ".join("full_path LIKE ?" for _ in scan_paths)
        conditions.append(f"({path_conds})")
        for sp in scan_paths:
            params.append(sp.rstrip("/\\") + "%")

    with write_tx(conn):
        rows = conn.execute(
            f"SELECT id FROM idv_files WHERE {' AND '.join(conditions)}",
            params,
        ).fetchall()
        file_ids = [r["id"] for r in rows]
        if file_ids:
            placeholders = ",".join("?" * len(file_ids))
            conn.execute(
                f"UPDATE idv_files SET status = 'archiviert', last_seen_at = ? "
                f"WHERE id IN ({placeholders})",
                [now] + file_ids,
            )
            conn.executemany(
                "INSERT INTO idv_file_history "
                "(file_id, scan_run_id, change_type, changed_at) "
                "VALUES (?, ?, 'archiviert', ?)",
                [(fid, scan_run_id, now) for fid in file_ids],
            )
        # Zaehler in scan_runs direkt setzen – OP_END_RUN merged per MAX.
        conn.execute(
            "UPDATE scan_runs SET archived_files = ? WHERE id = ?",
            (len(file_ids), scan_run_id),
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
        elif kind in ("auto_classify_bulk_ah", "auto_classify_bulk_idv"):
            # Pre-#345: hartkodierte AH/IDV-Bulks. Heute durch die konfigurierbaren
            # Regeln in ``auto_classify_rules`` abgedeckt — die bulk-Variante
            # (``auto_classify_rules_bulk``) läuft eine Ebene tiefer. Die alten
            # Kinds bleiben als No-Op erhalten, damit ältere Scanner-Versionen
            # kompatibel emittieren können.
            return
        elif kind == "auto_classify_rules_bulk":
            # Wendet alle aktiven Regeln aus ``auto_classify_rules`` auf alle
            # noch nicht bearbeiteten Funde an. Die erste passende Regel gewinnt
            # pro Datei (Sortier-Reihenfolge). Audit-Einträge verweisen auf die
            # Regel-ID, damit im Nachhinein nachvollziehbar bleibt, welche Regel
            # welche Klassifizierung ausgelöst hat.
            rules = load_auto_classify_rules(conn, only_enabled=True)
            if not rules:
                return

            # Unbearbeitete Funde einmal laden (klein genug, ein Scanlauf
            # generiert typischerweise < 100k Funde). Owner→OE wird joint.
            rows = conn.execute("""
                SELECT f.id, f.file_name, p.org_unit_id
                  FROM idv_files f
                  LEFT JOIN persons p
                         ON LOWER(p.user_id) = LOWER(f.file_owner)
                         OR LOWER(p.kuerzel) = LOWER(f.file_owner)
                         OR LOWER(p.ad_name) = LOWER(f.file_owner)
                 WHERE f.status = 'active'
                   AND f.bearbeitungsstatus = 'Neu'
                   AND NOT EXISTS (SELECT 1 FROM idv_register  r WHERE r.file_id = f.id)
                   AND NOT EXISTS (SELECT 1 FROM idv_file_links l WHERE l.file_id = f.id)
            """).fetchall()

            # ``idv_file_history.scan_run_id`` ist NOT NULL (siehe schema.sql).
            # Wir hängen die Audit-Einträge an den aktuell letzten Scan-Lauf —
            # das entspricht dem Zeitpunkt, an dem der Scanner den Bulk-Apply
            # emittiert hat. Ohne Scans gibt es noch keine Funde → Sentinel 0.
            scan_run = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM scan_runs"
            ).fetchone()[0]

            for row in rows:
                hit = evaluate_classify_rules(
                    rules, row["file_name"] or "", row["org_unit_id"],
                )
                if hit is None:
                    continue
                conn.execute(
                    "UPDATE idv_files SET bearbeitungsstatus = ? "
                    "WHERE id = ? AND bearbeitungsstatus = 'Neu'",
                    (hit["action"], row["id"]),
                )
                details = json.dumps({
                    "rule_id":      hit["id"],
                    "bezeichnung":  hit["bezeichnung"],
                    "pattern_type": hit["pattern_type"],
                    "pattern":      hit["pattern"],
                    "action":       hit["action"],
                }, ensure_ascii=False)
                conn.execute(
                    "INSERT INTO idv_file_history "
                    "(file_id, scan_run_id, change_type, changed_at, details) "
                    "VALUES (?, ?, 'auto_classified_rule', datetime('now','utc'), ?)",
                    (row["id"], scan_run, details),
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
