"""Local helper for the dashboard: serves it at http://127.0.0.1:8765 and runs
resume tailoring jobs in the background. It also drafts answers to
application questions for the It gets easier Autofill browser extension, and
keeps the apply queue (prep.py): new jobs that match well get a tailored
resume and drafted answers before the user opens them.

Only this machine can reach it (bound to 127.0.0.1), and every write request
must come from the dashboard's own origin (or, for drafts, from the
extension), so other websites open in the same browser can't trigger
tailoring or read your resume.

Run:  pythonw server.py      (just the helper; app.py runs it with the tray and the scheduler)
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import ai
import appinfo
import ats
import config
import filters
import prep
import pricing
import radar
import resume_import
import resume_tex as rt
import scheduler
import tailor
import texengine

# JOBRADAR_PORT only for tests next to a running copy: the extension talks to 8765.
HOST, PORT = "127.0.0.1", int(os.environ.get("JOBRADAR_PORT") or 8765)
ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}
STORE = config.store()
DASHBOARD = STORE.path_for("dashboard")
RESUME_DIR = STORE.path_for("resume_dir")
MASTER = RESUME_DIR / "master.tex"
OUT_DIR = RESUME_DIR / "tailored"
DB_PATH = STORE.path_for("db")
LOG_PATH = STORE.path_for("logs_dir") / "server.log"
MAX_BODY = 2 * 1024 * 1024
FILE_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.(pdf|tex)$")
# Browser extensions' own pages; websites can't send these origins. The
# extension also sends X-JobRadar-Client, which no website can add without a
# CORS preflight, and this server never answers preflights.
EXT_ORIGIN = re.compile(r"^(moz|chrome)-extension://[A-Za-z0-9-]+$")
EXT_CLIENT = "autofill"

EXECUTOR = ThreadPoolExecutor(max_workers=2)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS tailored (
    id         TEXT PRIMARY KEY,
    job_key    TEXT NOT NULL,
    created    REAL NOT NULL,
    backend    TEXT NOT NULL,
    model      TEXT NOT NULL,
    pdf        TEXT NOT NULL,
    tex        TEXT NOT NULL,
    result     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tailored_job ON tailored(job_key, created);
"""


def log(msg: str) -> None:
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + msg + "\n")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.executescript(HISTORY_SCHEMA + prep.SCHEMA)
    prep.migrate(conn)
    return conn


PREP = prep.Preparer(db, RESUME_DIR, OUT_DIR, log, store=STORE)
# Runs radar.py. Run now works everywhere; timed runs only in the app (app.py).
SCHEDULER = scheduler.Scheduler(STORE, log, automatic=False)
# Set by app.py: {"autostart": bool, "update": {...} or None, "port_conflict": str}
APP_STATE: dict = {}


def job_row(key: str) -> dict | None:
    conn = db()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        desc = "description" if "description" in cols else "''"
        row = conn.execute(f"SELECT company, title, {desc}, url FROM jobs WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"company": row[0], "title": row[1], "description": row[2] or "", "url": row[3]}


def apply_url_candidates(page_url: str) -> list[str]:
    """apply_url values radar.py could have stored for the form at page_url."""
    u = urlparse(page_url)
    if u.scheme not in ("http", "https") or not u.netloc:
        return []
    host = u.netloc.lower()
    path = u.path.rstrip("/")
    q = parse_qs(u.query)
    out = []
    if host.endswith("greenhouse.io"):
        m = re.match(r"^/([^/]+)/jobs/(\d+)", path)
        board, jid = m.groups() if m else (None, None)
        if path.startswith("/embed/job_app"):
            board, jid = (q.get("for") or [None])[0], (q.get("token") or [None])[0]
        jid = jid or (q.get("gh_jid") or [None])[0]
        if board and jid:
            out.append(f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={jid}")
    if host.endswith(("myworkdayjobs.com", "myworkdaysite.com")):
        path = re.sub(r"^/[a-z]{2}-[A-Z]{2}(?=/)", "", path)        # locale prefix
        path = re.sub(r"/apply(/.*)?$", "", path)
    path = re.sub(r"/(apply|application|thanks)$", "", path)
    base = f"https://{host}{path}"
    return out + [base, base + "/apply", base + "/application"]


def job_by_url(page_url: str) -> dict | None:
    cands = apply_url_candidates(page_url)
    if not cands:
        return None
    conn = db()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        if "apply_url" not in cols:
            return None
        desc = "description" if "description" in cols else "''"
        row = conn.execute(f"SELECT company, title, {desc}, url, key FROM jobs WHERE apply_url IN "
                           f"({','.join('?' * len(cands))}) LIMIT 1", cands).fetchone()
    finally:
        conn.close()
    return {"company": row[0], "title": row[1], "description": row[2] or "", "url": row[3], "key": row[4]} if row else None


# --------------------------------------------------------------------------
# Settings and companies

RENDER = {"pending": False, "finished": 0.0, "error": "", "jobs": None}
RENDER_WAKE = threading.Event()


def schedule_render() -> None:
    """Rebuild dashboard.html from stored jobs (radar.rerender) in the
    background, so changed roles show without waiting for the next run.
    Requests that arrive while one is running coalesce into one more."""
    RENDER["pending"] = True
    RENDER_WAKE.set()


def _render_loop() -> None:
    while True:
        RENDER_WAKE.wait()
        RENDER_WAKE.clear()
        try:
            RENDER.update(jobs=radar.rerender(STORE), error="")
        except Exception as e:
            log(f"re-render failed:\n{traceback.format_exc()}")
            RENDER["error"] = f"{type(e).__name__}: {e}"
        RENDER.update(finished=time.time(), pending=RENDER_WAKE.is_set())


def settings_payload() -> dict:
    s = STORE.load()
    return {
        "settings": s, "warnings": STORE.warnings, "home": str(STORE.home), "file": str(STORE.path),
        "defaults": config.defaults(), "new_role": {**config.ROLE.default, "id": "", "name": "", "title_keywords": []},
        "choices": {
            # Starting points for the "Add role" form; they only fill in title keywords.
            "templates": [{"id": k, "label": label, "title_keywords": kw} for k, (label, kw) in filters.ROLE_TEMPLATES.items()],
            "countries": [{"code": c, "name": n} for c, (n, _) in filters.COUNTRIES.items()],
            "seniority": list(config.SENIORITY), "workplace": list(config.WORKPLACE),
            "visa": list(config.VISA_STATUS), "backends": list(config.TAILOR_BACKENDS),
            "draft_backends": list(config.DRAFT_BACKENDS), "sources": list(config.SOURCES),
            "strictness": list(config.STRICTNESS), "tones": list(tailor.voice.TONES),
            "max_samples": tailor.voice.MAX_SAMPLES, "sample_max": tailor.voice.SAMPLE_MAX,
        },
        "resumes": {r["id"]: master_meta(r["id"]) for r in s["roles"]},
        "render": RENDER,
        "ai": ai_payload(),
    }


def ai_payload() -> dict:
    """Paid API providers for the Settings page: models with prices, whether
    a key is saved (never the key), this month's spend, and what a typical
    run costs with the user's resume."""
    out = {}
    for prov in ai.PROVIDERS:
        out[prov] = {"name": pricing.PROVIDER_NAMES[prov], "key": ai.key_status(prov),
                     "spent": round(ai.spent(prov), 4),
                     "models": [{"id": m.id, "label": m.label, "input": m.input, "output": m.output}
                                for m in pricing.MODELS[prov]]}
    return {"providers": out, "prices_as_of": pricing.PRICES_AS_OF, "month": ai.this_month(),
            "typical": typical_costs(), "default_voice": list(tailor.voice.DEFAULT_BANNED)}


TYPICAL_JD = "x" * 5000       # a posting of usual length, for typical-cost estimates


def typical_costs() -> dict:
    """{backend: {"tailor": $, "draft": $}} for the API backends, from the
    master resume and a posting of usual length. Character-based token
    counts: quick, and no network."""
    if not MASTER.exists():
        return {}
    try:
        tex = MASTER.read_text(encoding="utf-8")
        job = {"company": "", "title": ""}
        out = {}
        for backend in pricing.API_BACKENDS:
            t = tailor.estimate_tailor(tex, job, TYPICAL_JD, backend, exact=False)
            d = tailor.estimate_draft(tex, job, "Why do you want to work here?", "", TYPICAL_JD, backend, exact=False)
            out[backend] = {"tailor": t["cost"], "draft": d["cost"], "model": t["model"]}
        return out
    except ValueError:
        return {}


def extension_info() -> dict:
    """Where the Autofill extension is, for the setup steps: store listings
    once they exist, else a signed .xpi shipped with the app, and the folder
    Chrome's Load unpacked needs."""
    folder = extension_dir()
    xpis = sorted(folder.glob("web-ext-artifacts/*.xpi"), key=lambda p: p.stat().st_mtime) if folder else []
    return {"firefox_store": appinfo.FIREFOX_ADDON_URL, "chrome_store": appinfo.CHROME_STORE_URL,
            "xpi": xpis[-1].name if xpis else None, "folder": str(folder) if folder else None}


def extension_dir() -> Path | None:
    """The unpacked extension. A packaged app keeps it inside its bundle,
    which a folder picker can't always reach (a macOS .app), so it is
    copied to the data folder whenever the bundled version changes."""
    src = Path(__file__).resolve().parent / "extension"
    if not (src / "manifest.json").exists():
        return None
    if not getattr(sys, "frozen", False):
        return src
    dest = STORE.home / "extension"
    try:
        same = (dest / "manifest.json").read_bytes() == (src / "manifest.json").read_bytes()
    except OSError:
        same = False
    if not same:
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("test", "node_modules"))
    return dest


def app_payload() -> dict:
    """The app's state for the dashboard's setup steps and status."""
    return {"name": appinfo.NAME, "version": appinfo.VERSION, "schedule": SCHEDULER.status(),
            "setup": STORE.section("setup"), "tex": texengine.status(), "extension": extension_info(),
            "backends": tailor.backend_status(), "data_folder": str(STORE.home), **APP_STATE}


def set_role_resume(rid: str, name: str) -> None:
    with STORE.lock:
        s = STORE.load()
        for r in s["roles"]:
            if r["id"] == rid:
                r["resume"] = name
        STORE.save(s)


def _name_key(source: str, token: str) -> str:
    """The key radar.py looks display names up by for a board."""
    return token.split(".")[0] if source == "workday" else token


GREENHOUSE_BOARD = re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)")


def seen_names() -> dict[str, str]:
    """Greenhouse boards name their company in each posting; the names
    stored jobs carry, by board token."""
    conn = db()
    try:
        rows = conn.execute("SELECT company, apply_url FROM jobs WHERE source = 'greenhouse'").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out = {}
    for company, url in rows:
        m = GREENHOUSE_BOARD.search(url or "")
        if m:
            out.setdefault(m.group(1), company)
    return out


def company_list() -> dict:
    builtin = json.loads(radar.COMPANIES_PATH.read_text(encoding="utf-8"))
    c = STORE.section("companies")
    names = {**seen_names(), **builtin.get("display_names", {}), **c["display_names"]}
    removed = set(c["removed"])
    rows = []
    for origin, boards in (("builtin", builtin), ("added", c["added"])):
        for src, toks in boards.items():
            if src not in config.SOURCES:
                continue
            for tok in toks:
                rows.append({"source": src, "token": tok, "origin": origin, "removed": f"{src}:{tok}" in removed,
                             "name": names.get(_name_key(src, tok)) or ats.default_name(ats.Board(src, tok))})
    rows.sort(key=lambda r: (r["name"].lower(), r["source"]))
    return {"boards": rows, "blocked": c["blocked"]}


def master_path(role: str = "") -> Path:
    """The master resume a role tailors from: its own, when it has one
    uploaded, else master.tex."""
    r = STORE.role(role) if role else None
    own = RESUME_DIR / r["resume"] if r and r["resume"] else None
    return own if own and own.exists() else MASTER


def master_meta(role: str = "") -> dict | None:
    path = master_path(role)
    if not path.exists():
        return None
    meta_path = path.with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta["own"] = path != MASTER
    try:
        res = rt.parse(path.read_text(encoding="utf-8"))
        meta.update(rt.summary(res))
    except ValueError as e:
        meta["error"] = str(e)
    return meta


# --------------------------------------------------------------------------
# Jobs

def run_job(jid: str, params: dict) -> None:
    def update(**kw):
        with JOBS_LOCK:
            JOBS[jid].update(kw)

    update(state="running", started=time.time())
    try:
        master = master_path(params.get("role", "")).read_text(encoding="utf-8")
        backend = params["backend"]
        meta = {"company": params.get("company", ""), "title": params.get("title", ""),
                "instruction": params.get("instruction", "")}
        result = tailor.tailor(master, meta, params["jd"], backend, OUT_DIR,
                               rewrite_bullets=params.get("rewrite", True),
                               manual_answer=params.get("answer"))
        conn = db()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO tailored (id, job_key, created, backend, model, pdf, tex, result) VALUES (?,?,?,?,?,?,?,?)",
                    (jid, params.get("key", ""), time.time(), backend, result["model"],
                     result["pdf"], result["tex"], json.dumps(result)))
        finally:
            conn.close()
        update(state="done", result=result)
    except (tailor.BackendError, rt.CompileError, ValueError) as e:
        update(state="error", error=str(e))
    except Exception as e:  # report, don't kill the worker
        log(f"job {jid} failed:\n{traceback.format_exc()}")
        update(state="error", error=f"Unexpected error: {e}")


def run_draft(jid: str, params: dict) -> None:
    """A draft for one question, or (with params["questions"]) answers for several."""
    def update(**kw):
        with JOBS_LOCK:
            JOBS[jid].update(kw)

    update(state="running", started=time.time())
    try:
        master = MASTER.read_text(encoding="utf-8")
        job = {"company": params["company"], "title": params["title"]}
        if "questions" in params:
            call = lambda: tailor.answer_questions(master, job, params["profile"], params["questions"],
                                                   params["jd"], params["backend"])
        else:
            call = lambda: tailor.draft_answer(master, job, params["question"], params["context"], params["jd"],
                                               params["backend"], params["max_chars"])
        result = call()
        update(state="done", result=result)
    except (tailor.BackendError, ValueError) as e:
        update(state="error", error=str(e))
    except Exception as e:  # report, don't kill the worker
        log(f"draft {jid} failed:\n{traceback.format_exc()}")
        update(state="error", error=f"Unexpected error: {e}")


def _register(**fields) -> str:
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        # Keep the in-memory table bounded; tailoring results also live in SQLite.
        if len(JOBS) > 200:
            for old in sorted(JOBS, key=lambda k: JOBS[k]["created"])[:100]:
                JOBS.pop(old, None)
        JOBS[jid] = {"id": jid, "state": "queued", "created": time.time(), **fields}
    return jid


def submit_draft(params: dict) -> str:
    jid = _register(kind="draft", backend=params["backend"])
    EXECUTOR.submit(run_draft, jid, params)
    return jid


def submit(params: dict) -> str:
    jid = _register(backend=params["backend"])
    EXECUTOR.submit(run_job, jid, params)
    return jid


# --------------------------------------------------------------------------
# HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "jobradar/1.0"

    def log_message(self, fmt, *args):  # quiet; errors go to server.log
        pass

    # -- helpers
    def _host_ok(self) -> bool:
        # Blocks DNS-rebinding: only our own host names are served.
        return self.headers.get("Host", "") in {f"{HOST}:{PORT}", f"localhost:{PORT}"}

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _err(self, status: int, msg: str) -> None:
        self._json(status, {"error": msg})

    def _origin_ok(self, allow_extension: bool = False) -> bool:
        origin = self.headers.get("Origin")
        if origin in ORIGINS:
            return True
        # Firefox may omit Origin on an extension's own requests.
        return (allow_extension and self.headers.get("X-JobRadar-Client") == EXT_CLIENT
                and (origin is None or bool(EXT_ORIGIN.match(origin))))

    def _body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._err(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large")
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._err(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return None
        if not isinstance(data, dict):
            self._err(HTTPStatus.BAD_REQUEST, "Expected a JSON object")
            return None
        return data

    # -- GET
    def do_GET(self):
        if not self._host_ok():
            return self._err(HTTPStatus.FORBIDDEN, "Bad host")
        url = urlparse(self.path)
        q = parse_qs(url.query)
        path = url.path

        if path in ("/", "/index.html", "/dashboard.html"):
            if not DASHBOARD.exists():
                return self._err(HTTPStatus.NOT_FOUND, "Run radar.py once to build the dashboard")
            return self._send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")

        if path == "/api/status":
            role = (q.get("role") or [""])[0][:40]
            return self._json(200, {"backends": tailor.backend_status(), "master": master_meta(role),
                                    "costs": typical_costs()})

        if path == "/api/settings":
            return self._json(200, settings_payload())

        if path == "/api/companies":
            return self._json(200, company_list())

        if path == "/api/render":
            return self._json(200, RENDER)

        if path == "/api/app":
            return self._json(200, app_payload())

        if path == "/api/extension.xpi":
            info = extension_info()
            if not info["xpi"]:
                return self._err(HTTPStatus.NOT_FOUND, "No signed extension in this build")
            f = extension_dir() / "web-ext-artifacts" / info["xpi"]
            return self._send(200, f.read_bytes(), "application/x-xpinstall",
                              {"Content-Disposition": f'attachment; filename="{info["xpi"]}"'})

        if path == "/api/about":
            return self._json(200, {"text": tailor.about_me(), "max": tailor.ABOUT_MAX})

        if path == "/api/usage":
            return self._json(200, ai.usage_summary())

        if path == "/api/job":
            row = job_row((q.get("key") or [""])[0])
            return self._json(200, row) if row else self._err(HTTPStatus.NOT_FOUND, "Unknown job")

        if path.startswith(("/api/tailor/", "/api/draft/")):
            jid = path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = dict(JOBS.get(jid) or {})
            return self._json(200, job) if job else self._err(HTTPStatus.NOT_FOUND, "Unknown job")

        if path == "/api/history":
            key = (q.get("key") or [""])[0]
            conn = db()
            try:
                rows = conn.execute(
                    "SELECT id, created, backend, model, pdf, tex, result FROM tailored "
                    "WHERE job_key = ? ORDER BY created DESC LIMIT 10", (key,)).fetchall()
            finally:
                conn.close()
            return self._json(200, [{"id": r[0], "created": r[1], "backend": r[2], "model": r[3],
                                     "pdf": r[4], "tex": r[5], "result": json.loads(r[6])} for r in rows])

        if path == "/api/queue":
            return self._json(200, {**PREP.summary(), "items": PREP.items(),
                                    # Recent applied/dismissed, so the dashboard's own marks catch up.
                                    "done": PREP.items(("applied", "dismissed"), since=time.time() - 14 * 86400)})

        if path in ("/api/prepared", "/api/queue/next"):
            if not self._origin_ok(allow_extension=True):
                return self._err(HTTPStatus.FORBIDDEN, "Only the It gets easier extension can ask for this")
            if path == "/api/queue/next":
                return self._json(200, {"next": PREP.next_ready((q.get("exclude") or [""])[0])})
            return self._prepared((q.get("url") or [""])[0][:2000])

        if path.startswith("/files/"):
            name = path[len("/files/"):]
            if not FILE_NAME.match(name):
                return self._err(HTTPStatus.BAD_REQUEST, "Bad file name")
            f = (OUT_DIR / name).resolve()
            if f.parent != OUT_DIR.resolve() or not f.exists():
                return self._err(HTTPStatus.NOT_FOUND, "Not found")
            if name.endswith(".pdf"):
                return self._send(200, f.read_bytes(), "application/pdf",
                                  {"Content-Disposition": f'inline; filename="{name}"'})
            return self._send(200, f.read_bytes(), "application/x-tex; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{name}"'})

        return self._err(HTTPStatus.NOT_FOUND, "Not found")

    # -- POST
    def do_POST(self):
        if not self._host_ok():
            return self._err(HTTPStatus.FORBIDDEN, "Bad host")
        path = urlparse(self.path).path
        if not self._origin_ok(allow_extension=path in ("/api/draft", "/api/answers", "/api/applied")):
            return self._err(HTTPStatus.FORBIDDEN, "Requests must come from the dashboard")
        data = self._body()
        if data is None:
            return

        if path == "/api/settings":
            return self._settings(data)
        if path == "/api/keys":
            return self._keys(data)
        if path == "/api/estimate":
            return self._estimate(data)
        if path.startswith("/api/companies/"):
            return self._companies(path.rsplit("/", 1)[-1], data)
        if path == "/api/draft":
            return self._draft(data)
        if path == "/api/answers":
            return self._answers(data)
        if path == "/api/applied":
            return self._applied(data)

        if path == "/api/scoring":
            if len(str(data.get("text", ""))) < 200:
                return self._err(HTTPStatus.BAD_REQUEST, "No resume text")
            PREP.save_scoring(data)
            return self._json(200, {"ok": True})
        if path == "/api/prep/settings":
            return self._json(200, PREP.save_settings(data))
        if path == "/api/queue/choice":
            choice = data.get("choice")
            if choice not in ("tailored", "master"):
                return self._err(HTTPStatus.BAD_REQUEST, "choice must be tailored or master")
            if not PREP.set_choice(str(data.get("key", "")), choice):
                return self._err(HTTPStatus.NOT_FOUND, "No tailored resume for this job")
            return self._json(200, {"ok": True})
        if path == "/api/queue/tailor":
            if not PREP.force_tailor(str(data.get("key", "")), str(data.get("instruction", ""))):
                return self._err(HTTPStatus.CONFLICT, "This job is already tailored or no longer in the queue")
            return self._json(200, {"ok": True})
        if path == "/api/queue/retry":
            PREP.retry(str(data.get("key", "")))
            return self._json(200, {"ok": True})
        if path == "/api/marks":
            # The dashboard's Applied / Dismiss marks, so the queue skips those jobs.
            marks = data.get("marks") if isinstance(data.get("marks"), dict) else {}
            n = PREP.mark_many({str(k)[:500]: v for k, v in list(marks.items())[:5000]
                                if v in ("applied", "dismissed", None)})
            return self._json(200, {"ok": True, "matched": n})

        if path == "/api/about":
            # The applicant's own notes on interests and motivations, used by
            # drafted answers. Kept in resume/, which never leaves this machine.
            tailor.save_about_me(str(data.get("text", "")))
            return self._json(200, {"ok": True})

        if path == "/api/app/run":
            if not SCHEDULER.run_now("dashboard"):
                return self._err(HTTPStatus.CONFLICT, "A job search is already running")
            return self._json(202, app_payload())
        if path == "/api/app/tectonic":
            # The user agreed in the dashboard to download Tectonic.
            if not texengine.tectonic_asset():
                return self._err(HTTPStatus.BAD_REQUEST, "Tectonic has no download for this computer")
            texengine.DOWNLOAD.start()
            return self._json(202, texengine.status())

        if path == "/api/master/outline":
            # Text read from a PDF or text resume, as an outline the user can correct.
            text = str(data.get("text", ""))[:200_000]
            if len(text.strip()) < 200:
                return self._err(HTTPStatus.BAD_REQUEST, "No resume text")
            return self._json(200, {"outline": resume_import.text_to_outline(text)})

        if path == "/api/master":
            tex = str(data.get("tex", ""))
            if "outline" in data:
                # A PDF or text resume, as the outline the user checked: made into a .tex master.
                try:
                    tex = resume_import.outline_to_tex(str(data["outline"])[:200_000])
                except ValueError as e:
                    return self._err(HTTPStatus.UNPROCESSABLE_ENTITY, str(e))
            # With a role, the resume becomes that role's own master.
            role = str(data.get("role") or "")[:40]
            if role and role not in {r["id"] for r in STORE.load()["roles"]}:
                return self._err(HTTPStatus.BAD_REQUEST, "Unknown role")
            # The PDF is kept next to the tailored ones, so the user can look at it.
            preview = OUT_DIR / (f"master-{role}-preview.pdf" if role else "master-preview.pdf")
            try:
                res = rt.parse(tex)
                pages = rt.compile_pdf(tex, preview)
            except (ValueError, rt.CompileError) as e:
                return self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {
                    "error": f"Couldn't use this resume: {e}",
                    "no_engine": texengine.pick(rt.engine_for(tex)) is None})
            RESUME_DIR.mkdir(parents=True, exist_ok=True)
            target = RESUME_DIR / f"master-{role}.tex" if role else MASTER
            target.write_text(tex, encoding="utf-8")
            target.with_suffix(".json").write_text(json.dumps({"name": str(data.get("name", "master.tex"))[:200],
                                                               "uploaded": time.time(), "pages": pages}), encoding="utf-8")
            if role:
                set_role_resume(role, target.name)
            PREP.poke()
            return self._json(200, {**rt.summary(res), "pages": pages, "name": data.get("name"),
                                    "preview": f"/files/{preview.name}"})

        if path in ("/api/tailor", "/api/prompt"):
            role = str(data.get("role") or "")[:40]
            if not master_path(role).exists():
                return self._err(HTTPStatus.CONFLICT, "Upload your master .tex resume first")
            key = str(data.get("key", ""))
            row = job_row(key) or {}
            jd = str(data.get("jd") or row.get("description") or "")
            params = {"key": key, "jd": jd, "company": row.get("company", data.get("company", "")),
                      "title": row.get("title", data.get("title", "")),
                      "backend": str(data.get("backend") or STORE.section("tailoring")["backend"]), "rewrite": bool(data.get("rewrite", True)),
                      "answer": data.get("answer"), "instruction": tailor.clean_instruction(data.get("instruction")),
                      "role": role}
            if path == "/api/prompt":
                try:
                    prompt = tailor.build_manual_prompt(master_path(role).read_text(encoding="utf-8"), params, jd)
                except ValueError as e:
                    return self._err(HTTPStatus.UNPROCESSABLE_ENTITY, str(e))
                return self._json(200, {"prompt": prompt})
            if params["backend"] not in set(tailor.BACKENDS) | {"manual"}:
                return self._err(HTTPStatus.BAD_REQUEST, "Unknown backend")
            if len(jd.strip()) < 200:
                return self._err(HTTPStatus.UNPROCESSABLE_ENTITY,
                                 "No job description saved for this posting. Paste it into the box first.")
            if params["instruction"] and params["backend"] == "chatgpt" and not PREP.reserve_run():
                return self._err(HTTPStatus.TOO_MANY_REQUESTS,
                                 "Today's ChatGPT runs for It gets easier are used up. Raise the limit in the apply queue settings, "
                                 "or pick another model.")
            return self._json(202, {"id": submit(params)})

        return self._err(HTTPStatus.NOT_FOUND, "Not found")

    def _settings(self, data: dict):
        """Saves settings ({"settings": {...}}, whole or in part). Anything
        invalid rejects the save with the list of problems."""
        patch = data.get("settings")
        if not isinstance(patch, dict):
            return self._err(HTTPStatus.BAD_REQUEST, "Expected {\"settings\": {...}}")
        patch.pop("version", None)
        try:
            STORE.update(patch)
        except config.SettingsError as e:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "Some settings aren't valid", "errors": e.errors})
        PREP.poke()
        if set(patch) - {"setup", "app"}:      # the setup steps' progress doesn't change the job list
            schedule_render()
        return self._json(200, settings_payload())

    def _keys(self, data: dict):
        """Save ({"provider", "key"}) or remove ({"provider", "key": ""}) an
        API key. The answer says only whether one is saved now."""
        provider = str(data.get("provider", ""))
        if provider not in ai.PROVIDERS:
            return self._err(HTTPStatus.BAD_REQUEST, "Unknown provider")
        key = str(data.get("key") or "")
        if not key.strip():
            ai.delete_key(provider)
            PREP.poke()
            return self._json(200, {"provider": provider, "key": ai.key_status(provider), "warning": ""})
        try:
            key = ai.clean_key(key)
        except ValueError as e:
            return self._err(HTTPStatus.BAD_REQUEST, str(e))
        problem = ai.verify_key(provider, key)
        warning = ""
        if problem == "connection":
            warning = "Saved, but it couldn't be checked: no connection to the provider."
        elif problem:
            return self._err(HTTPStatus.UNPROCESSABLE_ENTITY, problem)
        ai.save_key(provider, key)
        PREP.poke()
        return self._json(200, {"provider": provider, "key": ai.key_status(provider), "warning": warning})

    def _estimate(self, data: dict):
        """What tailoring one job would cost on a paid API, before running it.
        {"cost": null} for the subscription logins and manual paste."""
        backend = str(data.get("backend", ""))
        provider = pricing.API_BACKENDS.get(backend)
        if not provider:
            return self._json(200, {"cost": None})
        role = str(data.get("role") or "")[:40]
        if not master_path(role).exists():
            return self._err(HTTPStatus.CONFLICT, "Upload your master .tex resume first")
        key = str(data.get("key", ""))
        row = job_row(key) or {}
        jd = str(data.get("jd") or row.get("description") or "")
        job = {"company": row.get("company", data.get("company", "")), "title": row.get("title", data.get("title", "")),
               "instruction": tailor.clean_instruction(data.get("instruction"))}
        try:
            est = tailor.estimate_tailor(master_path(role).read_text(encoding="utf-8"), job, jd, backend)
        except ValueError as e:
            return self._err(HTTPStatus.UNPROCESSABLE_ENTITY, str(e))
        budget = ai.settings(provider)["monthly_budget"]
        spent = ai.spent(provider)
        return self._json(200, {**est, "budget": budget, "spent": round(spent, 4),
                                "over_budget": spent + est["cost"] > budget})

    def _companies(self, action: str, data: dict):
        c = STORE.section("companies")
        if action == "lookup":
            res = ats.lookup(str(data.get("url", ""))[:2000])
            if res.get("board"):
                tracked = next((b for b in company_list()["boards"]
                                if f"{b['source']}:{b['token']}" == res["board"]), None)
                res["tracked"] = bool(tracked and not tracked["removed"])
            return self._json(200, res)
        source, token = str(data.get("source", "")), str(data.get("token", "")).strip()[:300]
        if action in ("add", "remove", "restore"):
            if source not in config.SOURCES or not token:
                return self._err(HTTPStatus.BAD_REQUEST, "Unknown board")
            board = f"{source}:{token}"
            if action == "add":
                # Checked again here: only boards that answer are saved.
                res = ats.check(ats.Board(source, token))
                if not res["ok"]:
                    return self._err(HTTPStatus.UNPROCESSABLE_ENTITY, res["error"])
                if token not in c["added"].get(source, []) and not any(
                        b["source"] == source and b["token"] == token and b["origin"] == "builtin"
                        for b in company_list()["boards"]):
                    c["added"].setdefault(source, []).append(token)
                name = str(data.get("name") or "").strip()[:120]
                if name:
                    c["display_names"][_name_key(source, token)] = name
                c["removed"] = [r for r in c["removed"] if r != board]
            elif action == "remove":
                if token in c["added"].get(source, []):
                    c["added"][source] = [t for t in c["added"][source] if t != token]
                elif board not in c["removed"]:
                    c["removed"].append(board)
            else:
                c["removed"] = [r for r in c["removed"] if r != board]
        elif action in ("block", "unblock"):
            name = str(data.get("name", "")).strip()[:120]
            if not name:
                return self._err(HTTPStatus.BAD_REQUEST, "No company name")
            key = filters.norm_company(name)
            c["blocked"] = [b for b in c["blocked"] if filters.norm_company(b) != key]
            if action == "block":
                c["blocked"].append(name)
        else:
            return self._err(HTTPStatus.NOT_FOUND, "Not found")
        try:
            STORE.update({"companies": c})
        except config.SettingsError as e:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "Couldn't save that", "errors": e.errors})
        if action in ("block", "unblock"):
            schedule_render()
        return self._json(200, company_list())

    def _prepared(self, page_url: str):
        """The resume and drafted answers prepared for the job behind an
        application form. The resume is sent inline so the extension can
        attach it; without a prepared one it attaches its stored master."""
        job = job_by_url(page_url)
        if not job:
            return self._err(HTTPStatus.NOT_FOUND, "It gets easier doesn't know this job")
        p = PREP.prepared(job["key"]) or {"state": None, "choice": "master", "pdf": None, "answers": []}
        pdf = p.pop("pdf")
        return self._json(200, {"job": {"key": job["key"], "company": job["company"], "title": job["title"]},
                                **p, "resume": {"name": pdf.name, "data": base64.b64encode(pdf.read_bytes()).decode()}
                                if pdf else None})

    def _applied(self, data: dict):
        """The extension saw the ATS's "application received" page."""
        key = str(data.get("key", ""))[:500]
        if not key:
            job = job_by_url(str(data.get("url", ""))[:2000])
            key = job["key"] if job else ""
        if not key or not PREP.mark(key, "applied"):
            return self._err(HTTPStatus.NOT_FOUND, "It gets easier doesn't know this job")
        return self._json(200, {"ok": True, "key": key, "next": PREP.next_ready(key)})

    def _draft(self, data: dict):
        if not MASTER.exists():
            return self._err(HTTPStatus.CONFLICT, "Upload your master .tex resume in the dashboard first")
        backend = str(data.get("backend", "chatgpt"))
        if backend not in set(tailor.DRAFT_BACKENDS) | {"manual"}:
            return self._err(HTTPStatus.BAD_REQUEST, "Drafting works with " + ", ".join(tailor.DRAFT_BACKENDS) + " or manual")
        question = str(data.get("question", "")).strip()[:2000]
        if not question:
            return self._err(HTTPStatus.BAD_REQUEST, "No question to answer")
        row = job_by_url(str(data.get("url", ""))[:2000]) or {}
        try:
            max_chars = max(0, min(int(data.get("max_chars") or 0), 20000))
        except (TypeError, ValueError):
            max_chars = 0
        params = {"backend": backend, "question": question, "max_chars": max_chars,
                  "context": str(data.get("context", ""))[:2000],
                  "company": row.get("company") or str(data.get("company", ""))[:200],
                  "title": row.get("title") or str(data.get("title", ""))[:300],
                  "jd": row.get("description") or str(data.get("jd", ""))[:30000]}
        if backend == "manual":
            prompt = tailor.build_draft_prompt(MASTER.read_text(encoding="utf-8"),
                                               {"company": params["company"], "title": params["title"]},
                                               question, params["context"], params["jd"], max_chars)
            return self._json(200, {"prompt": prompt})
        est = None
        if backend in pricing.API_BACKENDS:
            est = tailor.estimate_draft(MASTER.read_text(encoding="utf-8"), {"company": params["company"], "title": params["title"]},
                                        question, params["context"], params["jd"], backend, max_chars, exact=False)
        return self._json(202, {"id": submit_draft(params), "estimate": est and est["cost"]})

    def _answers(self, data: dict):
        """Answers for the questions autofill couldn't fill; poll /api/draft/<id>."""
        if not MASTER.exists():
            return self._err(HTTPStatus.CONFLICT, "Upload your master .tex resume in the dashboard first")
        backend = str(data.get("backend", "chatgpt"))
        if backend not in tailor.DRAFT_BACKENDS:
            return self._err(HTTPStatus.BAD_REQUEST, "Answering works with " + ", ".join(tailor.DRAFT_BACKENDS))
        questions = []
        for q in (data.get("questions") or [])[:60]:
            if not isinstance(q, dict) or not str(q.get("question", "")).strip():
                continue
            try:
                max_chars = max(0, min(int(q.get("max_chars") or 0), 20000))
            except (TypeError, ValueError):
                max_chars = 0
            questions.append({"id": str(q.get("id", ""))[:20], "question": str(q["question"])[:1500],
                              "kind": str(q.get("kind", "text"))[:20], "max_chars": max_chars,
                              "context": str(q.get("context", ""))[:800],
                              "options": [str(o)[:300] for o in (q.get("options") or [])[:200]]})
        if not questions:
            return self._err(HTTPStatus.BAD_REQUEST, "No questions to answer")
        profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        row = job_by_url(str(data.get("url", ""))[:2000]) or {}
        params = {"backend": backend, "questions": questions,
                  "profile": {k: str(v)[:300] for k, v in profile.items() if k in tailor.PROFILE_FOR_ANSWERS},
                  "company": row.get("company") or str(data.get("company", ""))[:200],
                  "title": row.get("title") or str(data.get("title", ""))[:300],
                  "jd": row.get("description") or str(data.get("jd", ""))[:30000]}
        return self._json(202, {"id": submit_draft(params)})


def prepare() -> None:
    """Folders, settings and a first dashboard page, before serving."""
    if not STORE.path.exists():
        # A new install: its settings are written before anything else
        # creates files here, so it isn't taken for an older install that
        # has already done the setup steps (config._v3).
        STORE.save(STORE.load(), strict=False)
    for d in (RESUME_DIR, OUT_DIR, DB_PATH.parent, LOG_PATH.parent):
        d.mkdir(parents=True, exist_ok=True)
    STORE.load()
    for w in STORE.warnings:
        log(f"settings: {w}")
    if not DASHBOARD.exists():
        radar.rerender(STORE)


def bind() -> ThreadingHTTPServer:
    """Raises OSError when the port is taken."""
    return ThreadingHTTPServer((HOST, PORT), Handler)


def start_background() -> None:
    threading.Thread(target=_render_loop, name="render", daemon=True).start()
    PREP.start()
    SCHEDULER.start()


def main() -> int:
    prepare()
    try:
        httpd = bind()
    except OSError as e:
        log(f"could not bind {HOST}:{PORT}: {e}")
        return 1
    log(f"serving http://{HOST}:{PORT}")
    start_background()
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
