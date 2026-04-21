"""
Prozess-Lock für idvault: verhindert, dass mehrere App-Instanzen gleichzeitig
auf dieselbe SQLite-Datenbank schreiben und den Scheduler doppelt starten.

Lock-File: <instance_path>/idv.lock  (JSON: {"pid": …, "started": "…"})

Verhalten:
  - Kein Lock-File   → anlegen, weitermachen
  - Lock-File, PID aktiv → SystemExit mit Fehlermeldung
  - Lock-File, PID tot    → Stale-Lock überschreiben, weitermachen

Kompatibel mit Windows und Linux (kein psutil erforderlich).
"""

import atexit
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


_LOCK_FILENAME = "idv.lock"
_lock_path: Path | None = None


# ── plattformübergreifende PID-Prüfung ────────────────────────────────────────

def _pid_running(pid: int) -> bool:
    """Gibt True zurück, wenn der Prozess mit ``pid`` existiert und läuft."""
    if pid <= 0:
        return False

    # psutil bevorzugen, falls installiert
    try:
        import psutil  # type: ignore
        return psutil.pid_exists(pid)
    except ImportError:
        pass

    if os.name == "nt":
        return _pid_running_windows(pid)
    return _pid_running_posix(pid)


def _pid_running_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # Signal 0 prüft Existenz ohne zu töten
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Prozess existiert, aber wir haben keine Berechtigung → aktiv
        return True
    except OSError:
        return False


def _pid_running_windows(pid: int) -> bool:
    import ctypes
    import ctypes.wintypes as wt

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wt.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return False
    finally:
        kernel32.CloseHandle(handle)


# ── Lock-Datei-Operationen ─────────────────────────────────────────────────────

def _read_lock(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_lock(path: Path) -> None:
    data = {
        "pid":     os.getpid(),
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _remove_lock() -> None:
    if _lock_path is not None and _lock_path.exists():
        try:
            _lock_path.unlink()
        except OSError:
            pass


# ── öffentliche API ───────────────────────────────────────────────────────────

def acquire(instance_path: str) -> None:
    """Lock-File anlegen oder Stale-Lock überschreiben.

    Wirft SystemExit(1), wenn eine andere aktive Instanz erkannt wird.
    Muss einmalig beim App-Start aufgerufen werden.
    """
    global _lock_path
    _lock_path = Path(instance_path) / _LOCK_FILENAME

    if _lock_path.exists():
        data = _read_lock(_lock_path)
        if data:
            existing_pid = int(data.get("pid", 0))
            if existing_pid and existing_pid != os.getpid() and _pid_running(existing_pid):
                started = data.get("started", "unbekannt")
                print(
                    f"\n[idvault] FEHLER: Eine andere idvault-Instanz läuft bereits "
                    f"(PID {existing_pid}, gestartet {started}).\n"
                    f"Bitte diese Instanz zuerst beenden oder das Lock-File entfernen:\n"
                    f"  {_lock_path}\n",
                    file=sys.stderr,
                )
                sys.exit(1)
            # Stale-Lock: Prozess existiert nicht mehr → überschreiben
        # Ungültiges Lock-File → ebenfalls überschreiben

    _write_lock(_lock_path)
    atexit.register(_remove_lock)


def release() -> None:
    """Lock-File beim sauberen Shutdown explizit entfernen."""
    _remove_lock()
