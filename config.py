"""User settings: one JSON file in the user's data directory.

Every module reads its choices from here instead of its own constants:
the user's roles (radar.py tags jobs with the ones they match), the visa
features each turns on, the user's own company boards, match-score weights
(match.py and the dashboard), tailoring backends, strictness and the apply
queue (tailor.py, prep.py), API models and budgets (ai.py), the writing
voice of drafted answers, and where data files live.

The file is checked against SCHEMA on every load. A value of the wrong type
falls back to its default (and is reported), numbers outside their range are
clamped, unknown keys are dropped. Defaults reproduce how Onward behaved
before settings existed, so an install without a settings file runs exactly
as before. Each file carries a version; MIGRATIONS bring older files up to
date when they are loaded.

Data directory, first match wins:
  1. $JOBRADAR_HOME
  2. the code folder, when it already holds settings.json or jobs.db
     (installs from before the data directory existed keep working in place)
  3. the platform's per-user data folder: %LOCALAPPDATA%\\JobRadar on
     Windows, ~/Library/Application Support/JobRadar on macOS,
     $XDG_DATA_HOME/jobradar (~/.local/share/jobradar) elsewhere
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import filters
import pricing
import voice

CODE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = "settings.json"
VERSION = 6


def platform_data_dir() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "JobRadar"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "JobRadar"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "jobradar"


def default_home() -> Path:
    env = os.environ.get("JOBRADAR_HOME")
    if env:
        return Path(env).expanduser()
    if (CODE_DIR / SETTINGS_FILE).exists() or (CODE_DIR / "jobs.db").exists():
        return CODE_DIR
    return platform_data_dir()


# --------------------------------------------------------------------------
# Schema
#
# A small declarative validator (the project has no third-party
# dependencies). Each node knows its default and cleans a value into a valid
# one, appending "path: problem" to `errors` for anything it had to replace.

class Node:
    default: Any

    def clean(self, value, path: str, errors: list[str]):
        raise NotImplementedError

    def fresh(self):
        return copy.deepcopy(self.default)


class Bool(Node):
    def __init__(self, default: bool) -> None:
        self.default = default

    def clean(self, value, path, errors):
        if isinstance(value, bool):
            return value
        errors.append(f"{path}: expected true or false")
        return self.default


class Num(Node):
    """A number clamped to [lo, hi]; `integer` rounds it."""

    def __init__(self, default: float, lo: float, hi: float, integer: bool = False) -> None:
        self.default, self.lo, self.hi, self.integer = default, lo, hi, integer

    def clean(self, value, path, errors):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if isinstance(value, str):
                try:
                    value = float(value.strip())
                except ValueError:
                    value = None
            if not isinstance(value, (int, float)):
                errors.append(f"{path}: expected a number")
                return self.default
        if value != value:      # NaN
            errors.append(f"{path}: expected a number")
            return self.default
        out = max(self.lo, min(self.hi, value))
        return int(round(out)) if self.integer else float(out)


def Int(default: int, lo: int, hi: int) -> Num:
    return Num(default, lo, hi, integer=True)


class Str(Node):
    def __init__(self, default: str = "", max_len: int = 200, choices: tuple[str, ...] | None = None,
                 pattern: str | None = None) -> None:
        self.default, self.max_len, self.choices = default, max_len, choices
        self.pattern = re.compile(pattern) if pattern else None

    def clean(self, value, path, errors):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            errors.append(f"{path}: expected text")
            return self.default
        value = value.strip()[:self.max_len]
        if self.choices is not None and value not in self.choices:
            errors.append(f"{path}: must be one of {', '.join(self.choices)}")
            return self.default
        if self.pattern and not self.pattern.fullmatch(value):
            errors.append(f"{path}: not a valid value")
            return self.default
        return value


class List(Node):
    """A list of items. Text given for a list of strings is split on commas,
    so "Boston, Seattle" works too. Empty strings and duplicates are dropped."""

    def __init__(self, item: Node, default: list | None = None, max_items: int = 100) -> None:
        self.item, self.default, self.max_items = item, default or [], max_items

    def clean(self, value, path, errors):
        if isinstance(value, str) and isinstance(self.item, Str):
            value = value.split(",")
        if not isinstance(value, list):
            errors.append(f"{path}: expected a list")
            return self.fresh()
        if len(value) > self.max_items:
            errors.append(f"{path}: at most {self.max_items} items")
        out = []
        for i, v in enumerate(value[:self.max_items]):
            item_errors: list[str] = []
            c = self.item.clean(v, f"{path}[{i}]", item_errors)
            if item_errors:
                errors.extend(item_errors)
                # An invalid word is dropped; an object keeps its valid fields.
                if not isinstance(self.item, Obj):
                    continue
            if c == "" or c in out:
                continue
            out.append(c)
        return out


class Map(Node):
    """A dict with free-form keys (company display names, tokens per source)."""

    def __init__(self, value: Node, key_pattern: str = r"[^\x00-\x1f]{1,200}", max_items: int = 2000,
                 default: dict | None = None) -> None:
        self.value, self.key = value, re.compile(key_pattern)
        self.max_items, self.default = max_items, default or {}

    def clean(self, value, path, errors):
        if not isinstance(value, dict):
            errors.append(f"{path}: expected an object")
            return self.fresh()
        out = {}
        for k, v in list(value.items())[:self.max_items]:
            if not isinstance(k, str) or not self.key.fullmatch(k):
                errors.append(f"{path}: bad key {k!r}")
                continue
            out[k] = self.value.clean(v, f"{path}.{k}", errors)
        return out


class Obj(Node):
    def __init__(self, fields: dict[str, Node], check: Callable[[dict, str, list[str]], None] | None = None) -> None:
        self.fields, self.check = fields, check

    @property
    def default(self):
        return {k: f.fresh() for k, f in self.fields.items()}

    def fresh(self):
        return self.default

    def clean(self, value, path, errors):
        if not isinstance(value, dict):
            errors.append(f"{path}: expected an object")
            return self.default
        out = {}
        for k, f in self.fields.items():
            out[k] = f.clean(value[k], f"{path}.{k}" if path else k, errors) if k in value else f.fresh()
        if self.check:
            self.check(out, path, errors)
        return out


# --------------------------------------------------------------------------
# Choices shared with the modules that use them

SENIORITY = ("intern", "entry", "mid", "senior")
WORKPLACE = ("onsite", "hybrid", "remote")
# opt: on F-1 OPT or STEM OPT (sponsorship, H-1B and OPT signals all matter);
# sponsorship: needs H-1B or other sponsorship; authorized: citizen, permanent
# resident or otherwise free to work, so no visa signals are shown.
VISA_STATUS = ("opt", "sponsorship", "authorized")
# chatgpt and claude-code run on the user's subscription login; openai-api and
# claude-api bill the user's own API key (ai.py), within a monthly budget.
TAILOR_BACKENDS = ("chatgpt", "claude-code", "openai-api", "claude-api", "manual")
DRAFT_BACKENDS = ("chatgpt", "claude-code", "openai-api", "claude-api")
# strict: bullets keep their wording except for the job's terms; reword: they may
# be rephrased freely. Both reject new skills, numbers and job-only terms.
STRICTNESS = ("strict", "reword")
SETUP_STEPS = ("welcome", "resume", "profile", "role", "ai", "extension", "fetch")
SOURCES = ("greenhouse", "lever", "ashby", "workday", "smartrecruiters", "oracle", "eightfold", "amazon", "icims")

ID = r"[a-z0-9][a-z0-9-]{0,39}"
TIME = r"([01]?\d|2[0-3]):[0-5]\d"
WORD = Str(max_len=80)


def _role_check(r: dict, path: str, errors: list[str]) -> None:
    if not filters.TitleRules(r["title_keywords"]).rules:
        errors.append(f"{path}.title_keywords: add at least one title keyword")
    if not r["seniority"]:
        errors.append(f"{path}.seniority: pick at least one level")
        r["seniority"] = ["entry"]
    if not r["workplace"]:
        errors.append(f"{path}.workplace: pick at least one of on-site, hybrid, remote")
        r["workplace"] = list(WORKPLACE)
    if not r["name"]:
        r["name"] = r["id"]


ROLE = Obj({
    "id": Str(filters.DEFAULT_ROLE["id"], 40, pattern=ID),
    "name": Str(filters.DEFAULT_ROLE["name"], 80),
    # Lines read top to bottom (filters.TitleRules); the first one a title fits decides.
    "title_keywords": List(Str(max_len=1000), list(filters.SOFTWARE_KEYWORDS), 100),
    "seniority": List(Str(choices=SENIORITY), ["entry"], 4),
    # ISO 3166 alpha-2 codes (filters.COUNTRIES); empty means any country.
    "countries": List(Str(max_len=2, pattern=r"[A-Z]{2}"), ["US"], 40),
    "cities": List(WORD, [], 50),
    "workplace": List(Str(choices=WORKPLACE), list(WORKPLACE), 3),
    # Yearly pay floor, in the posting's own currency. Only postings that
    # state pay are held to it; 0 means no floor.
    "salary_min": Int(0, 0, 10_000_000),
    "must_have": List(WORD, [], 30),            # the posting must mention every one
    "exclude": List(WORD, [], 50),              # anywhere in the posting
    "companies_allow": List(Str(max_len=120), [], 500),
    "companies_block": List(Str(max_len=120), [], 500),
    # A master .tex in the resume folder for this role; "" uses master.tex.
    "resume": Str("", 120, pattern=r"([A-Za-z0-9_.-]+\.tex)?"),
    "visa": Obj({
        "status": Str("opt", choices=VISA_STATUS),
        "hide_no_sponsorship": Bool(False),     # start with "Hide no sponsorship" on
    }),
}, _role_check)


def _roles_check(s: dict, path: str, errors: list[str]) -> None:
    seen: set[str] = set()
    roles = []
    for r in s["roles"]:
        if r["id"] in seen:
            errors.append(f"roles: duplicate id {r['id']!r}")
            continue
        seen.add(r["id"])
        roles.append(r)
    s["roles"] = roles or [ROLE.default]
    if s["active_role"] not in seen:
        s["active_role"] = s["roles"][0]["id"]


QUEUE = Obj({
    "enabled": Bool(True),
    "threshold": Int(60, 0, 100),               # minimum match score to prepare a job
    "daily_cap": Int(25, 0, 200),               # model runs a day for the queue (tailoring and drafts each count one)
    "backend": Str("chatgpt", choices=DRAFT_BACKENDS),
    "skip_coverage": Int(85, 0, 100),           # use the master when it already covers this % of required keywords
    "needs_sponsorship": Bool(True),            # leave out postings that say no sponsorship or no OPT
    "locations": List(WORD, [], 30),            # substrings of the job location; empty means anywhere
    "remote_ok": Bool(True),                    # remote jobs pass the location filter
    "lookback_hours": Int(48, 1, 24 * 14),      # how far back "newly seen" reaches
    "max_age_days": Int(7, 1, 60),              # only postings this recent are prepared, however new to the radar
    "draft_answers": Bool(True),
    "notify_desktop": Bool(True),
    "ntfy_server": Str("https://ntfy.sh", pattern=r"https?://[A-Za-z0-9.:-]+/?"),
    "ntfy_topic": Str("", 64, pattern=r"[A-Za-z0-9_-]{0,64}"),
    "quiet_start": Str("22:00", 5, pattern=TIME),
    "quiet_end": Str("08:00", 5, pattern=TIME),
})

SCHEMA = Obj({
    "version": Int(VERSION, 0, 1_000_000),
    # Relative paths are inside the data directory.
    "paths": Obj({
        "db": Str("jobs.db", 500),
        "resume_dir": Str("resume", 500),
        "data_dir": Str("data", 500),           # downloaded datasets (USCIS H-1B exports)
        "dashboard": Str("dashboard.html", 500),
        "logs_dir": Str("", 500),
    }),
    "radar": Obj({
        # How old a posting may be and still be shown. Separate from fetch_days:
        # boards like Greenhouse return every open job, however long it has
        # been up, and a role up for months has most likely been filled.
        "dashboard_days": Int(30, 1, 3650),
        # Hiring programs ("New Grad 2027", "Entry Level - University Grad")
        # take applications for a whole season, so they're shown longer.
        "program_days": Int(60, 1, 3650),
        # How far back paged sources (Workday, Oracle, the SimplifyJobs feed)
        # read each run. Deeper means many more requests per run.
        "fetch_days": Int(7, 1, 60),
        "stale_after_days": Int(2, 1, 30),      # unseen for this long means the posting was closed
        "purge_after_days": Int(45, 7, 3650),   # rows older than this are deleted
        "workers": Int(24, 1, 64),
        "timeout": Int(25, 5, 120),
        "simplify_feed": Bool(True),            # SimplifyJobs New-Grad listings
        "interval_minutes": Int(30, 10, 1440),  # how often the app fetches
    }),
    "companies": Obj({
        # Boards the user added on top of companies.json, per source.
        "added": Map(List(Str(max_len=300), [], 2000), key_pattern="|".join(SOURCES)),
        "removed": List(Str(max_len=320), [], 2000),       # "<source>:<token>" from companies.json to skip
        "blocked": List(Str(max_len=120), [], 2000),       # company names never shown or fetched
        "display_names": Map(Str(max_len=120)),
    }),
    "roles": List(ROLE, [ROLE.default], 30),
    "active_role": Str(filters.DEFAULT_ROLE["id"], 40, pattern=ID),    # shown first in the dashboard
    # Match score = coverage x (1 - experience_weight + experience_weight x experience fit).
    # Same formula in match.py and the dashboard's scoreJob.
    "scoring": Obj({
        "experience_weight": Num(0.4, 0, 1),
        "prior_keywords": Num(3, 0, 20),        # short checklists drift toward the middle
        "prior_credit": Num(0.5, 0, 1),
        "years_penalty": Num(0.35, 0, 1),       # per year short of what the posting asks
        "min_experience_fit": Num(0.15, 0, 1),
        "unstated_years_fit": Num(0.85, 0, 1),
        "unknown_keyword_weight": Num(0.8, 0, 3),
    }),
    "tailoring": Obj({
        "backend": Str("chatgpt", choices=TAILOR_BACKENDS),     # the tailor dialog's default
        "draft_backend": Str("chatgpt", choices=DRAFT_BACKENDS),
        "target_pages": Int(1, 1, 3),
        "strictness": Str("strict", choices=STRICTNESS),
    }),
    # The user's own API keys (stored by ai.py, never here): model and monthly
    # budget in US dollars. A budget of 0 blocks every paid call.
    "providers": Obj({p: Obj({
        "model": Str(pricing.default_model(p), 100, choices=pricing.model_ids(p)),
        "monthly_budget": Num(5, 0, 10_000),
    }) for p in pricing.MODELS}),
    # How drafted answers should sound (tailor.VOICE).
    "voice": Obj({
        "tone": Str("casual", choices=voice.TONES),
        "samples": List(Str(max_len=voice.SAMPLE_MAX), [], voice.MAX_SAMPLES),
        "banned_phrases": List(Str(max_len=120), list(voice.DEFAULT_BANNED), 300),
    }),
    "queue": QUEUE,
    "ui": Obj({"advanced": Bool(False)}),
    # The first-run steps in the dashboard. Scheduled fetches wait until
    # they are done or skipped; `step` is where a closed window picks up.
    "setup": Obj({
        "done": Bool(False),
        "step": Str("welcome", choices=SETUP_STEPS),
    }),
    "app": Obj({
        "check_updates": Bool(True),            # once a day, against the project's GitHub releases
        "skip_version": Str("", 40),            # an update the user said not to offer again
    }),
    # Messages for the user about changes made to their settings, shown once
    # in the dashboard until dismissed.
    "notices": List(Str(max_len=500), [], 20),
}, _roles_check)


def defaults() -> dict:
    return SCHEMA.default


def validate(data) -> tuple[dict, list[str]]:
    errors: list[str] = []
    clean = SCHEMA.clean(data if isinstance(data, dict) else {}, "", errors)
    return clean, errors


# --------------------------------------------------------------------------
# Migrations: MIGRATIONS[n] turns a version n-1 file into version n.

def _v1(data: dict, home: Path) -> dict:
    """Version 0 is an install from before settings.json. The apply queue kept
    its settings in resume/prep.json; they move here."""
    legacy = home / "resume" / "prep.json"
    if "queue" not in data and legacy.exists():
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            data["queue"] = {k: v for k, v in raw.items() if k in QUEUE.fields}
    return data


def _v2(data: dict, home: Path) -> dict:
    """Search profiles with preset role families become roles with their own
    title keywords (each family's keywords, so they match as before), and the
    local Ollama backend, which was removed, becomes ChatGPT."""
    families = {k: kw for k, (_, kw) in filters.ROLE_TEMPLATES.items()}
    for f in data.pop("role_families", None) or []:
        if isinstance(f, dict) and f.get("id"):
            inc, exc = [w for w in f.get("include") or [] if w], [w for w in f.get("exclude") or [] if w]
            families[f["id"]] = (["- " + "/".join(exc)] if exc else []) + (["/".join(inc)] if inc else [])
    if "profiles" in data:
        roles = []
        for p in data.pop("profiles") or []:
            if not isinstance(p, dict):
                continue
            lines = []
            if p.get("exclude_title"):
                lines.append("- " + "/".join(p["exclude_title"]))
            for fam in p.get("roles") or ["software"]:
                lines += [l for l in families.get(fam, []) if l not in lines]
            role = {k: v for k, v in p.items() if k not in ("roles", "exclude_title")}
            roles.append({**role, "title_keywords": lines or list(filters.SOFTWARE_KEYWORDS)})
        data["roles"] = roles
    if "active_profile" in data:
        data["active_role"] = data.pop("active_profile")

    notices = list(data.get("notices") or [])
    moved = False
    for section, key in (("tailoring", "backend"), ("tailoring", "draft_backend"), ("queue", "backend")):
        sec = data.get(section)
        if isinstance(sec, dict) and sec.get(key) == "ollama":
            sec[key] = "chatgpt"
            moved = True
    if isinstance(data.get("tailoring"), dict):
        data["tailoring"].pop("ollama_url", None)
        data["tailoring"].pop("ollama_model", None)
    if moved:
        notices.append("The local Ollama model is no longer part of Onward, so tailoring and drafting "
                       "that used it now use ChatGPT. You can pick Claude instead in Settings.")
    data["notices"] = notices
    return data


def _v3(data: dict, home: Path) -> dict:
    """Setup steps arrived. Anyone already using Onward has done
    what they cover, so only a new install is shown them."""
    used = any(k in data for k in ("paths", "radar", "companies", "roles", "scoring", "tailoring", "queue", "ui")) \
        or (home / "jobs.db").exists() or (home / "resume" / "master.tex").exists()
    if used:
        data["setup"] = {**(data.get("setup") if isinstance(data.get("setup"), dict) else {}), "done": True}
    return data


def _v4(data: dict, home: Path) -> dict:
    """API models moved from tailoring.claude_model to one entry per provider."""
    t = data.get("tailoring")
    model = t.pop("claude_model", None) if isinstance(t, dict) else None
    if model and model != pricing.default_model("anthropic"):
        if model in pricing.model_ids("anthropic"):
            prov = data.setdefault("providers", {})
            if isinstance(prov, dict):
                prov.setdefault("anthropic", {})["model"] = model
        else:
            data["notices"] = list(data.get("notices") or []) + [
                f"The Claude API model {model} isn't offered any more, so Claude API runs now use "
                f"{pricing.default_model('anthropic')}. You can pick another in Settings, under AI models."]
    return data


def _v5(data: dict, home: Path) -> dict:
    """dashboard_days split into what's shown (now a year, so "All open"
    really means every open posting) and how far back paged sources fetch
    (fetch_days, 7). Someone on the old default of 7 gets the new one; a
    value they chose themselves is kept."""
    r = data.get("radar")
    if isinstance(r, dict) and r.get("dashboard_days") == 7:
        r["dashboard_days"] = 365
    return data


def _v6(data: dict, home: Path) -> dict:
    """A year of open postings was mostly roles filled long ago: the default
    is 30 days now, with 60 for new-grad and entry-level programs
    (program_days). A value the user chose themselves is kept."""
    r = data.get("radar")
    if isinstance(r, dict) and r.get("dashboard_days") == 365:
        r["dashboard_days"] = 30
    return data


MIGRATIONS: dict[int, Callable[[dict, Path], dict]] = {1: _v1, 2: _v2, 3: _v3, 4: _v4, 5: _v5, 6: _v6}


def migrate(data: dict, home: Path) -> tuple[dict, bool]:
    """(data at VERSION, whether anything changed). A file from a newer
    version is left as it is: unknown fields are ignored, known ones used."""
    data = dict(data)
    v = data.get("version", 0)
    if not isinstance(v, int) or v < 0:
        v = 0
    changed = False
    while v < VERSION:
        v += 1
        data = MIGRATIONS[v](data, home)
        data["version"] = v
        changed = True
    return data, changed


# --------------------------------------------------------------------------
# Store

class SettingsError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class Store:
    """settings.json, cached until the file changes on disk. radar.py runs
    as its own process, so writes are atomic (temp file + rename)."""

    def __init__(self, path: Path, home: Path | None = None) -> None:
        self.path = Path(path)
        self.home = Path(home) if home else self.path.parent
        self.lock = threading.RLock()
        self._data: dict | None = None
        self._stamp: tuple[int, int] | None = None
        self.warnings: list[str] = []

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def load(self) -> dict:
        """A deep copy of the current settings."""
        with self.lock:
            stamp = self._file_stamp()
            if self._data is None or stamp != self._stamp:
                self._data = self._read()
                self._stamp = self._file_stamp()
            return copy.deepcopy(self._data)

    def _read(self) -> dict:
        raw: dict = {}
        self.warnings = []
        exists = self.path.exists()
        if exists:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                self.warnings.append(f"{self.path.name} could not be read ({e}); using defaults")
                raw = {}
            if not isinstance(raw, dict):
                self.warnings.append(f"{self.path.name} is not a JSON object; using defaults")
                raw = {}
        newer = isinstance(raw.get("version"), int) and raw["version"] > VERSION
        raw, migrated = migrate(raw, self.home)
        data, errors = validate(raw)
        self.warnings += errors
        if newer:
            self.warnings.append(f"{self.path.name} is from a newer version of Onward (version {raw['version']}); "
                                 "it is used read-only")
            data["version"] = raw["version"]
        elif migrated and exists:
            self._write(data)       # migrate once, not on every load
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def save(self, data: dict, strict: bool = True) -> dict:
        """Replace all settings. With strict, any invalid value rejects the
        whole save (SettingsError); otherwise invalid values become defaults."""
        clean, errors = validate({**data, "version": VERSION})
        if errors and strict:
            raise SettingsError(errors)
        with self.lock:
            cur = self.load()
            if cur.get("version", VERSION) > VERSION:
                raise SettingsError([f"{self.path.name} is from a newer version of Onward; not overwriting it"])
            self._write(clean)
            self._data, self._stamp = clean, self._file_stamp()
        return copy.deepcopy(clean)

    def update(self, patch: dict, strict: bool = True) -> dict:
        """Merge `patch` into the current settings (objects merge, everything
        else, lists included, is replaced) and save."""
        with self.lock:
            return self.save(merge(self.load(), patch), strict)

    def section(self, name: str) -> dict:
        return self.load()[name]

    # ---------------------------------------------------------------- paths

    def path_for(self, name: str) -> Path:
        """A configured path ("db", "resume_dir", ...) made absolute."""
        p = Path(self.load()["paths"][name] or ".").expanduser()
        return p if p.is_absolute() else self.home / p

    def role(self, rid: str | None = None) -> dict:
        s = self.load()
        rid = rid or s["active_role"]
        return next((r for r in s["roles"] if r["id"] == rid), s["roles"][0])

    def resume_for(self, rid: str | None = None) -> Path:
        """The master .tex a role tailors from."""
        name = self.role(rid)["resume"] if rid else ""
        return self.path_for("resume_dir") / (name or "master.tex")


def merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


_STORE: Store | None = None
_STORE_LOCK = threading.Lock()


def store() -> Store:
    """The settings of this install, in default_home()."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            home = default_home()
            _STORE = Store(home / SETTINGS_FILE, home)
        return _STORE


def use(s: Store | None) -> None:
    """Point every module at another store (tests, or a second data dir)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = s
