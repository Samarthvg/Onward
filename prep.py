"""Background preparation queue: gets jobs ready to apply to before the user
opens them.

After each radar run, newly seen jobs that score at or above the user's
threshold and pass their filters are queued. One at a time, best match
first, each job gets either a tailored resume or a note that the master
resume already fits (tailoring would only reorder or the posting is
already covered), plus drafts for the custom questions on its form when
the ATS publishes them. Model runs per day are capped to stay inside the
user's plan; runs on an API key are also held to its monthly budget (ai.py). Nothing here applies to anything: the user opens each job from
the queue, reviews what the extension filled and presses Submit.
"""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import config
import match
import notify
import questions
import resume_tex as rt
import tailor

SCHEMA = """
CREATE TABLE IF NOT EXISTS prep (
    job_key     TEXT PRIMARY KEY,
    state       TEXT NOT NULL,             -- queued, working, ready, error, applied, dismissed, closed
    score       INTEGER,
    created     REAL NOT NULL,
    updated     REAL NOT NULL,
    decision    TEXT NOT NULL DEFAULT '',  -- tailor or master
    reason      TEXT NOT NULL DEFAULT '',
    tailored_id TEXT NOT NULL DEFAULT '',
    choice      TEXT NOT NULL DEFAULT '',  -- tailored or master: the resume the extension attaches
    reviewed    INTEGER NOT NULL DEFAULT 0,
    answers     TEXT NOT NULL DEFAULT '[]',
    error       TEXT NOT NULL DEFAULT '',
    instruction TEXT NOT NULL DEFAULT '',  -- the user's "adjust" request for the next tailoring
    attempts    INTEGER NOT NULL DEFAULT 0,
    notified    REAL,
    applied     REAL
);
CREATE INDEX IF NOT EXISTS prep_state ON prep(state, score);
CREATE TABLE IF NOT EXISTS prep_usage (
    day  TEXT PRIMARY KEY,
    runs INTEGER NOT NULL
);
"""

# Columns added after the first release of the queue.
MIGRATIONS = {"instruction": "TEXT NOT NULL DEFAULT ''"}


def migrate(conn) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(prep)")}
    for col, decl in MIGRATIONS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE prep ADD COLUMN {col} {decl}")


# The queue's settings are settings.json "queue" (config.QUEUE has each one's
# meaning and range); they used to live in resume/prep.json.
DEFAULTS = config.QUEUE.default
STALE_AFTER = 2 * 86400          # radar.py's STALE_AFTER_DAYS: unseen this long means closed
MAX_ATTEMPTS = 2
NOTIFY_EVERY = 30 * 60           # while the queue is still working, at most one toast per half hour
BACKOFF = 15 * 60                # after the backend is unreachable or signed out
# Problems no retry fixes until the user acts: sign-in, a missing or rejected
# API key, or a used-up monthly budget.
FATAL = re.compile(r"not signed in|isn't installed|log ?in|api key|api budget", re.I)
DASHBOARD_URL = "http://127.0.0.1:8765/#queue"


def clean_settings(raw: dict) -> dict:
    """Queue settings with anything invalid replaced by its default and
    numbers clamped to their range."""
    return config.QUEUE.clean({k: v for k, v in raw.items() if k in config.QUEUE.fields}, "queue", [])


def required_coverage(score: dict) -> float:
    """Share of the required keywords' weight the resume already has."""
    req = [i for i in score["items"] if i["kind"] in ("title", "req")] or score["items"]
    total = sum(i["weight"] for i in req)
    return sum(i["weight"] for i in req if i["ok"]) / total if total else 1.0


def decide(score: dict, res: rt.Resume, settings: dict) -> tuple[str, str]:
    """("tailor" or "master", why). Tailoring can only add skills the resume
    backs (its skill tags and inventory), so when none of the missing
    keywords are backed it could only reorder and trim, and the master is
    used as is. The same when the master already covers most requirements."""
    backed = set(res.inventory)
    for e in res.entries:
        backed |= set(e.allowed)
    missing = [i["k"] for i in score["items"] if not i["ok"]]
    addable = [k for k in missing if k in match.TAXONOMY and k in backed]
    cov = round(100 * required_coverage(score))
    if not addable:
        miss = ", ".join(missing[:4]) + (f" and {len(missing) - 4} more" if len(missing) > 4 else "")
        return "master", ("Your master resume has every keyword this posting asks for." if not missing else
                          f"Tailoring could only reorder: nothing on your resume backs {miss}.")
    if cov >= settings["skip_coverage"]:
        return "master", f"Your master resume already covers {cov}% of the required keywords."
    return "tailor", f"Can work in {', '.join(addable[:5])} from your skill tags ({cov}% of required keywords covered now)."


def change_summary(result: dict) -> dict:
    """What tailoring changed, compact enough for one line in the queue."""
    ch = result.get("changes") or []
    applied = [c for c in ch if c.get("status") == "applied" and c.get("kind") in ("bullet", "tech", "skills")]
    return {
        "rewrites": sum(1 for c in applied if c["kind"] == "bullet"),
        "lists": sum(1 for c in applied if c["kind"] in ("tech", "skills")),
        "dropped_bullets": sum(1 for c in ch if c.get("kind") == "drop_bullet"),
        "dropped_entries": [c.get("label", "") for c in ch if c.get("kind") == "drop_entry"],
        "added": [k["keyword"] for k in result.get("keywords") or [] if k.get("status") == "added"],
        "missing": [k["keyword"] for k in result.get("keywords") or [] if k.get("status") == "missing"],
        "rejected": sum(1 for c in ch if c.get("status") == "rejected"),
        "pages": result.get("pages"),
        "summary": result.get("summary", ""),
    }


class Preparer:
    """The queue. `connect` opens jobs.db with this module's SCHEMA and the
    `tailored` table applied (server.db); `store` holds its settings."""

    def __init__(self, connect: Callable, resume_dir: Path, out_dir: Path, log: Callable[[str], None] = print,
                 store: config.Store | None = None):
        self.connect = connect
        self.resume_dir = Path(resume_dir)
        self.out_dir = Path(out_dir)
        self.master_path = self.resume_dir / "master.tex"
        self.store = store or config.store()
        self.scoring_path = self.resume_dir / "scoring.json"
        self.log = log
        self.wake = threading.Event()
        self.lock = threading.Lock()          # settings file and scans
        self.dirty = True
        self.corpus_seen: float | None = None
        self.status = ""                      # why the queue is idle, for the dashboard
        self.paused_until = 0.0
        self.current: str | None = None
        self.last_notify = 0.0
        self._stop = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ settings

    def settings(self) -> dict:
        return self.store.section("queue")

    def save_settings(self, raw: dict) -> dict:
        with self.lock:
            s = clean_settings({**self.settings(), **raw})
            self.store.update({"queue": s})
        self.poke()
        return s

    def weights(self) -> dict:
        return self.store.section("scoring")

    def master_for(self, roles: list[str]) -> Path:
        """The master resume for a job: that of the first of its roles with
        a resume of its own, else master.tex."""
        own = {r["id"]: r["resume"] for r in self.store.load()["roles"] if r["resume"]}
        name = next((own[r] for r in roles if r in own and (self.resume_dir / own[r]).exists()), "")
        return self.resume_dir / name if name else self.master_path

    def save_scoring(self, data: dict) -> None:
        """The dashboard's resume as it scores it (text, skills, the user's
        corrections, years) and the Apply kit details used for drafts."""
        prof = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        out = {
            "name": str(data.get("name", ""))[:200],
            "text": str(data.get("text", ""))[:200000],
            "skills": [str(x)[:80] for x in (data.get("skills") or [])][:500] if isinstance(data.get("skills"), list) else None,
            "off": [str(x)[:80] for x in (data.get("off") or [])][:500],
            "added": [str(x)[:80] for x in (data.get("added") or [])][:500],
            "years": data.get("years") if isinstance(data.get("years"), (int, float)) else None,
            "profile": {k: str(v)[:300] for k, v in prof.items() if k in tailor.PROFILE_FOR_ANSWERS},
            "updated": time.time(),
        }
        with self.lock:
            self._write(self.scoring_path, out)
        self.poke()

    def _write(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)

    def scoring(self) -> dict | None:
        try:
            data = json.loads(self.scoring_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and len(data.get("text", "")) >= 200 else None
        except (OSError, json.JSONDecodeError):
            return None

    def resume(self) -> match.Resume | None:
        """The resume the dashboard scores against; the master .tex when the
        dashboard hasn't sent one yet."""
        s = self.scoring()
        if s:
            return match.Resume.from_profile(s)
        if self.master_path.exists():
            return match.Resume(match.tex_text(self.master_path.read_text(encoding="utf-8")))
        return None

    # ------------------------------------------------------------ scanning

    def poke(self) -> None:
        self.dirty = True
        # Settings changed (a new key, a higher budget): try again right away.
        self.paused_until = 0.0
        self.wake.set()

    @staticmethod
    def corpus(conn) -> tuple[float, dict, dict] | None:
        try:
            row = conn.execute("SELECT updated, skill_w, term_w FROM corpus WHERE id = 1").fetchone()
        except Exception:      # radar.py hasn't run with scoring yet
            return None
        return (row[0], json.loads(row[1]), json.loads(row[2])) if row else None

    @staticmethod
    def eligible(job: dict, s: dict) -> str | None:
        """Why a job is filtered out, or None."""
        if s["needs_sponsorship"] and (job["sponsorship"] == "no" or job["no_opt"]):
            return "says no sponsorship or no OPT"
        locs = [l.lower() for l in s["locations"]]
        if locs and not (job["remote"] and s["remote_ok"]) and not any(l in job["location"].lower() for l in locs):
            return "outside your locations"
        return None

    JOB_COLS = ("key", "company", "title", "location", "remote", "sponsorship", "no_opt", "min_yoe",
                "entry_hint", "kw", "degree", "has_jd", "first_seen", "last_seen", "url", "apply_url", "source",
                "roles")

    def _job(self, row) -> dict:
        j = dict(zip(self.JOB_COLS, row))
        j["kw"] = json.loads(j["kw"] or "[]")
        j["degree"] = json.loads(j["degree"]) if j["degree"] else None
        j["has_jd"] = bool(j["has_jd"])
        j["roles"] = json.loads(j["roles"] or "[]")
        return j

    @staticmethod
    def _tagged(conn) -> bool:
        """Whether radar.py has tagged jobs with the user's roles yet."""
        return "roles" in {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}

    def _select(self, conn) -> str:
        roles = "roles" if self._tagged(conn) else "'[]'"
        return ("SELECT key, company, title, location, remote, sponsorship, no_opt, min_yoe, entry_hint, kw, degree, "
                f"length(description) > 200, first_seen, last_seen, url, apply_url, source, {roles} FROM jobs ")

    def scan(self, now: float | None = None) -> int:
        """Queue newly seen jobs that qualify. Returns how many were added."""
        now = time.time() if now is None else now
        s = self.settings()
        conn = self.connect()
        try:
            corpus = self.corpus(conn)
            if corpus:
                self.corpus_seen = corpus[0]
            if not s["enabled"]:
                self.status = "Preparation is off."
                return 0
            if not corpus:
                self.status = "Waiting for the next radar run."
                return 0
            if not self.master_path.exists():
                self.status = "Upload your master .tex resume to start preparing jobs."
                return 0
            resume = self.resume()
            weights = self.weights()
            masters: dict[Path, rt.Resume] = {}

            def master_of(j: dict) -> rt.Resume:
                path = self.master_for(j["roles"])
                if path not in masters:
                    masters[path] = rt.parse(path.read_text(encoding="utf-8"))
                return masters[path]
            _, skill_w, term_w = corpus
            with conn:
                # Postings that closed, and queued ones the current settings no longer want.
                conn.execute("UPDATE prep SET state = 'closed', updated = ? WHERE state IN ('queued', 'ready', 'error') "
                             "AND job_key IN (SELECT key FROM jobs WHERE last_seen < ?)", (now, now - STALE_AFTER))
                conn.execute("UPDATE prep SET state = 'closed', updated = ? WHERE state IN ('queued', 'ready', 'error') "
                             "AND job_key NOT IN (SELECT key FROM jobs)", (now,))
                # Postings now older than the queue takes: they leave it, prepared or not.
                conn.execute("UPDATE prep SET state = 'expired', updated = ? WHERE state IN ('queued', 'ready', 'error') "
                             "AND job_key IN (SELECT key FROM jobs WHERE COALESCE(posted_at, first_seen) < ?)",
                             (now, now - s["max_age_days"] * 86400))
                # Rescore what's in the queue (the resume or keyword weights may have changed),
                # so it always shows the dashboard's number.
                for row in conn.execute(self._select(conn) + "WHERE key IN (SELECT job_key FROM prep "
                                        "WHERE state IN ('queued', 'working', 'ready', 'error'))").fetchall():
                    j = self._job(row)
                    sc = match.score_job(j, resume, skill_w, term_w, weights)
                    conn.execute("UPDATE prep SET score = ? WHERE job_key = ?", (sc["pct"], j["key"]))
                    if sc["pct"] is None or sc["pct"] < s["threshold"] or self.eligible(j, s):
                        conn.execute("DELETE FROM prep WHERE job_key = ? AND state = 'queued'", (j["key"],))
                # Only jobs one of the user's roles wants.
                rows = conn.execute(
                    # New to the radar and recently posted: a board read for the first
                    # time lists postings that went up months ago.
                    self._select(conn) + "WHERE kw != '' AND first_seen >= ? AND last_seen >= ? "
                    "AND COALESCE(posted_at, first_seen) >= ? "
                    + ("AND roles != '[]' " if self._tagged(conn) else "")
                    + "AND key NOT IN (SELECT job_key FROM prep)",
                    (now - s["lookback_hours"] * 3600, now - STALE_AFTER, now - s["max_age_days"] * 86400)).fetchall()
                added = 0
                for row in rows:
                    j = self._job(row)
                    sc = match.score_job(j, resume, skill_w, term_w, weights)
                    if sc["pct"] is None or sc["pct"] < s["threshold"] or self.eligible(j, s):
                        continue
                    decision, reason = decide(sc, master_of(j), s)
                    conn.execute("INSERT INTO prep (job_key, state, score, created, updated, decision, reason) "
                                 "VALUES (?, 'queued', ?, ?, ?, ?, ?)", (j["key"], sc["pct"], now, now, decision, reason))
                    added += 1
            self.status = ""
            if added:
                self.log(f"prep: queued {added} job(s)")
            return added
        finally:
            conn.close()

    # ------------------------------------------------------------ usage cap

    @staticmethod
    def today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def usage(self, conn) -> int:
        row = conn.execute("SELECT runs FROM prep_usage WHERE day = ?", (self.today(),)).fetchone()
        return row[0] if row else 0

    def reserve_run(self) -> bool:
        """One run against today's cap, for re-tailoring started from the dashboard."""
        conn = self.connect()
        try:
            return self.reserve(conn, self.settings()["daily_cap"])
        finally:
            conn.close()

    def reserve(self, conn, cap: int) -> bool:
        """Counts one model run against today's cap, if any are left."""
        with conn:
            if self.usage(conn) >= cap:
                return False
            conn.execute("INSERT INTO prep_usage (day, runs) VALUES (?, 1) "
                         "ON CONFLICT(day) DO UPDATE SET runs = runs + 1", (self.today(),))
        return True

    # ------------------------------------------------------------ processing

    def _update(self, conn, key: str, when: str | None = None, **kw) -> None:
        """Update a row; with `when`, only while it is still in that state
        (an undo may have put a working job back meanwhile)."""
        kw["updated"] = time.time()
        guard = " AND state = ?" if when else ""
        with conn:
            conn.execute(f"UPDATE prep SET {', '.join(k + ' = ?' for k in kw)} WHERE job_key = ?{guard}",
                         (*kw.values(), key, *([when] if when else [])))

    def next_job(self, conn, cap_left: bool) -> tuple[str, str] | None:
        sql = "SELECT job_key, decision FROM prep WHERE state = 'queued'"
        if not cap_left:
            sql += " AND decision = 'master'"      # these still get ready without a model run
        return conn.execute(sql + " ORDER BY score DESC, created LIMIT 1").fetchone()

    def process(self, key: str) -> str:
        """Prepare one queued job: "done", "requeued", or "stop" (backend down)."""
        s = self.settings()
        conn = self.connect()
        self.current = key
        try:
            roles = "j.roles" if self._tagged(conn) else "'[]'"
            row = conn.execute("SELECT j.company, j.title, j.description, j.apply_url, p.decision, p.attempts, p.answers, "
                               f"p.instruction, {roles} FROM prep p JOIN jobs j ON j.key = p.job_key "
                               "WHERE p.job_key = ?", (key,)).fetchone()
            if not row:
                return "done"
            company, title, jd, apply_url, decision, attempts, drafted, instruction, tags = row
            self._update(conn, key, state="working", error="")
            master = self.master_for(json.loads(tags or "[]")).read_text(encoding="utf-8")
            meta = {"company": company, "title": title, "instruction": instruction}
            tailored_id, notes = "", []
            if decision == "tailor":
                if not self.reserve(conn, s["daily_cap"]):
                    self._update(conn, key, when="working", state="queued",
                                 error=f"Waiting: today's {s['daily_cap']} model runs are used up. Continues tomorrow.")
                    return "requeued"
                result = tailor.tailor(master, meta, jd, s["backend"], self.out_dir)
                tailored_id = uuid.uuid4().hex[:12]
                with conn:
                    conn.execute("INSERT INTO tailored (id, job_key, created, backend, model, pdf, tex, result) "
                                 "VALUES (?,?,?,?,?,?,?,?)", (tailored_id, key, time.time(), s["backend"],
                                                             result["model"], result["pdf"], result["tex"], json.dumps(result)))
            # Answers drafted on an earlier pass (before "Tailor anyway") are kept.
            answers = json.loads(drafted or "[]")
            if not answers and s["draft_answers"]:
                answers = self._draft(conn, s, master, meta, jd, apply_url, notes)
            self._update(conn, key, when="working", state="ready", tailored_id=tailored_id,
                         choice="tailored" if tailored_id else "master", answers=json.dumps(answers),
                         error="; ".join(notes))
            self.log(f"prep: ready {company} / {title} ({'tailored' if tailored_id else 'master'}, {len(answers)} answers)")
            return "done"
        except (tailor.BackendError, rt.CompileError, ValueError) as e:
            msg = str(e)
            if FATAL.search(msg):
                self._update(conn, key, when="working", state="queued", error=msg)
                self.status = msg
                self.paused_until = time.time() + BACKOFF
                return "stop"
            attempts += 1
            self._update(conn, key, when="working", state="error" if attempts >= MAX_ATTEMPTS else "queued", attempts=attempts, error=msg)
            return "done"
        except Exception as e:
            self.log(f"prep: {key} failed:\n{traceback.format_exc()}")
            self._update(conn, key, when="working", state="error", attempts=MAX_ATTEMPTS, error=f"Unexpected error: {e}")
            return "done"
        finally:
            self.current = None
            conn.close()

    def _draft(self, conn, s: dict, master: str, meta: dict, jd: str, apply_url: str, notes: list[str]) -> list[dict]:
        """Answers to the form's custom questions. Failures only cost the
        drafts: the extension drafts at fill time instead."""
        try:
            qs = questions.fetch(apply_url)
        except Exception as e:     # network, or the ATS changed its format
            notes.append(f"couldn't read the form's questions ({type(e).__name__})")
            return []
        if not qs:
            return []
        if not self.reserve(conn, s["daily_cap"]):
            notes.append("daily model run limit reached, so questions are drafted when you open the form")
            return []
        prof = (self.scoring() or {}).get("profile") or {}
        # Short ids for the model; positions map them back.
        batch = [{**q, "id": str(i), "context": ""} for i, q in enumerate(qs)]
        try:
            got = tailor.answer_questions(master, meta, prof, batch, jd, s["backend"])
        except tailor.BackendError as e:
            notes.append(f"drafting answers failed: {e}")
            return []
        byid = {a["id"]: a for a in got["answers"]}
        return [{"question": q["question"], "kind": q["kind"], "options": q["options"],
                 "answer": byid[str(i)]["answer"], "choices": byid[str(i)]["choices"]}
                for i, q in enumerate(qs) if str(i) in byid]

    def step(self) -> None:
        """One pass: rescan when the radar or settings changed, then work the queue."""
        conn = self.connect()
        try:
            corpus = self.corpus(conn)
        finally:
            conn.close()
        if self.dirty or (corpus and corpus[0] != self.corpus_seen):
            self.dirty = False
            with self.lock:
                self.scan()
        while not self._stop and time.time() >= self.paused_until and self.settings()["enabled"]:
            s = self.settings()
            conn = self.connect()
            try:
                nxt = self.next_job(conn, self.usage(conn) < s["daily_cap"])
            finally:
                conn.close()
            if not nxt or self.process(nxt[0]) == "stop":
                break
            if self.dirty:
                break               # new radar data or settings: rescan first
        self._note_cap()
        self.maybe_notify()

    def _note_cap(self) -> None:
        cap = self.settings()["daily_cap"]
        conn = self.connect()
        try:
            if self.usage(conn) >= cap:
                with conn:
                    conn.execute("UPDATE prep SET error = ? WHERE state = 'queued' AND decision = 'tailor'",
                                 (f"Waiting: today's {cap} model runs are used up. Continues tomorrow.",))
        finally:
            conn.close()

    # ------------------------------------------------------------ notifications

    def maybe_notify(self, now: datetime | None = None) -> bool:
        s = self.settings()
        if not (s["notify_desktop"] or s["ntfy_topic"]):
            return False
        now = now or datetime.now()
        if notify.quiet(now, s["quiet_start"], s["quiet_end"]):
            return False            # held until quiet hours end
        conn = self.connect()
        try:
            fresh = conn.execute("SELECT p.job_key, j.company FROM prep p JOIN jobs j ON j.key = p.job_key "
                                 "WHERE p.state = 'ready' AND p.notified IS NULL ORDER BY p.score DESC").fetchall()
            if not fresh:
                return False
            busy = conn.execute("SELECT COUNT(*) FROM prep WHERE state IN ('queued', 'working')").fetchone()[0]
            if busy and self.usage(conn) < s["daily_cap"] and time.time() - self.last_notify < NOTIFY_EVERY:
                return False        # more are on the way; wait for them
            ready = conn.execute("SELECT COUNT(*) FROM prep WHERE state = 'ready'").fetchone()[0]
            names = list(dict.fromkeys(c for _, c in fresh))
            title = f"{ready} job{'s' if ready != 1 else ''} ready to apply"
            body = "New: " + ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
            sent = False
            if s["notify_desktop"]:
                sent = notify.desktop(title, body, DASHBOARD_URL) or sent
            if s["ntfy_topic"]:
                sent = notify.ntfy(s["ntfy_server"], s["ntfy_topic"], title, body) or sent
            with conn:
                conn.executemany("UPDATE prep SET notified = ? WHERE job_key = ?", [(time.time(), k) for k, _ in fresh])
            self.last_notify = time.time()
            return sent
        finally:
            conn.close()

    # ------------------------------------------------------------ user actions

    def mark(self, key: str, state: str) -> bool:
        """applied or dismissed, from the dashboard or the extension."""
        return bool(self.mark_many({key: state}))

    def mark_many(self, marks: dict[str, str | None], recorded: list[str] | None = None) -> int:
        """Applied/dismissed marks (None undoes one). Jobs not queued yet get
        a row too, so they are never prepared. Returns how many jobs matched;
        `recorded` collects the jobs newly added to an applications table."""
        now = time.time()
        n = 0
        conn = self.connect()
        try:
            with conn:
                for key, state in marks.items():
                    if not conn.execute("SELECT 1 FROM jobs WHERE key = ?", (key,)).fetchone():
                        continue
                    n += 1
                    if state is None:
                        # Back to ready when it was prepared; otherwise forget it so a scan may queue it.
                        conn.execute("DELETE FROM prep WHERE job_key = ? AND choice = '' "
                                     "AND state IN ('applied', 'dismissed')", (key,))
                        conn.execute("UPDATE prep SET state = 'ready', applied = NULL, updated = ? "
                                     "WHERE job_key = ? AND state IN ('applied', 'dismissed')", (now, key))
                        continue
                    conn.execute("INSERT INTO prep (job_key, state, created, updated, applied) VALUES (?, ?, ?, ?, ?) "
                                 "ON CONFLICT(job_key) DO UPDATE SET state = excluded.state, updated = excluded.updated, "
                                 "applied = COALESCE(excluded.applied, prep.applied)",
                                 (key, state, now, now, now if state == "applied" else None))
                    if state == "applied" and self._record_application(conn, key, now) and recorded is not None:
                        recorded.append(key)
            return n
        finally:
            conn.close()

    def unmark(self, key: str) -> None:
        """Undo applied/dismissed: back to ready when it was prepared."""
        self.mark_many({key: None})

    @staticmethod
    def _record_application(conn, key: str, now: float) -> bool:
        """Also record it in an `applications` table, if a tracker made one.
        True when a row was added."""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(applications)")}
        if "job_key" not in cols:
            return False
        job = conn.execute("SELECT company, title, url FROM jobs WHERE key = ?", (key,)).fetchone()
        vals = {"job_key": key, "company": job[0], "title": job[1], "url": job[2], "status": "applied",
                "applied_at": now, "created": now, "updated": now}
        vals = {k: v for k, v in vals.items() if k in cols}
        cur = conn.execute(f"INSERT OR IGNORE INTO applications ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                           tuple(vals.values()))
        return cur.rowcount > 0

    # ------------------------------------------------------------ undo

    def snapshot(self, keys) -> dict[str, dict | None]:
        """The queue rows of these jobs as they are now (None: no row), for
        restore() to put back."""
        conn = self.connect()
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(prep)")]
            out: dict[str, dict | None] = {}
            for key in keys:
                row = conn.execute(f"SELECT {', '.join(cols)} FROM prep WHERE job_key = ?", (key,)).fetchone()
                out[key] = dict(zip(cols, row)) if row else None
            return out
        finally:
            conn.close()

    def restore(self, rows: dict[str, dict | None], applications: list[str] = ()) -> None:
        """Put snapshot() rows back exactly, and take out the applications
        rows the undone change added."""
        conn = self.connect()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(prep)")}
            with conn:
                for key, row in rows.items():
                    if row is None:
                        conn.execute("DELETE FROM prep WHERE job_key = ?", (key,))
                        continue
                    row = {k: v for k, v in row.items() if k in cols}
                    conn.execute(f"INSERT OR REPLACE INTO prep ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                                 tuple(row.values()))
                app_cols = {r[1] for r in conn.execute("PRAGMA table_info(applications)")}
                if "job_key" in app_cols:
                    for key in applications:
                        conn.execute("DELETE FROM applications WHERE job_key = ?", (key,))
        finally:
            conn.close()
        self.wake.set()

    def set_choice(self, key: str, choice: str) -> bool:
        conn = self.connect()
        try:
            with conn:
                cur = conn.execute("UPDATE prep SET choice = ?, reviewed = 1, updated = ? WHERE job_key = ? "
                                   "AND (? = 'master' OR tailored_id != '')", (choice, time.time(), key, choice))
            return cur.rowcount > 0
        finally:
            conn.close()

    def force_tailor(self, key: str, instruction: str = "") -> bool:
        """Tailor a job the skip rule sent to the master resume, or, with an
        instruction ("emphasize backend more"), tailor it again. The earlier
        version stays in the tailored history."""
        instruction = tailor.clean_instruction(instruction)
        reason = f"Re-tailoring: {instruction}" if instruction else "You asked for a tailored resume."
        conn = self.connect()
        try:
            with conn:
                cur = conn.execute("UPDATE prep SET decision = 'tailor', state = 'queued', attempts = 0, error = '', "
                                   "reason = ?, instruction = ?, reviewed = 0, updated = ? WHERE job_key = ? "
                                   "AND (tailored_id = '' OR ? != '') AND state IN ('ready', 'error', 'queued')",
                                   (reason, instruction, time.time(), key, instruction))
            return cur.rowcount > 0
        finally:
            conn.close()
            self.wake.set()

    def retry(self, key: str) -> None:
        conn = self.connect()
        try:
            with conn:
                conn.execute("UPDATE prep SET state = 'queued', attempts = 0, error = '', updated = ? "
                             "WHERE job_key = ? AND state IN ('error', 'ready')", (time.time(), key))
        finally:
            conn.close()
        self.wake.set()

    # ------------------------------------------------------------ reading

    def items(self, states=("ready", "working", "queued", "error"), since: float = 0) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT p.job_key, p.state, p.score, p.decision, p.reason, p.choice, p.reviewed, p.answers, p.error, "
                "p.created, p.updated, p.applied, p.instruction, j.company, j.title, j.location, j.url, j.apply_url, j.remote, "
                "t.id, t.pdf, t.result FROM prep p JOIN jobs j ON j.key = p.job_key "
                "LEFT JOIN tailored t ON t.id = p.tailored_id AND p.tailored_id != '' "
                f"WHERE p.state IN ({','.join('?' * len(states))}) AND p.updated >= ? "
                "ORDER BY CASE p.state WHEN 'ready' THEN 0 WHEN 'working' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END, "
                "p.score DESC LIMIT 300", (*states, since)).fetchall()
        finally:
            conn.close()
        cols = ("key", "state", "score", "decision", "reason", "choice", "reviewed", "answers", "note", "created",
                "updated", "applied", "instruction", "company", "title", "location", "url", "apply_url", "remote",
                "tailored_id", "pdf", "result")
        out = []
        for r in rows:
            it = dict(zip(cols, r))
            result = it.pop("result")
            it.update(reviewed=bool(it["reviewed"]), remote=bool(it["remote"]), answers=len(json.loads(it["answers"] or "[]")),
                      apply_url=it["apply_url"] or it["url"], tailored_id=it["tailored_id"] or "", pdf=it["pdf"] or "",
                      changes=change_summary(json.loads(result)) if result else None)
            out.append(it)
        return out

    def prepared(self, key: str) -> dict | None:
        """What the extension needs for one job's form."""
        conn = self.connect()
        try:
            row = conn.execute("SELECT p.state, p.choice, p.answers, t.pdf FROM prep p "
                               "LEFT JOIN tailored t ON t.id = p.tailored_id AND p.tailored_id != '' "
                               "WHERE p.job_key = ?", (key,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        state, choice, answers, pdf = row
        pdf_path = self.out_dir / pdf if pdf and choice == "tailored" else None
        return {"state": state, "choice": choice or "master",
                "pdf": pdf_path if pdf_path and pdf_path.exists() else None,
                "answers": json.loads(answers or "[]") if state in ("ready", "applied") else []}

    def next_ready(self, exclude: str = "") -> dict | None:
        nxt = [i for i in self.items(("ready",)) if i["key"] != exclude]
        return nxt[0] if nxt else None

    def summary(self) -> dict:
        s = self.settings()
        conn = self.connect()
        try:
            counts = dict(conn.execute("SELECT state, COUNT(*) FROM prep GROUP BY state").fetchall())
            used = self.usage(conn)
            corpus = self.corpus(conn)
        finally:
            conn.close()
        return {"settings": s, "counts": counts, "runs_today": used, "status": self.status,
                "paused": self.paused_until > time.time(), "current": self.current,
                "radar_updated": corpus[0] if corpus else None, "scoring": bool(self.scoring())}

    # ------------------------------------------------------------ thread

    def start(self) -> None:
        # A job left "working" by a helper that was stopped mid-run starts over.
        conn = self.connect()
        try:
            with conn:
                conn.execute("UPDATE prep SET state = 'queued' WHERE state = 'working'")
        finally:
            conn.close()

        def loop():
            while not self._stop:
                try:
                    self.step()
                except Exception:
                    self.log(f"prep loop failed:\n{traceback.format_exc()}")
                self.wake.wait(timeout=60)
                self.wake.clear()
        self._thread = threading.Thread(target=loop, name="prep", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        self.wake.set()
