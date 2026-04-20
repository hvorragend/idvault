"""
Single-Writer-Thread fuer SQLite.

In der Webapp darf es nur *einen* Thread geben, der aktiv Schreibvorgaenge
auf die SQLite-Datei ausfuehrt. Alle anderen Komponenten (Request-Handler,
Daemon-Threads, Scheduler, Scanner-stdout-Consumer) schicken ihre Writes
per `submit(...)` an diesen Thread und holen sich das Ergebnis ueber ein
`concurrent.futures.Future` ab (falls `wait=True`).

Vorteile:
  - SQLite bekommt pro Webapp-Prozess nur eine einzige Writer-Connection →
    keine `database is locked`-Rennen mehr zwischen Hintergrund-Threads.
  - BEGIN IMMEDIATE + 60 s busy_timeout gilt weiterhin (siehe db_pragmas
    / db_write_tx) — der Scanner-Subprozess als einziger externer Writer
    wird durch die Reihenfolge der Job-Queue deterministisch serialisiert.

Reader bleiben per-Request (WAL erlaubt unbegrenzte Reader). Der Writer-
Thread oeffnet exakt eine Connection beim Start, erkennt Crashes ueber
einen Watchdog und loggt `CRITICAL`, bevor er neu startet.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


log = logging.getLogger(__name__)


@dataclass
class _Job:
    func: Callable[[sqlite3.Connection], Any]
    future: Future
    submitted_at: float = field(default_factory=time.monotonic)


_SENTINEL = object()


class DbWriter:
    """Dedizierter Writer-Thread mit einer eigenen SQLite-Connection."""

    def __init__(self, db_path: str, *, queue_max: int = 10_000,
                 apply_pragmas: Optional[Callable[[sqlite3.Connection], None]] = None):
        self._db_path = db_path
        self._queue: queue.Queue = queue.Queue(maxsize=queue_max)
        self._apply_pragmas = apply_pragmas
        self._thread: Optional[threading.Thread] = None
        self._tid: Optional[int] = None
        self._stop_flag = threading.Event()
        self._started = threading.Event()
        self._conn: Optional[sqlite3.Connection] = None

        # Telemetrie (lock-frei genug fuer diag-Endpoint)
        self.jobs_processed = 0
        self.retries = 0
        self.rollbacks = 0
        self.last_write_ts: Optional[float] = None
        self.restart_count = 0

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._started.clear()
        self._thread = threading.Thread(
            target=self._run, name="idvault-db-writer", daemon=True
        )
        self._thread.start()
        # Bis zur ersten funktionalen Connection warten, damit early submits
        # nicht auf einen halb-initialisierten Thread treffen.
        self._started.wait(timeout=5.0)

    def stop(self, *, drain: bool = True, timeout: float = 10.0) -> None:
        """Beendet den Writer-Thread. `drain=True` arbeitet noch gequeuete Jobs ab."""
        if self._thread is None:
            return
        self._stop_flag.set()
        # Sentinel einreihen, damit `queue.get()` sofort zurueckkehrt.
        try:
            self._queue.put_nowait((_SENTINEL, None))
        except queue.Full:
            pass
        if drain:
            self._thread.join(timeout=timeout)
        else:
            self._thread.join(timeout=1.0)

    def queue_depth(self) -> int:
        return self._queue.qsize()

    # -- Submission ----------------------------------------------------------

    def submit(self, func: Callable[[sqlite3.Connection], Any], *,
               wait: bool = True, timeout: Optional[float] = 120.0) -> Any:
        """Reiht einen Write-Callable ein.

        `func(conn)` wird im Writer-Thread aufgerufen — die Connection ist
        die einzige Writer-Connection des Prozesses. Die Funktion darf
        `with write_tx(conn):` nutzen oder direkt `conn.execute(...)`
        (autocommit).

        `wait=True` (Default) blockiert bis zum Ergebnis. `wait=False`
        gibt das `Future` zurueck.
        """
        if threading.get_ident() == self._tid:
            raise RuntimeError(
                "db_writer.submit() darf nicht aus dem Writer-Thread heraus "
                "aufgerufen werden (sonst Deadlock)."
            )
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError("db_writer wurde nicht gestartet oder ist tot.")

        fut: Future = Future()
        self._queue.put(_Job(func=func, future=fut))

        if not wait:
            return fut
        return fut.result(timeout=timeout)

    # -- Internals -----------------------------------------------------------

    def _open_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=60)
        conn.row_factory = sqlite3.Row
        if self._apply_pragmas is not None:
            self._apply_pragmas(conn)
        return conn

    def _run(self) -> None:
        self._tid = threading.get_ident()
        # Watchdog: Bei Exception aus dem Worker-Loop Conn schliessen und
        # restart-Schleife; Jobs verbleiben in der Queue.
        while not self._stop_flag.is_set():
            try:
                self._conn = self._open_conn()
            except Exception:
                log.critical("db_writer: Konnte Writer-Connection nicht oeffnen", exc_info=True)
                time.sleep(2.0)
                continue

            if not self._started.is_set():
                self._started.set()

            try:
                self._worker_loop()
            except BaseException:
                log.critical("db_writer: Worker-Loop abgestuerzt — restart folgt", exc_info=True)
                self.restart_count += 1
            finally:
                try:
                    if self._conn is not None:
                        self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
            if self._stop_flag.is_set():
                break
            time.sleep(0.5)  # Backoff vor Restart

    def _worker_loop(self) -> None:
        while not self._stop_flag.is_set():
            item = self._queue.get()
            if item is _SENTINEL or (isinstance(item, tuple) and item and item[0] is _SENTINEL):
                return
            job: _Job = item
            try:
                result = job.func(self._conn)
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "locked" in msg or "busy" in msg:
                    self.retries += 1
                self.rollbacks += 1
                job.future.set_exception(exc)
            except BaseException as exc:
                self.rollbacks += 1
                job.future.set_exception(exc)
            else:
                self.jobs_processed += 1
                self.last_write_ts = time.time()
                job.future.set_result(result)


# --- Modulweiter Singleton-Zugriff ------------------------------------------

_writer_singleton: Optional[DbWriter] = None
_writer_lock = threading.Lock()


def start_writer(db_path: str) -> DbWriter:
    """Startet (idempotent) den globalen Writer und gibt ihn zurueck."""
    global _writer_singleton
    from db_pragmas import apply_pragmas

    def _apply(conn):
        apply_pragmas(conn, role="writer")

    with _writer_lock:
        if _writer_singleton is None:
            _writer_singleton = DbWriter(db_path, apply_pragmas=_apply)
        if not (_writer_singleton._thread and _writer_singleton._thread.is_alive()):
            _writer_singleton.start()
    return _writer_singleton


def get_writer() -> DbWriter:
    if _writer_singleton is None:
        raise RuntimeError(
            "db_writer wurde nicht gestartet. init_app_db() oder "
            "start_writer(db_path) zuerst aufrufen."
        )
    return _writer_singleton


def stop_writer() -> None:
    """Drained und beendet den globalen Writer (fuer atexit / SvcStop)."""
    global _writer_singleton
    with _writer_lock:
        if _writer_singleton is not None:
            _writer_singleton.stop(drain=True, timeout=10.0)
            _writer_singleton = None


def writer_stats() -> dict:
    """Metriken fuer /admin/diag bzw. /healthz."""
    if _writer_singleton is None:
        return {"running": False}
    w = _writer_singleton
    alive = bool(w._thread and w._thread.is_alive())
    return {
        "running": alive,
        "queue_depth": w.queue_depth(),
        "jobs_processed": w.jobs_processed,
        "retries": w.retries,
        "rollbacks": w.rollbacks,
        "restart_count": w.restart_count,
        "last_write_ts": w.last_write_ts,
    }
