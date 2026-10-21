"""Runs radar.py on a timer inside the app, replacing the scheduled tasks.

Each run is its own process (radar.py, or the packaged app with --radar), so
a crash or a memory spike in one run can't take the dashboard down with it,
and quitting the app stops a run cleanly: SQLite keeps every write atomic.

When the next run is due comes from the last run that finished, as recorded
in jobs.db (`runs`) and in app-state.json, so restarting the app, waking the
computer or a run started by hand all count. Runs wait until the first-run
setup is done, and a run never overlaps another: radar.py holds radar.lock,
so even a leftover scheduled task can't fetch at the same time.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import config

STARTUP_DELAY = 120         # seconds after the app starts before a due run begins
RUN_TIMEOUT = 45 * 60       # a run still going after this is stopped
RETRY_SKIPPED = 5 * 60      # another run held the lock: look again this soon
TICK = 5.0
SKIPPED = 3                 # radar.SKIPPED (not imported: radar.py is a large module)

PHASES = {"starting": "Starting", "visa-data": "Downloading H-1B sponsor data", "fetching": "Reading job boards",
          "saving": "Saving and scoring jobs", "done": "Done"}


def default_command(status_file: Path) -> list[str]:
    """How to start one radar run from this install."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--radar", "--status-file", str(status_file)]
    return [sys.executable, str(Path(__file__).resolve().parent / "radar.py"), "--status-file", str(status_file)]


class Scheduler:
    def __init__(self, store: config.Store, log: Callable[[str], None], *,
                 command: Callable[[Path], list[str]] = default_command,
                 clock: Callable[[], float] = time.time, popen=subprocess.Popen,
                 automatic: bool = True) -> None:
        self.store, self.log, self.command, self.clock, self.popen = store, log, command, clock, popen
        self.automatic = automatic
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = False
        self.proc = None
        self.run: dict | None = None            # the run in progress
        self.started_at = clock()
        self.on_change: Callable[[], None] | None = None
        home = store.home
        self.state_path = home / "app-state.json"
        self.status_path = home / "radar-status.json"
        self.state = self._load_state()         # {"last": {...}} of the last run this app started
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ persistence

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as e:
            self.log(f"scheduler: could not save {self.state_path.name}: {e}")

    def last_db_run(self) -> dict | None:
        """The newest finished run in jobs.db, whoever started it."""
        db = self.store.path_for("db")
        if not db.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                row = conn.execute("SELECT started, finished, matched, new, failed FROM runs "
                                   "ORDER BY id DESC LIMIT 1").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        if not row:
            return None
        return {"started": row[0], "finished": row[1], "matched": row[2], "new": row[3], "failed": row[4]}

    # ------------------------------------------------------------ timing

    def interval(self) -> float:
        return self.store.section("radar")["interval_minutes"] * 60

    def setup_done(self) -> bool:
        return self.store.section("setup")["done"]

    def next_run(self) -> float | None:
        """When the next scheduled run starts; None while runs wait (setup
        not done, not automatic, or one is going)."""
        if not self.automatic or not self.setup_done() or self.run:
            return None
        now = self.clock()
        last = self.state.get("last") or {}
        if last.get("outcome") == "skipped":
            return max(last["finished"] + RETRY_SKIPPED, self.started_at + STARTUP_DELAY)
        finished = [t for t in ((self.last_db_run() or {}).get("finished"), last.get("finished")) if t]
        due = max(finished) + self.interval() if finished else now
        # Right after login a due run waits a little, so it doesn't slow startup.
        return max(due, self.started_at + STARTUP_DELAY)

    # ------------------------------------------------------------ runs

    def run_now(self, reason: str = "manual") -> bool:
        """Start a run now. False when one is already going."""
        with self.lock:
            if self.run:
                return False
            self._start(reason)
        return True

    def _start(self, reason: str) -> None:
        try:
            self.status_path.unlink()
        except OSError:
            pass
        cmd = self.command(self.status_path)
        env = {**os.environ, "JOBRADAR_HOME": str(self.store.home)}
        kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}     # CREATE_NO_WINDOW
        try:
            self.proc = self.popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, **kw)
        except OSError as e:
            self.log(f"scheduler: could not start a run: {e}")
            self._finish({"outcome": "error", "error": f"Couldn't start the job search: {e}"}, self.clock())
            return
        self.run = {"started": self.clock(), "reason": reason}
        self.log(f"scheduler: run started ({reason})")
        self._changed()

    def progress(self) -> dict:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _poll(self) -> None:
        """Check on the run in progress; record it once it ends."""
        with self.lock:
            if not self.run:
                return
            code = self.proc.poll()
            now = self.clock()
            if code is None:
                if now - self.run["started"] > RUN_TIMEOUT:
                    self.log("scheduler: run took too long; stopping it")
                    self._kill()
                    self._finish({"outcome": "error", "error": "The job search took too long and was stopped."}, now)
                return
            p = self.progress()
            if code == 0:
                last = {"outcome": "ok", **{k: p[k] for k in ("matched", "new", "failed", "boards") if k in p}}
            elif code == SKIPPED:
                last = {"outcome": "skipped", "error": "Another job search was still running."}
            else:
                last = {"outcome": "error",
                        "error": p.get("error") or f"The job search stopped with code {code}. Details are in radar.log."}
            self._finish(last, now)

    def _finish(self, last: dict, now: float) -> None:
        self.state["last"] = {"started": (self.run or {}).get("started", now), "finished": now, **last}
        self._save_state()
        self.log(f"scheduler: run {last['outcome']}" + (f": {last['error']}" if last.get("error") else ""))
        self.run, self.proc = None, None
        self._changed()

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def tick(self) -> None:
        """One step of the loop: finish an ended run, start a due one."""
        self._poll()
        with self.lock:
            due = self.next_run()
            if due is not None and due <= self.clock():
                self._start("schedule")

    def _loop(self) -> None:
        while not self.stopping:
            try:
                self.tick()
            except Exception as e:     # keep scheduling whatever one tick hit
                self.log(f"scheduler: {type(e).__name__}: {e}")
            self.wake.wait(1.0 if self.run else TICK)
            self.wake.clear()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stopping = True
        self.wake.set()
        with self.lock:
            if self.run:
                self.log("scheduler: stopping the run in progress")
                self._kill()
                self.run, self.proc = None, None

    # ------------------------------------------------------------ status

    def status(self) -> dict:
        """What the tray and the dashboard show."""
        with self.lock:
            last = dict(self.state.get("last") or {})
            db_last = self.last_db_run()
            # A run started elsewhere (by hand) may be newer than ours.
            if db_last and db_last["finished"] and db_last["finished"] > last.get("finished", 0):
                last = {"outcome": "ok", **db_last}
            out = {
                "automatic": self.automatic, "setup_done": self.setup_done(),
                "interval_minutes": self.interval() / 60, "next_run": self.next_run(),
                "last": last or None, "running": None,
            }
            if self.run:
                p = self.progress()
                out["running"] = {**self.run, "phase": p.get("phase", "starting"),
                                  "label": PHASES.get(p.get("phase", "starting"), "Working"),
                                  **{k: p[k] for k in ("done", "total", "jobs") if k in p}}
            return out
