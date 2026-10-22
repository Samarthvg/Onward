"""Onward: pulls entry-level US software engineering postings straight from
company applicant tracking systems, keeps a SQLite history, and renders a
static dashboard of the freshest roles.

Usage:
    python radar.py            fetch, store, render dashboard.html
    python radar.py --check    list board tokens that no longer resolve
    python radar.py --render   rebuild dashboard.html from stored jobs

Only one run fetches at a time (radar.lock in the data folder); a run that
finds another one going exits with SKIPPED. With --status-file, a run keeps
a small JSON file of its progress for the app's scheduler to show.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import email.utils
import gzip
import os
import math
import html
import json
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Callable
from pathlib import Path

import config
import feed
import filters
import oslock
import reqparse
# Title and location rules live in filters.py with the user's roles.
from filters import (ENTRY_HINT, NON_US, NOT_ENTRY, REMOTE, US_CITIES, US_MARKER,  # noqa: F401
                     US_STATE_NAMES, US_STATES, is_entry_title,
                     is_swe_title, us_location)

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "jobs.db"
DASHBOARD_PATH = ROOT / "dashboard.html"
TEMPLATE_PATH = ROOT / "dashboard_template.html"
COMPANIES_PATH = ROOT / "companies.json"
LOG_PATH = ROOT / "radar.log"
SKILLS_PATH = ROOT / "skills.json"
H1B_DIR = ROOT / "data"
H1B_YEARS = (2023, 2022)    # newest bulk exports USCIS publishes without a login
H1B_URL = "https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-{year}.csv"

SIMPLIFY_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/"
    ".github/scripts/listings.json"
)
SIMPLIFY_VERIFY_URL = "https://github.com/SimplifyJobs/New-Grad-Positions/blob/dev/.github/scripts/listings.json"

DASHBOARD_DAYS = 30         # postings older than this are not shown
PROGRAM_DAYS = 60           # nor new-grad and entry-level programs older than this
FETCH_DAYS = 7              # how far back paged sources (Workday, Oracle, the feed) read
STALE_AFTER_DAYS = 2        # unseen for this long means the posting was closed
PURGE_AFTER_DAYS = 45       # rows older than this are deleted
WORKERS = 24
TIMEOUT = 25
USER_AGENT = "Onward/1.0 (personal job search)"


# --------------------------------------------------------------------------
# Data model

@dataclass
class Job:
    key: str
    company: str
    title: str
    location: str
    remote: bool
    url: str
    source: str
    posted_at: float | None     # epoch seconds, as reported by the source
    posted_exact: bool          # False when the source only gave an update time
    sponsorship: str            # "no", "yes" or "unknown"
    no_opt: bool                # posting refuses STEM OPT / I-983 / CPT
    sponsor_note: str
    min_yoe: int | None
    entry_hint: bool
    apply_url: str              # the application form itself, not the description page
    verify_url: str             # raw source record, so the timestamp can be checked by hand
    posted_field: str           # name of the source field posted_at came from
    posted_raw: str             # that field's value, exactly as the source sent it
    skills: str                 # JSON {"t": title, "r": required, "p": preferred}
    description: str            # plain text, for resume tailoring
    # Key the job had as a bare SimplifyJobs listing, when it was then read
    # from its own posting (whose title and location give it a new key).
    listing_key: str = ""
    country: str = ""               # the source's own country field, when it sends one
    pay_min: float | None = None    # yearly pay the posting states
    pay_max: float | None = None
    roles: str = "[]"               # JSON list of the user's roles that want it


# --------------------------------------------------------------------------
# Settings
#
# The constants above are the defaults; configure() sets them from the
# user's settings.json, along with their roles.

SEARCH = filters.Search()
SETTINGS: dict = config.defaults()


def configure(store: config.Store) -> None:
    global DB_PATH, DASHBOARD_PATH, LOG_PATH, H1B_DIR, DASHBOARD_DAYS, PROGRAM_DAYS, FETCH_DAYS, STALE_AFTER_DAYS, PURGE_AFTER_DAYS
    global WORKERS, TIMEOUT, ORACLE_DATE_FACET, SEARCH, SETTINGS
    s = SETTINGS = store.load()
    DB_PATH = store.path_for("db")
    DASHBOARD_PATH = store.path_for("dashboard")
    LOG_PATH = store.path_for("logs_dir") / "radar.log"
    H1B_DIR = store.path_for("data_dir")
    r = s["radar"]
    DASHBOARD_DAYS, STALE_AFTER_DAYS, PURGE_AFTER_DAYS = r["dashboard_days"], r["stale_after_days"], r["purge_after_days"]
    PROGRAM_DAYS = r["program_days"]
    FETCH_DAYS = r["fetch_days"]
    WORKERS, TIMEOUT = r["workers"], r["timeout"]
    ORACLE_DATE_FACET = 7 if FETCH_DAYS <= 7 else 30
    SEARCH = filters.Search(s["roles"], s["companies"]["blocked"])


def load_companies(settings: dict, discovered: dict[str, list[str]] | None = None,
                   discovered_names: dict[str, str] | None = None) -> dict:
    """companies.json, plus the boards the user added and the ones discovery
    tracks (discover.tracked), minus the ones the user removed or whose
    company they blocked."""
    companies = json.loads(COMPANIES_PATH.read_text(encoding="utf-8"))
    c = settings["companies"]
    for src, toks in [*c["added"].items(), *(discovered or {}).items()]:
        companies[src] = list(dict.fromkeys(list(companies.get(src, [])) + toks))
    names = companies.setdefault("display_names", {})
    for tok, name in (discovered_names or {}).items():
        names.setdefault(tok, name)
    names.update(c["display_names"])
    removed = set(c["removed"])
    blocked = {filters.norm_company(b) for b in c["blocked"]}
    names = companies["display_names"]
    for src, toks in companies.items():
        if src in FETCHERS:
            companies[src] = [t for t in toks if f"{src}:{t}" not in removed
                              and filters.norm_company(names.get(t) or t) not in blocked]
    return companies


# --------------------------------------------------------------------------
# Filters

YOE = re.compile(
    r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s*\+?\s*(?:\(\d+\)\s*)?"
    r"(?:-|to|–)?\s*(?:\d{1,2}\s*)?\+?\s*(?:or more\s+)?(?:additional\s+)?years?"
    r"(?:\s+of)?(?:[^.\n]{0,60}?)\bexperience",
    re.I,
)
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10}

NO_SPONSOR = re.compile(
    r"(not|unable to|cannot|can ?not|won'?t|will not|does not|do not|doesn'?t|don'?t|"
    r"is not able to|are not able to|not able to|no)\s+(?:currently\s+|be\s+)?"
    r"(?:able to\s+)?(?:offer|provide|support|sponsor)\w*\s*(?:any\s+)?"
    r"(?:employment\s+)?(?:visa|immigration|work authorization|h-?1b)?\s*sponsor\w*"
    r"|without (?:the )?(?:need for |requiring )?(?:current or future )?"
    r"(?:visa |employer |immigration )?sponsorship"
    r"|no (?:visa |immigration )?sponsorship"
    r"|sponsorship (?:is )?not (?:available|offered|provided)"
    r"|must be (?:a )?u\.?s\.? (?:citizen|person)"
    r"|u\.?s\.? citizenship (?:is )?required"
    r"|requires? (?:an? )?(?:active )?(?:secret|top secret|ts/sci|security) clearance"
    r"|(?:active|current) (?:secret|top secret|ts/sci) clearance"
    r"|\bitar\b|export control\w* (?:laws|regulations).{0,80}u\.?s\.? person"
    # "does not sponsor/support H-1B petitions", "will not provide support for visas"
    r"|(?:not|unable to|cannot|won'?t|doesn'?t|don'?t)\s+(?:\w+\s+){0,3}?"
    r"(?:sponsor|support)\w*(?:\s*/\s*\w+)?\s+(?:\w+\s+){0,4}?"
    r"(?:h-?1b|visas?|immigration|work (?:permits?|authorization)|petitions?)"
    r"|authorized to work for any employer"
    r"|not (?:be )?eligible for (?:visa |immigration )?sponsorship"
    r"|sponsorship (?:will|is|cannot|can ?not) (?:not )?be (?:provided|offered|available|considered)",
    re.I,
)
# STEM OPT needs an employer to sign Form I-983; some postings refuse it
# separately from H-1B, which matters to anyone working on OPT.
NO_OPT = re.compile(
    r"(?:not|no|unable to|cannot|won'?t)\b[^.\n]{0,80}\b(?:stem[- ]opt|\bopt\b|i-983|\bcpt\b)"
    r"|(?:stem[- ]opt|i-983)[^.\n]{0,40}\bnot (?:be )?(?:supported|accepted|eligible|available)",
    re.I,
)
YES_SPONSOR = re.compile(
    r"(visa|immigration|h-?1b) sponsorship (?:is )?(?:available|offered|provided)"
    r"|(?:we|will|can|do|are able to) (?:offer|provide|support) (?:visa |immigration |h-?1b )?sponsorship"
    r"|sponsorship (?:is )?available"
    r"|\bwe (?:will |can |do )?sponsor (?:h-?1b|visas?|work visas?)",
    re.I,
)

def min_years(text: str) -> int | None:
    years = []
    for m in YOE.finditer(text):
        g = m.group(1).lower()
        n = NUMBER_WORDS.get(g) or (int(g) if g.isdigit() else None)
        if n is not None and n < 20:
            years.append(n)
    return min(years) if years else None


def sponsorship(text: str) -> tuple[str, str]:
    m = NO_SPONSOR.search(text)
    if m:
        return "no", _context(text, m)
    m = YES_SPONSOR.search(text)
    if m:
        return "yes", _context(text, m)
    return "unknown", ""


def _context(text: str, m: re.Match) -> str:
    start, end = max(0, m.start() - 80), min(len(text), m.end() + 80)
    return re.sub(r"\s+", " ", text[start:end]).strip()


TAG = re.compile(r"<[^>]+>")


BLOCK_TAG = re.compile(r"<\s*(?:br|/li|/p|/h[1-6]|/div|/ul|/ol|tr)\b[^>]*>", re.I)
LI_TAG = re.compile(r"<\s*li\b[^>]*>", re.I)


def strip_html(s: str | None) -> str:
    """HTML -> text, keeping one line per paragraph and marking list items
    with "• " so requirement bullets stay identifiable for match scoring."""
    if not s:
        return ""
    s = html.unescape(html.unescape(s))
    s = LI_TAG.sub("\n• ", s)
    s = BLOCK_TAG.sub("\n", s)
    s = TAG.sub(" ", s)
    s = re.sub(r"[ \t\r\f\v\xa0]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s)
    return re.sub(r"\n•\s*\n", "\n• ", s).strip()


def parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def norm_key(company: str, title: str, location: str) -> str:
    def n(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return f"{n(company)}|{n(title)}|{n(location)}"


SKILL_BOUNDARY = "(?<![A-Za-z0-9+#])(?:{})(?![A-Za-z0-9+#])"
SKILLS = [
    (s["n"], re.compile(SKILL_BOUNDARY.format(s["re"]), 0 if s.get("cs") else re.I))
    for s in json.loads(SKILLS_PATH.read_text(encoding="utf-8"))
]
TAIL_CUT = re.compile(
    r"\b(benefits|perks|equal (?:employment )?opportunity|compensation|pay range|"
    r"salary range|base salary|pay transparency|what we offer)\b", re.I)
PREFERRED = re.compile(
    r"\b(preferred|nice[- ]to[- ]haves?|bonus points|bonus|pluses|good to have|"
    r"desired qualifications|extra credit)\b", re.I)


def find_skills(text: str) -> set[str]:
    return {name for name, rx in SKILLS if rx.search(text)} if text else set()


def split_sections(description: str) -> tuple[str, str]:
    """(required, preferred) text. Benefits and legal boilerplate at the tail
    are cut so they don't add noise."""
    desc = description or ""
    cut = TAIL_CUT.search(desc, int(len(desc) * 0.4))
    if cut:
        desc = desc[:cut.start()]
    pref = PREFERRED.search(desc, int(len(desc) * 0.2))
    return (desc[:pref.start()], desc[pref.start():]) if pref else (desc, "")


def extract_skills(title: str, description: str) -> dict[str, list[str]]:
    """Title skills weigh most, then required, then preferred."""
    req_text, pref_text = split_sections(description)
    t = find_skills(title)
    r = find_skills(req_text) - t
    p = find_skills(pref_text) - t - r
    return {"t": sorted(t), "r": sorted(r), "p": sorted(p)}


# --------------------------------------------------------------------------
# Funnel: where postings drop out, per source, each run
#
# Every posting a fetcher looks at is counted once as "seen", then at the
# first rule that drops it: "title" (no role's title keywords fit), "level"
# (keywords fit but not the seniority), "location", or "details" (what only
# the description tells: years of experience, must-have and excluded words,
# pay). The rest are "kept". Seen minus all of those is postings that closed
# or fell outside the fetch window between the listing and the detail record.
# Each count is kept twice: over everything seen, and over postings dated
# within the last 7 days, which is what decides how many new jobs a week
# the radar can find.

FUNNEL_STAGES = ("seen", "title", "level", "location", "details", "kept")
FRESH_DAYS = 7


class Funnel:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.counts: dict[tuple[str, str], list[int]] = {}

    def add(self, source: str, stage: str, fresh: bool) -> None:
        with self.lock:
            c = self.counts.setdefault((source, stage), [0, 0])
            c[0] += 1
            c[1] += int(fresh)

    def rows(self) -> list[tuple[str, str, int, int]]:
        with self.lock:
            return [(src, stage, a, f) for (src, stage), (a, f) in sorted(self.counts.items())]

    def totals(self, fresh: bool = False) -> dict[str, int]:
        out = dict.fromkeys(FUNNEL_STAGES, 0)
        for _, stage, a, f in self.rows():
            out[stage] += f if fresh else a
        return out


FUNNEL = Funnel()


def is_fresh(posted_at: float | None) -> bool:
    return posted_at is not None and posted_at >= time.time() - FRESH_DAYS * 86400


def screen(source: str, title: str, location: str | None = None, country: str | None = None,
           remote: bool = False, fresh: bool = False) -> bool:
    """The listing-only checks (title, level, and location when the listing
    gives one), counted in the funnel. Fetchers call it before paying for a
    posting's details; build_job() calls it for postings that came whole."""
    FUNNEL.add(source, "seen", fresh)
    stage = SEARCH.title_stage(title)
    if stage is None and location is not None and not SEARCH.location_wanted(location, country, remote):
        stage = "location"
    if stage:
        FUNNEL.add(source, stage, fresh)
        return False
    return True


def build_job(*, company, title, location, country, remote, url, source,
              posted_at, posted_exact, description, apply_url, verify_url,
              posted_field, posted_raw, sponsor_hint=None, screened: bool = False,
              via: str | None = None) -> Job | None:
    """screened: the fetcher already passed the title through screen().
    via: the funnel source to count under, when it differs from source
    (a Simplify listing read from its own posting)."""
    title = (title or "").strip()
    location = (location or "").strip()
    company = (company or "").strip()
    src, fresh = via or source, is_fresh(posted_at)
    if not title:
        return None
    if not screened:
        if not screen(src, title, location, country, remote, fresh):
            return None
    elif not SEARCH.location_wanted(location, country, remote):
        FUNNEL.add(src, "location", fresh)
        return None
    yoe = min_years(description)
    entry = bool(ENTRY_HINT.search(title))
    pay = filters.pay_range(description)
    roles = SEARCH.matching(filters.Facts(company, title, location, country, bool(remote or REMOTE.search(location)),
                                             description or "", yoe, entry, pay))
    if not roles:
        FUNNEL.add(src, "details", fresh)
        return None
    FUNNEL.add(src, "kept", fresh)
    spons, note = sponsorship(description)
    # "e.g." / "i.e." would end the sentence early for the [^.] window.
    no_opt = bool(NO_OPT.search(re.sub(r"\b(e\.g|i\.e)\.", "eg", description or "", flags=re.I)))
    if spons == "unknown" and sponsor_hint:
        spons, note = sponsor_hint
    return Job(
        key=norm_key(company, title, location),
        company=company,
        title=title,
        location=location or ("Remote" if remote else ""),
        remote=bool(remote or REMOTE.search(location)),
        url=url,
        source=source,
        posted_at=posted_at,
        posted_exact=posted_exact,
        sponsorship=spons,
        no_opt=no_opt,
        sponsor_note=note,
        min_yoe=yoe,
        entry_hint=entry,
        apply_url=apply_url or url,
        verify_url=verify_url,
        posted_field=posted_field,
        posted_raw=str(posted_raw) if posted_raw is not None else "",
        skills=json.dumps(extract_skills(title, description), separators=(",", ":")),
        description=(description or "")[:20000],
        country=(country or "").strip()[:60],
        pay_min=pay[0] if pay else None,
        pay_max=pay[1] if pay else None,
        roles=json.dumps(roles),
    )


# --------------------------------------------------------------------------
# HTTP
#
# Every request goes through one gate per host: requests to a host start at
# least its interval apart (HOST_INTERVAL, or the robots.txt Crawl-delay when
# that is longer), a 429/503 pauses the whole host for as long as it asked,
# and robots.txt rules are honored. Different hosts still run in parallel, so
# politeness costs little wall time.

class BoardMissing(Exception):
    """404/410: the board, or the posting, no longer exists."""


class Throttled(Exception):
    """The host kept answering 429, or asked for a longer pause than a run can wait."""


class Disallowed(Exception):
    """The host's robots.txt does not allow the request."""


DEFAULT_INTERVAL = 1.0          # unknown hosts, e.g. company career pages
HOST_INTERVAL = {               # seconds between request starts, matched by host suffix
    "raw.githubusercontent.com": 0.0,
    "boards-api.greenhouse.io": 0.1,
    "api.ashbyhq.com": 0.1,
    "api.smartrecruiters.com": 0.25,
    "myworkdayjobs.com": 0.5,
    "myworkdaysite.com": 0.5,
    "oraclecloud.com": 0.5,
    "eightfold.ai": 3.0,        # Eightfold throttles bursts: 429s for ~20s after a few quick requests
    "careers.microsoft.com": 3.0,
}
RETRY_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
MAX_RETRY_WAIT = 30             # a longer Retry-After ends that host for this run
THROTTLE_RETRIES = 2            # extra attempts after a 429, which usually clears within seconds

# Hosts whose robots.txt speaks to crawlers while the API itself is published
# for programmatic use. api.smartrecruiters.com disallows "*", but its Posting
# API is documented for listing a company's public postings without a key:
# https://developers.smartrecruiters.com/docs/posting-api
ROBOTS_EXEMPT = {"api.smartrecruiters.com"}
ROBOTS_TTL = 86400
ROBOTS: dict[str, tuple[float, str]] = {}      # netloc -> (fetched, robots.txt body), kept in the db
ROBOTS_FETCHED: set[str] = set()                # netlocs whose entry changed this run


class HostGate:
    def __init__(self, interval: float) -> None:
        self.lock = threading.Lock()
        self.interval = interval
        self.next_at = 0.0          # monotonic time the next request may start
        self.paused_until = 0.0

    def wait(self, url: str) -> None:
        with self.lock:
            now = time.monotonic()
            if self.paused_until - now > MAX_RETRY_WAIT:
                raise Throttled(f"{url} (host asked to pause {self.paused_until - now:.0f}s)")
            start = max(now, self.next_at, self.paused_until)
            self.next_at = start + self.interval
        # time.sleep can wake a little early (Windows clocks tick every ~15 ms),
        # so keep sleeping until the slot really starts: a site's crawl delay
        # is a promise, not an estimate.
        while (left := start - time.monotonic()) > 0:
            time.sleep(left)

    def pause(self, seconds: float) -> None:
        with self.lock:
            self.paused_until = max(self.paused_until, time.monotonic() + seconds)


class Robots:
    """robots.txt as RFC 9309 reads it: the groups naming our product token,
    else the "*" groups; the longest matching rule wins and Allow wins ties;
    "*" and a trailing "$" work in patterns."""

    # The product token, and the one sent before the rename: a site's rules
    # written for that still apply.
    AGENTS = ("onward", "jobradar")

    def __init__(self, body: str) -> None:
        groups: list[tuple[list[str], list[tuple[bool, str]], list[float]]] = []
        in_agents = False
        for raw in body.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            field, value = (s.strip() for s in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if not in_agents:
                    groups.append(([], [], []))
                groups[-1][0].append(value.lower())
                in_agents = True
                continue
            in_agents = False
            if not groups:
                continue
            if field in ("allow", "disallow") and value:
                groups[-1][1].append((field == "allow", value))
            elif field == "crawl-delay":
                try:
                    groups[-1][2].append(float(value))
                except ValueError:
                    pass
        chosen = ([g for g in groups if any(a in g[0] for a in self.AGENTS)]
                  or [g for g in groups if "*" in g[0]])
        self.rules = [(allow, _robots_pattern(p), len(p)) for g in chosen for allow, p in g[1]]
        self.delay = max((d for g in chosen for d in g[2]), default=None)

    def allowed(self, path: str) -> bool:
        best: tuple[int, bool] | None = None
        for allow, rx, length in self.rules:
            if rx.match(path) and (best is None or length > best[0] or (length == best[0] and allow)):
                best = (length, allow)
        return best is None or best[1]


def _robots_pattern(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = re.escape(pattern[:-1] if anchored else pattern).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


_GATES: dict[str, HostGate] = {}
_ROBOTS_PARSED: dict[str, Robots] = {}
_HOST_LOCKS: dict[str, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()


def _host_lock(netloc: str) -> threading.Lock:
    with _REGISTRY_LOCK:
        return _HOST_LOCKS.setdefault(netloc, threading.Lock())


def gate_for(netloc: str) -> HostGate:
    with _REGISTRY_LOCK:
        gate = _GATES.get(netloc)
        if gate is None:
            host = netloc.split(":")[0]
            interval = next((sec for suffix, sec in HOST_INTERVAL.items()
                             if host == suffix or host.endswith("." + suffix)), DEFAULT_INTERVAL)
            gate = _GATES[netloc] = HostGate(interval)
        return gate


def robots_for(scheme: str, netloc: str) -> Robots:
    with _host_lock(netloc):
        parsed = _ROBOTS_PARSED.get(netloc)
        if parsed is None:
            hit = ROBOTS.get(netloc)
            if hit and time.time() - hit[0] < ROBOTS_TTL:
                body = hit[1]
            else:
                body = _fetch_robots(scheme, netloc)
                if body is not None:
                    ROBOTS[netloc] = (time.time(), body)
                    ROBOTS_FETCHED.add(netloc)
            parsed = _ROBOTS_PARSED[netloc] = Robots(body or "")
            if parsed.delay:
                gate = gate_for(netloc)
                gate.interval = max(gate.interval, parsed.delay)
        return parsed


def _fetch_robots(scheme: str, netloc: str) -> str | None:
    """robots.txt body; "" when the site has none. None when it couldn't be
    read: the request then goes ahead and robots.txt is tried again next run."""
    url = f"{scheme}://{netloc}/robots.txt"
    gate_for(netloc).wait(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = _read(resp)
            # Some sites answer with their HTML home page; that holds no rules.
            if not resp.headers.get("Content-Type", "text/plain").startswith("text/plain"):
                return ""
            return body.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return "" if 400 <= e.code < 500 else None     # RFC 9309: 4xx means no rules
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _read(resp) -> bytes:
    raw = resp.read()
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
        raw = gzip.decompress(raw)
    return raw


def retry_after(value: str | None) -> float | None:
    """Seconds to wait from a Retry-After header (delay-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - time.time())


def fetch(url: str, body: dict | None = None, *, accept: str = "application/json",
          retries: int = 3) -> bytes:
    parts = urllib.parse.urlsplit(url)
    netloc = parts.netloc
    if parts.hostname not in ROBOTS_EXEMPT:
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if not robots_for(parts.scheme, netloc).allowed(path):
            raise Disallowed(url)
    gate = gate_for(netloc)
    headers = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "gzip"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    delay = 2.0
    attempt = 0
    while True:
        attempt += 1
        gate.wait(url)
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return _read(resp)
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                raise BoardMissing(url) from e
            if e.code not in RETRY_STATUS:
                raise
            asked = retry_after(e.headers.get("Retry-After"))
            # Without a Retry-After, back off from at least twice the host's own spacing.
            wait = asked if asked is not None else max(delay, 2 * gate.interval)
            delay = wait * 2
            # The pause holds every request to this host, not just this one.
            gate.pause(wait)
            limit = retries + THROTTLE_RETRIES if e.code == 429 else retries
            if wait > MAX_RETRY_WAIT or attempt >= limit:
                if e.code == 429 or asked is not None:
                    raise Throttled(f"{netloc} answered {e.code}"
                                    + (f" with Retry-After {asked:.0f}s" if asked is not None else "")) from e
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt >= retries:
                raise
            time.sleep(delay)
            delay *= 2


def get_json(url: str, retries: int = 3, body: dict | None = None):
    return json.loads(fetch(url, body, retries=retries).decode("utf-8"))


def post_json(url: str, body: dict, retries: int = 3):
    return get_json(url, retries, body)


def get_text(url: str) -> str:
    return fetch(url, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8").decode(
        "utf-8", "replace")


_ONCE: dict[str, object] = {}


def get_json_once(url: str):
    """For a feed several fetchers read in one run (an Ashby board that is
    both in companies.json and behind Simplify listings): fetched once."""
    with _host_lock("once:" + url):
        if url not in _ONCE:
            try:
                _ONCE[url] = get_json(url)
            except Exception as e:      # remembered too, so a dead board is asked once
                _ONCE[url] = e
        hit = _ONCE[url]
    if isinstance(hit, Exception):
        raise hit
    return hit


# --------------------------------------------------------------------------
# Posting detail cache
#
# Detail records cost one request per posting. Each is fetched once and kept
# in the details table under the posting's URL, including postings that turned
# out to be closed or to have no machine-readable record, so no run asks for
# them again.

DETAILS: dict[str, dict] = {}
NEW_DETAILS: dict[str, dict] = {}
_DETAILS_LOCK = threading.Lock()
GONE = {"gone": True}                   # 404/410 at the source: the posting closed
UNAVAILABLE = {"unavailable": True}     # page exists but carries no posting record


def cached_posting(key: str, load: Callable[[], dict | None]) -> dict:
    """build_job() keyword arguments (minus company) for one posting, or GONE / UNAVAILABLE."""
    hit = DETAILS.get(key)
    if hit is not None:
        return hit
    try:
        posting = load() or UNAVAILABLE
    except BoardMissing:
        posting = GONE
    with _DETAILS_LOCK:
        DETAILS[key] = NEW_DETAILS[key] = posting
    return posting


def is_posting(p: dict | None) -> bool:
    return bool(p) and "title" in p


def _day_start(raw: str | None) -> float | None:
    """Local midnight of the date a date-only field names ("2026-10-21...")."""
    try:
        return datetime.strptime((raw or "")[:10], "%Y-%m-%d").timestamp()
    except ValueError:
        return None


_CLOCK = re.compile(r"T(\d{2}):(\d{2})(?::(\d{2}))?")


def _has_clock_time(raw: str | None) -> bool:
    """Some sources write a bare date as midnight in some zone
    ("2026-02-27T05:00:00.000Z" is midnight Eastern). A real clock time almost
    never lands on :00:00, so only a nonzero minute or second counts as exact."""
    m = _CLOCK.search(raw or "")
    return bool(m and (int(m.group(2)) or int(m.group(3) or 0)))


def _dated(raw: str | None) -> tuple[float | None, bool]:
    """(posted_at, posted_exact) for an ISO field that may be a bare date."""
    if _has_clock_time(raw):
        return parse_iso(raw), True
    return _day_start(raw), False


def _sections(*parts: tuple[str, str | None]) -> str:
    """Join (heading, html) pairs into one description with the headings kept
    on their own lines, so reqparse reads each part as the section it is."""
    out = []
    for heading, body in parts:
        text = strip_html(body)
        if text and text.upper() != "UNAVAILABLE":
            out.append(f"{heading}\n{text}" if heading else text)
    return "\n".join(out)


# --------------------------------------------------------------------------
# Sources
#
# Each posting is turned into build_job() keyword arguments by one function
# per source (_greenhouse_posting, ...). Board fetchers and Simplify listing
# enrichment share them, so a posting reads the same whichever way it came.
#
# Big Tech careers sites left out on purpose (their Simplify listings stay
# listing-only):
#   Apple   jobs.apple.com: Apple's Website Terms of Use forbid robots, spiders
#           and page-scraping of its sites.
#   Google  robots.txt disallows /about/careers/applications/jobs/results, and
#           Google's terms bind automated access to robots.txt.
#   Meta    metacareers.com robots.txt: automated collection is prohibited
#           without written permission.
# LinkedIn's user agreement forbids scraping, so its job pages are skipped too.
NO_AUTOMATED_ACCESS = ("jobs.apple.com", "google.com", "metacareers.com", "facebook.com", "linkedin.com")

ISO3 = {"US": "USA", "CA": "CAN", "GB": "GBR", "IE": "IRL", "DE": "DEU", "FR": "FRA", "NL": "NLD", "ES": "ESP",
        "IN": "IND", "SG": "SGP", "JP": "JPN", "AU": "AUS", "IL": "ISR", "PL": "POL", "MX": "MEX", "BR": "BRA"}


def country_param(name: str, codes: dict[str, str] | None = None) -> dict[str, str]:
    """A search source's country filter, when every role wants one
    country; otherwise none, and the roles filter the results."""
    only = SEARCH.only_country()
    value = (codes or {}).get(only) if codes is not None else filters.COUNTRIES[only][0] if only else None
    return {name: value} if only and value else {}


def skipped_commitments(always: set[str], intern: set[str]) -> set[str]:
    """Employment types a board fetcher drops before reading postings:
    part-time and contract always, internships unless a role wants them."""
    return always if SEARCH.wants_level("intern") else always | intern


def _greenhouse_posting(token: str, j: dict) -> dict:
    first = parse_iso(j.get("first_published"))
    jid = j.get("id")
    return dict(
        title=j.get("title"),
        location=(j.get("location") or {}).get("name", ""),
        country=None,
        remote=False,
        url=j.get("absolute_url"),
        source="greenhouse",
        posted_at=first or parse_iso(j.get("updated_at")),
        posted_exact=first is not None,
        description=strip_html(j.get("content")),
        apply_url=f"https://job-boards.greenhouse.io/embed/job_app?for={token}&token={jid}",
        verify_url=f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{jid}",
        posted_field="first_published" if first else "updated_at",
        posted_raw=j.get("first_published") or j.get("updated_at"),
    )


def fetch_greenhouse(token: str) -> list[Job]:
    """The board's list without descriptions is about a tenth of the size of
    the one with them, which counts at a thousand boards. Descriptions are
    read once per posting that passes the listing checks (cached_posting);
    titles, locations and dates come from the live list every run."""
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    out = []
    for j in data.get("jobs", []):
        live = _greenhouse_posting(token, j)
        if not screen("greenhouse", live["title"] or "", live["location"], fresh=is_fresh(live["posted_at"])):
            continue
        detail = cached_posting(live["verify_url"], lambda: _greenhouse_posting(token, get_json(live["verify_url"])))
        if not is_posting(detail):
            continue
        job = build_job(company=j.get("company_name") or token, **{**live, "description": detail["description"]},
                        screened=True)
        if job:
            out.append(job)
    return out


def _lever_posting(token: str, j: dict, api: str = "https://api.lever.co") -> dict:
    cats = j.get("categories") or {}
    locs = cats.get("allLocations") or [cats.get("location") or ""]
    created = j.get("createdAt")
    # Lever keeps requirement lists apart from the body, each with its own
    # heading ("Core Requirements", "Preferred"). They go in with those
    # headings and before the closing text (salary, benefits), so the
    # requirement parser reads them as the sections they are.
    lists = "\n".join(f"{l.get('text') or ''}:\n{strip_html(l.get('content'))}"
                      for l in j.get("lists") or [])
    return dict(
        title=j.get("text"),
        location=" / ".join(l for l in locs if l),
        country=j.get("country"),
        remote=(j.get("workplaceType") == "remote"),
        url=j.get("hostedUrl"),
        source="lever",
        posted_at=created / 1000 if created else None,
        posted_exact=created is not None,
        description="\n".join(filter(None, [j.get("descriptionPlain"), lists, j.get("additionalPlain")])),
        apply_url=j.get("applyUrl"),
        verify_url=f"{api}/v0/postings/{token}/{j.get('id')}",
        posted_field="createdAt",
        posted_raw=created,
    )


def fetch_lever(token: str) -> list[Job]:
    data = get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        if (cats.get("commitment") or "").lower() in skipped_commitments({"part-time", "contract"}, {"intern", "internship"}):
            continue
        job = build_job(company=display_name(token), **_lever_posting(token, j))
        if job:
            out.append(job)
    return out


def _ashby_posting(token: str, j: dict) -> dict:
    locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
    country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry"))
    if len(locs) > 1:
        country = None  # multi-location postings: judge by the strings
    return dict(
        title=j.get("title"),
        location=" / ".join(l for l in locs if l),
        country=country,
        remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
        url=j.get("jobUrl"),
        source="ashby",
        posted_at=parse_iso(j.get("publishedAt")),
        posted_exact=True,
        description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
        apply_url=j.get("applyUrl"),
        verify_url=f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(token)}",
        posted_field="publishedAt",
        posted_raw=j.get("publishedAt"),
    )


def fetch_ashby(token: str) -> list[Job]:
    data = get_json_once(f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(token)}")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        if (j.get("employmentType") or "") in skipped_commitments({"PartTime", "Contract", "Temporary"}, {"Intern"}):
            continue
        job = build_job(company=display_name(token), **_ashby_posting(token, j))
        if job:
            out.append(job)
    return out


WORKDAY_MAX_PAGES = 25
_POSTED_ON = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)


WORKDAY_UNDATED_PAGES = 5            # sites that hide "Posted N days ago" in listings


def _workday_days(posted_on: str | None) -> int | None:
    m = _POSTED_ON.search(posted_on or "")
    if not m:
        return None
    word = m.group(1).lower()
    return 0 if word == "today" else 1 if word == "yesterday" else int(m.group(2))


def _workday_posting(base: str, cxs: str, site: str, path: str, list_location: str = "") -> dict:
    """base: public site root ("https://nvidia.wd5.myworkdayjobs.com"),
    cxs: its JSON API root, path: "/job/<location>/<title>_<req id>"."""
    info = get_json(f"{cxs}{path}").get("jobPostingInfo") or {}
    locs = [info.get("location") or list_location] + list(info.get("additionalLocations") or [])
    location = " / ".join(l for l in locs if l)
    start = info.get("startDate") or ""
    url = f"{base}/{site}{path}"
    return dict(
        title=info.get("title") or "",
        location=location,
        country=None if len(locs) > 1 else ((info.get("country") or {}).get("descriptor")),
        remote=bool(REMOTE.search(location)),
        url=url,
        source="workday",
        posted_at=_day_start(start),
        posted_exact=False,          # Workday gives a date, never a time
        description=strip_html(info.get("jobDescription")),
        apply_url=f"{url}/apply",
        verify_url=f"{cxs}{path}",
        posted_field="startDate",
        posted_raw=start,
    )


def fetch_workday(token: str) -> list[Job]:
    """token: "<tenant>.<wdN>/<site>", e.g. "nvidia.wd5/NVIDIAExternalCareerSite".

    With an empty search the CXS feed lists newest postings first (a few
    sites pin older "featured" postings on top), so pages are read until the
    last posting on a page is past the dashboard window. Detail records
    (location, description, posting date) are fetched only for postings whose
    titles pass the filters, once each (see cached_posting)."""
    host_part, site = token.split("/", 1)
    tenant = host_part.split(".")[0]
    base = f"https://{host_part}.myworkdayjobs.com"
    cxs = f"{base}/wday/cxs/{tenant}/{site}"
    company = display_name(tenant)
    cutoff = time.time() - FETCH_DAYS * 86400
    out: list[Job] = []
    total = 0
    for page in range(WORKDAY_MAX_PAGES):
        data = post_json(f"{cxs}/jobs", {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": ""})
        postings = data.get("jobPostings") or []
        page_days = [_workday_days(p.get("postedOn")) for p in postings]
        undated = all(d is None for d in page_days)
        for p, days in zip(postings, page_days):
            if days is not None and days > FETCH_DAYS:
                continue
            title = p.get("title") or ""
            if not screen("workday", title, fresh=days is not None and days <= FRESH_DAYS):
                continue
            path = p.get("externalPath") or ""
            posting = cached_posting(f"{base}/{site}{path}", lambda: _workday_posting(
                base, cxs, site, path, p.get("locationsText") or ""))
            if not is_posting(posting):
                continue
            posted = posting["posted_at"]
            if posted is not None and posted < cutoff - 86400:
                continue
            job = build_job(company=company, **{**posting, "title": title}, screened=True)
            if job:
                out.append(job)
        if page == 0:
            total = data.get("total") or 0   # Workday reports total on the first page only
        last = page_days[-1] if page_days else None
        if (not postings or (page + 1) * 20 >= total
                or (last is not None and last > FETCH_DAYS)
                or (undated and page + 1 >= WORKDAY_UNDATED_PAGES)):
            break
    return out


SR_API = "https://api.smartrecruiters.com/v1/companies"
SR_PAGE = 100
SR_MAX_PAGES = 10


def _smartrecruiters_posting(company: str, d: dict) -> dict:
    loc = d.get("location") or {}
    ad = (d.get("jobAd") or {}).get("sections") or {}
    pid = d.get("id")
    url = d.get("postingUrl") or f"https://jobs.smartrecruiters.com/{company}/{pid}"
    released = d.get("releasedDate")
    location = loc.get("fullLocation") or ", ".join(
        filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
    return dict(
        title=d.get("name"),
        location=location,
        country=loc.get("country"),
        remote=bool(loc.get("remote")),
        url=url,
        source="smartrecruiters",
        posted_at=parse_iso(released),
        posted_exact=released is not None,
        description=_sections(*(((ad.get(k) or {}).get("title") or "", (ad.get(k) or {}).get("text"))
                                for k in ("jobDescription", "qualifications", "additionalInformation")),
                              ("About the company", (ad.get("companyDescription") or {}).get("text"))),
        apply_url=d.get("applyUrl") or url,
        verify_url=f"{SR_API}/{company}/postings/{pid}",
        posted_field="releasedDate",
        posted_raw=released,
    )


def fetch_smartrecruiters(token: str) -> list[Job]:
    """token: the company identifier in jobs.smartrecruiters.com/<identifier>.
    The postings list comes newest first; descriptions need one request each."""
    cutoff = time.time() - FETCH_DAYS * 86400
    out: list[Job] = []
    for page in range(SR_MAX_PAGES):
        only = SEARCH.only_country()
        country = f"country={only.lower()}&" if only else ""
        data = get_json(f"{SR_API}/{token}/postings?{country}limit={SR_PAGE}&offset={page * SR_PAGE}")
        content = data.get("content") or []
        total = data.get("totalFound") or 0
        # An unknown identifier answers 200 with nothing in it, the same as a
        # company with no US openings; only the unfiltered count tells them apart.
        if page == 0 and not total and not get_json(f"{SR_API}/{token}/postings?limit=1").get("totalFound"):
            raise BoardMissing(f"smartrecruiters:{token}")
        for p in content:
            released = parse_iso(p.get("releasedDate"))
            title = p.get("name") or ""
            if (released and released < cutoff) or not screen(
                    "smartrecruiters", title, fresh=is_fresh(released)):
                continue
            pid = p.get("id")
            posting = cached_posting(f"https://jobs.smartrecruiters.com/{token}/{pid}", lambda: _smartrecruiters_posting(
                token, get_json(f"{SR_API}/{token}/postings/{pid}")))
            if not is_posting(posting):
                continue
            company = DISPLAY_NAMES.get(token) or (p.get("company") or {}).get("name") or token
            job = build_job(company=company, **posting, screened=True)
            if job:
                out.append(job)
        last = parse_iso(content[-1].get("releasedDate")) if content else None
        if not content or (page + 1) * SR_PAGE >= total or (last and last < cutoff):
            break
    return out


ORACLE_PAGE = 200
ORACLE_MAX_PAGES = 10
ORACLE_DATE_FACET = 7 if FETCH_DAYS <= 7 else 30     # the site's own "posted within" buckets


def _oracle_api(host: str) -> str:
    return f"https://{host}/hcmRestApi/resources/latest"


def _oracle_page(host: str, site: str, rid: str) -> str:
    return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}"


def _oracle_posting(host: str, site: str, rid: str, page_site: str | None = None) -> dict:
    """site: the site number for the API; page_site: the site as the public
    job page URL names it, when that differs."""
    url = (f"{_oracle_api(host)}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
           f"&finder=ById;Id=%22{rid}%22,siteNumber={site}")
    items = get_json(url).get("items") or []
    if not items:
        raise BoardMissing(url)
    d = items[0]
    locs = [d.get("PrimaryLocation") or ""] + [s.get("Name") or "" for s in d.get("secondaryLocations") or []]
    location = " / ".join(l for l in locs if l)
    raw = d.get("ExternalPostedStartDate")
    posted, exact = _dated(raw)
    page = _oracle_page(host, page_site or site, rid)
    description = _sections(("", d.get("ExternalDescriptionStr")),
                            ("Responsibilities", d.get("ExternalResponsibilitiesStr")),
                            ("Qualifications", d.get("ExternalQualificationsStr")))
    return dict(
        title=d.get("Title"),
        location=location,
        country=d.get("PrimaryLocationCountry") if len(locs) == 1 else None,
        remote=bool(REMOTE.search(location)) or "remote" in (d.get("WorkplaceType") or "").lower(),
        url=page,
        source="oracle",
        posted_at=posted,
        posted_exact=exact,
        description=_sections(("", description or d.get("ShortDescriptionStr")),
                              ("About the company", d.get("CorporateDescriptionStr"))),
        apply_url=page,
        verify_url=url,
        posted_field="ExternalPostedStartDate",
        posted_raw=raw,
    )


def fetch_oracle(token: str) -> list[Job]:
    """token: "<host>/<site number>", e.g. "jpmc.fa.oraclecloud.com/CX_1001",
    read from a careers URL .../hcmUI/CandidateExperience/en/sites/<site>/...
    The search is limited to the site's own "posted in the last 7 days"
    bucket, newest first; details need one request per posting."""
    host, site = token.split("/", 1)
    # Careers URLs name the site by number ("CX_1001") or by URL name
    # ("LateralHiring"). The search only honors the number: anything else,
    # names and unknown numbers alike, searches every site of the tenant,
    # internal ones included. So the site is looked up first.
    sites = get_json(f"{_oracle_api(host)}/recruitingCESites?onlyData=true&limit=500").get("items") or []
    number = next((s.get("SiteNumber") for s in sites if s.get("StatusCode") == "ORA_ACTIVE"
                   and site in (s.get("SiteNumber"), s.get("SiteURLName"))), None)
    if not number:
        raise BoardMissing(f"oracle:{token}")
    finder = f"findReqs;siteNumber={number},sortBy=POSTING_DATES_DESC,limit={ORACLE_PAGE}"
    list_url = f"{_oracle_api(host)}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations"
    out: list[Job] = []
    for page in range(ORACLE_MAX_PAGES):
        items = get_json(f"{list_url}&finder={finder},offset={page * ORACLE_PAGE},"
                         f"selectedPostingDatesFacet={ORACLE_DATE_FACET}").get("items") or [{}]
        total = items[0].get("TotalJobsCount") or 0
        reqs = items[0].get("requisitionList") or []
        for r in reqs:
            title = r.get("Title") or ""
            locs = [r.get("PrimaryLocation") or ""] + [s.get("Name") or "" for s in r.get("secondaryLocations") or []]
            country = r.get("PrimaryLocationCountry") if len(locs) == 1 else None
            if not screen("oracle", title, " / ".join(l for l in locs if l), country,
                          fresh=ORACLE_DATE_FACET <= FRESH_DAYS):
                continue
            rid = str(r.get("Id"))
            posting = cached_posting(_oracle_page(host, site, rid), lambda: _oracle_posting(host, number, rid, site))
            if is_posting(posting):
                job = build_job(company=display_name(token), **posting, screened=True)
                if job:
                    out.append(job)
        if not reqs or (page + 1) * ORACLE_PAGE >= total:
            break
    return out


EIGHTFOLD_PAGE = 10                  # fixed by the API
EIGHTFOLD_MAX_PAGES = 30


def _eightfold_posting(host: str, pid: str) -> dict:
    url = f"https://{host}/api/pcsx/position_details?position_id={pid}&hl=en"
    d = get_json(url).get("data") or {}
    if not d:
        raise BoardMissing(url)
    ts = d.get("postedTs")
    # Some tenants (Qualcomm) post at 00:00:00 UTC every time: a date, not a time.
    exact = bool(ts) and int(ts) % 86400 != 0
    posted = float(ts) if exact else _day_start(
        datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")) if ts else None
    page = d.get("publicUrl") or f"https://{host}/careers/job/{pid}"
    locs = d.get("locations") or [d.get("location") or ""]
    return dict(
        title=d.get("name"),
        location=" / ".join(l for l in locs if l),
        country=None,
        remote=d.get("workLocationOption") == "remote",
        url=page,
        source="eightfold",
        posted_at=posted,
        posted_exact=exact,
        description=strip_html(d.get("jobDescription")),
        apply_url=page,
        verify_url=url,
        posted_field="postedTs",
        posted_raw=ts,
    )


def fetch_eightfold(token: str) -> list[Job]:
    """token: "<careers host>|<company domain>[|<filter>=<value>...]", e.g.
    "apply.careers.microsoft.com|microsoft.com|filter_profession=software engineering".
    Eightfold's public search (robots.txt allows /api/pcsx) returns ten
    postings a page, newest first when sorted by timestamp."""
    host, domain, *extra = token.split("|")
    only = SEARCH.only_country()
    query = urllib.parse.urlencode([("domain", domain), ("query", "")]
                                   + ([("location", filters.COUNTRIES[only][0])] if only else [])
                                   + [("sort_by", "timestamp")] + [tuple(f.split("=", 1)) for f in extra])
    company = DISPLAY_NAMES.get(token) or domain.split(".")[0].title()
    cutoff = time.time() - FETCH_DAYS * 86400
    out: list[Job] = []
    for page in range(EIGHTFOLD_MAX_PAGES):
        data = get_json(f"https://{host}/api/pcsx/search?{query}&start={page * EIGHTFOLD_PAGE}").get("data") or {}
        positions = data.get("positions") or []
        for p in positions:
            ts = p.get("postedTs")
            title = p.get("name") or ""
            location = " / ".join(p.get("locations") or [])
            if (ts and ts < cutoff) or not screen("eightfold", title, location,
                                                  fresh=is_fresh(float(ts) if ts else None)):
                continue
            pid = str(p.get("id"))
            posting = cached_posting(f"https://{host}/careers/job/{pid}", lambda: _eightfold_posting(host, pid))
            if is_posting(posting):
                job = build_job(company=company, **posting, screened=True)
                if job:
                    out.append(job)
        last = positions[-1].get("postedTs") if positions else None
        if (not positions or (page + 1) * EIGHTFOLD_PAGE >= (data.get("count") or 0)
                or (last and last < cutoff)):
            break
    return out


AMAZON_SEARCH = "https://www.amazon.jobs/en/search.json"
AMAZON_PAGE = 100
AMAZON_MAX_PAGES = 10


def _amazon_posting(j: dict) -> dict:
    jid = j.get("id_icims")
    raw = j.get("posted_date") or ""
    try:
        posted = datetime.strptime(raw, "%B %d, %Y").timestamp()
    except ValueError:
        posted = None
    url = "https://www.amazon.jobs" + (j.get("job_path") or f"/en/jobs/{jid}")
    return dict(
        title=j.get("title"),
        location=j.get("normalized_location") or j.get("location") or "",
        country=j.get("country_code"),
        remote=False,
        url=url,
        source="amazon",
        posted_at=posted,
        posted_exact=False,          # a date, never a time
        description=_sections(("Description", j.get("description")),
                              ("Basic Qualifications", j.get("basic_qualifications")),
                              ("Preferred Qualifications", j.get("preferred_qualifications"))),
        apply_url=j.get("url_next_step") or url,
        # Searching by job id returns just that posting, posted_date included.
        verify_url=f"{AMAZON_SEARCH}?base_query={jid}&result_limit=10",
        posted_field="posted_date",
        posted_raw=raw,
    )


def fetch_amazon(category: str) -> list[Job]:
    """token: an amazon.jobs job category slug, e.g. "software-development".
    The search JSON behind amazon.jobs carries full descriptions, so one
    request covers a hundred postings."""
    cutoff = time.time() - FETCH_DAYS * 86400
    out: list[Job] = []
    for page in range(AMAZON_MAX_PAGES):
        query = urllib.parse.urlencode({"category[]": category, **country_param("country", ISO3), "sort": "recent",
                                        "result_limit": AMAZON_PAGE, "offset": page * AMAZON_PAGE})
        data = get_json(f"{AMAZON_SEARCH}?{query}")
        jobs = data.get("jobs") or []
        if page == 0 and not data.get("hits"):
            raise BoardMissing(f"amazon:{category}")
        last = None
        for j in jobs:
            posting = _amazon_posting(j)
            last = posting["posted_at"]
            if last is not None and last < cutoff - 86400:
                continue
            job = build_job(company="Amazon", **posting)
            if job:
                out.append(job)
        if not jobs or (page + 1) * AMAZON_PAGE >= (data.get("hits") or 0) or (last and last < cutoff - 86400):
            break
    return out


def _amazon_by_id(jid: str) -> dict:
    data = get_json(f"{AMAZON_SEARCH}?base_query={jid}&result_limit=10")
    for j in data.get("jobs") or []:
        if str(j.get("id_icims")) == jid:
            return _amazon_posting(j)
    raise BoardMissing(f"amazon job {jid}")


JIBE_PAGE = 100
JIBE_MAX_PAGES = 10
SITEMAP_URL = re.compile(r"<loc>\s*([^<]+?)\s*</loc>(?:\s*<lastmod>\s*([^<]+?)\s*</lastmod>)?")
ICIMS_JOB = re.compile(r"https?://[^/]+/jobs/(\d+)/([^/?#]+)/job")


def _jibe_posting(host: str, d: dict) -> dict:
    raw = d.get("posted_date") or ""
    posted, exact = _dated(raw)
    slug = d.get("slug") or d.get("req_id")
    page = f"https://{host}/jobs/{slug}?lang=en-us"
    multiple = str(d.get("multipleLocations")).lower() == "true"
    return dict(
        title=d.get("title"),
        location=d.get("full_location") or d.get("location_name") or "",
        country=None if multiple else d.get("country_code"),
        remote=bool(REMOTE.search(d.get("location_type") or "")),
        url=page,
        source="icims",
        posted_at=posted,
        posted_exact=exact,
        description=_sections(("", d.get("description")), ("Responsibilities", d.get("responsibilities")),
                              ("Qualifications", d.get("qualifications"))),
        apply_url=d.get("apply_url") or page,
        # The job page embeds the same date as schema.org JobPosting.datePosted.
        verify_url=page,
        posted_field="posted_date",
        posted_raw=raw,
    )


def _fetch_jibe(host: str) -> list[Job]:
    """iCIMS career sites on a company domain (careers.amd.com) run on
    iCIMS's Jibe front end, whose public /api/jobs returns full postings."""
    cutoff = time.time() - FETCH_DAYS * 86400
    out: list[Job] = []
    for page in range(1, JIBE_MAX_PAGES + 1):
        query = urllib.parse.urlencode({"page": page, "limit": JIBE_PAGE, "sortBy": "posted_date",
                                        "descending": "true", "internal": "false",
                                        **country_param("country")})
        data = get_json(f"https://{host}/api/jobs?{query}")
        jobs = [item.get("data") or {} for item in data.get("jobs") or []]
        last = None
        for d in jobs:
            posting = _jibe_posting(host, d)
            last = posting["posted_at"]
            if last is not None and last < cutoff - 86400:
                continue
            job = build_job(company=display_name(host), **posting)
            if job:
                out.append(job)
        if not jobs or page * JIBE_PAGE >= (data.get("totalCount") or 0) or (last and last < cutoff - 86400):
            break
    return out


def _fetch_icims_portal(host: str) -> list[Job]:
    """careers-<company>.icims.com portals have no JSON API. Their sitemap
    (linked from robots.txt) lists every posting with a slug title and a
    last-modified time; postings modified within the window whose slug passes
    the title filters are read once each for their schema.org JobPosting."""
    cutoff = time.time() - FETCH_DAYS * 86400
    out: list[Job] = []
    for loc, lastmod in SITEMAP_URL.findall(get_text(f"https://{host}/sitemap.xml")):
        m = ICIMS_JOB.match(html.unescape(loc))
        modified = parse_iso(lastmod)
        if not m or (modified and modified < cutoff):
            continue
        slug_title = urllib.parse.unquote(m.group(2)).replace("-", " ")
        if not screen("icims", slug_title, fresh=is_fresh(modified)):
            continue
        page = html.unescape(loc)
        posting = cached_posting(page, lambda: jsonld_posting(f"{page}?in_iframe=1", "icims", page))
        if is_posting(posting):
            job = build_job(company=display_name(host), **posting, screened=True)
            if job:
                out.append(job)
    return out


def fetch_icims(host: str) -> list[Job]:
    """token: the careers host, "careers-gdms.icims.com" or "careers.amd.com"."""
    return _fetch_icims_portal(host) if host.endswith(".icims.com") else _fetch_jibe(host)


LD_JSON = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def _ld_items(data) -> list[dict]:
    if isinstance(data, list):
        return [x for d in data for x in _ld_items(d)]
    if isinstance(data, dict):
        return [data] + _ld_items(data.get("@graph") or [])
    return []


def _ld_text(v) -> str:
    if isinstance(v, dict):
        return v.get("name") or ""
    return str(v or "")


def jsonld_posting(url: str, source: str = "careerpage", page_url: str | None = None) -> dict | None:
    """A posting from the schema.org JobPosting most career pages embed for
    search engines. None when the page has none."""
    page = get_text(url)
    for m in LD_JSON.finditer(page):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for item in _ld_items(data):
            types = item.get("@type")
            if "JobPosting" in (types if isinstance(types, list) else [types]):
                return _jsonld_job(item, page_url or url, source)
    return None


def _jsonld_job(item: dict, url: str, source: str) -> dict:
    places = item.get("jobLocation") or []
    places = places if isinstance(places, list) else [places]
    locs, countries = [], []
    for place in places:
        addr = (place or {}).get("address") or {}
        if isinstance(addr, str):
            locs.append(addr)
            continue
        country = _ld_text(addr.get("addressCountry"))
        countries.append(country)
        locs.append(", ".join(p for p in (_ld_text(addr.get("addressLocality")),
                                          _ld_text(addr.get("addressRegion")), country)
                              if p and p != "UNAVAILABLE"))
    raw = item.get("datePosted") or ""
    posted, exact = _dated(raw)
    remote = "TELECOMMUTE" in str(item.get("jobLocationType") or "").upper()
    return dict(
        title=html.unescape(item.get("title") or ""),
        location=" / ".join(l for l in locs if l) or ("Remote" if remote else ""),
        country=countries[0] if len(countries) == 1 else None,
        remote=remote,
        url=url,
        source=source,
        posted_at=posted,
        posted_exact=exact,
        description=_sections(("", item.get("description")), ("Responsibilities", item.get("responsibilities")),
                              ("Qualifications", item.get("qualifications"))),
        apply_url=url,
        verify_url=url,
        posted_field="datePosted",
        posted_raw=raw,
    )


# Listing URLs (as SimplifyJobs records them) that point at a system with a
# per-posting record. Anything else is read for its schema.org JobPosting.
POSTING_URLS: list[tuple[re.Pattern, Callable[[re.Match], dict | None]]] = [
    (re.compile(r"https?://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?!embed/)(?P<token>[\w-]+)/jobs/(?P<id>\d+)"),
     lambda m: _greenhouse_posting(m["token"], get_json(
         f"https://boards-api.greenhouse.io/v1/boards/{m['token']}/jobs/{m['id']}"))),
    (re.compile(r"https?://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/embed/job_app\?"
                r"(?=.*\bfor=(?P<token>[\w-]+))(?=.*\btoken=(?P<id>\d+))"),
     lambda m: _greenhouse_posting(m["token"], get_json(
         f"https://boards-api.greenhouse.io/v1/boards/{m['token']}/jobs/{m['id']}"))),
    (re.compile(r"https?://jobs\.(?P<eu>eu\.)?lever\.co/(?P<token>[^/?#]+)/(?P<id>[0-9a-f-]{36})"),
     lambda m: _lever_posting(m["token"], get_json(
         f"https://api.{m['eu'] or ''}lever.co/v0/postings/{m['token']}/{m['id']}"),
         f"https://api.{m['eu'] or ''}lever.co")),
    (re.compile(r"https?://jobs\.ashbyhq\.com/(?P<token>[^/?#]+)/(?P<id>[0-9a-f-]{36})"),
     lambda m: _ashby_by_id(urllib.parse.unquote(m["token"]), m["id"])),
    (re.compile(r"https?://(?P<tenant>[\w-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
                r"(?P<site>[^/?#]+)(?P<path>/job/[^?#]+)"),
     lambda m: _workday_posting(f"https://{m['tenant']}.{m['wd']}.myworkdayjobs.com",
                                f"https://{m['tenant']}.{m['wd']}.myworkdayjobs.com/wday/cxs/{m['tenant']}/{m['site']}",
                                m["site"], _workday_path(m["path"]))),
    (re.compile(r"https?://(?P<wd>wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/(?P<tenant>[\w-]+)/"
                r"(?P<site>[^/?#]+)(?P<path>/job/[^?#]+)"),
     lambda m: _workday_posting(f"https://{m['wd']}.myworkdaysite.com/recruiting/{m['tenant']}",
                                f"https://{m['wd']}.myworkdaysite.com/wday/cxs/{m['tenant']}/{m['site']}",
                                m["site"], _workday_path(m["path"]))),
    (re.compile(r"https?://jobs\.smartrecruiters\.com/(?P<company>[^/?#]+)/(?P<id>\d+)"),
     lambda m: _smartrecruiters_posting(m["company"], get_json(f"{SR_API}/{m['company']}/postings/{m['id']}"))),
    (re.compile(r"https?://(?P<host>[\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[\w-]+/sites/"
                r"(?P<site>[\w-]+)/job/(?P<id>\d+)"),
     lambda m: _oracle_posting(m["host"], m["site"], m["id"])),
    (re.compile(r"https?://(?P<host>[\w-]+\.eightfold\.ai|apply\.careers\.microsoft\.com)/careers/job/(?P<id>\d+)"),
     lambda m: _eightfold_posting(m["host"], m["id"])),
    (re.compile(r"https?://(?:www\.)?amazon\.jobs/(?:[\w-]+/)?jobs/(?P<id>\d+)"),
     lambda m: _amazon_by_id(m["id"])),
    (re.compile(r"https?://(?P<host>[\w-]+\.icims\.com)/jobs/(?P<id>\d+)"),
     lambda m: jsonld_posting(f"https://{m['host']}/jobs/{m['id']}/job?in_iframe=1", "icims", m.string)),
    (re.compile(r"https?://[^/]+/jobs/\d+\?(?:.*&)?icims=1"),     # Jibe pages on company domains
     lambda m: jsonld_posting(m.string, "icims")),
]


def _workday_path(path: str) -> str:
    return re.sub(r"/apply(?:/.*)?$", "", path.rstrip("/"))


def _ashby_by_id(token: str, pid: str) -> dict:
    for j in get_json_once(f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(token)}").get("jobs", []):
        if j.get("id") == pid:
            return _ashby_posting(token, j)
    raise BoardMissing(f"ashby:{token}/{pid}")


def posting_loader(url: str) -> Callable[[], dict | None] | None:
    parts = urllib.parse.urlsplit(url or "")
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or any(
            host == h or host.endswith("." + h) for h in NO_AUTOMATED_ACCESS):
        return None
    for rx, load in POSTING_URLS:
        m = rx.match(url)
        if m:
            return lambda: load(m)
    return lambda: jsonld_posting(url)


SIMPLIFY_SPONSOR = {
    "Does Not Offer Sponsorship": ("no", "Listing says no sponsorship"),
    "U.S. Citizenship is Required": ("no", "Listing says U.S. citizenship required"),
    "Offers Sponsorship": ("yes", "Listing says sponsorship offered"),
}
SIMPLIFY_WORKERS = 8


def fetch_simplify(_: str = "") -> list[Job]:
    """SimplifyJobs' New-Grad list catches companies no board here covers. A
    listing that passes the filters is replaced by the posting it links to
    whenever that posting has a readable record (ATS API or schema.org data),
    so it gets the real description, date and evidence; the rest stay as
    listings without descriptions."""
    data = get_json(SIMPLIFY_URL)
    cutoff = time.time() - FETCH_DAYS * 86400
    listings = [j for j in data
                if j.get("active") and j.get("is_visible", True) and (j.get("date_posted") or 0) >= cutoff
                and screen("simplify", j.get("title") or "", " / ".join(j.get("locations") or []),
                           fresh=is_fresh(j.get("date_posted")))]
    with ThreadPoolExecutor(max_workers=SIMPLIFY_WORKERS) as pool:
        results = list(pool.map(_simplify_job, listings))
    tally: dict[str, int] = {}
    for _, status in results:
        tally[status] = tally.get(status, 0) + 1
    log(f"simplify: {len(listings)} listings; "
        + ", ".join(f"{n} {s}" for s, n in sorted(tally.items(), key=lambda kv: -kv[1])))
    return [job for job, _ in results if job]


def _simplify_job(j: dict) -> tuple[Job | None, str]:
    locs = j.get("locations") or []
    url = j.get("url") or ""
    listing = dict(
        title=j.get("title"),
        location=" / ".join(locs),
        country=None,
        remote=any(REMOTE.search(l) for l in locs),
        url=url,
        source="simplify",
        posted_at=float(j["date_posted"]),
        posted_exact=True,
        description="",
        apply_url=url,
        verify_url=SIMPLIFY_VERIFY_URL,
        posted_field="date_posted",
        posted_raw=j["date_posted"],
    )
    listing_key = ""
    load = posting_loader(url)
    status = "listing only (no readable source)"
    if load:
        try:
            posting = cached_posting(url, load)
        except Disallowed:
            posting, status = None, "listing only (robots.txt disallows)"
        except Exception as e:
            posting, status = None, f"listing only ({type(e).__name__})"
        if posting and posting.get("gone"):
            return None, "closed at the source"
        if is_posting(posting):
            listing_key = norm_key(j.get("company_name", ""), (listing["title"] or "").strip(),
                                   listing["location"].strip())
            listing = {**posting, "location": posting["location"] or listing["location"]}
            status = "with source posting"
    job = build_job(company=j.get("company_name", ""), **listing,
                    sponsor_hint=SIMPLIFY_SPONSOR.get(j.get("sponsorship", "")), screened=True, via="simplify")
    if job and status == "with source posting" and listing_key != job.key:
        job.listing_key = listing_key
    return job, status


DISPLAY_NAMES: dict[str, str] = {}


def display_name(token: str) -> str:
    return DISPLAY_NAMES.get(token) or token.replace("-", " ").title()


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "workday": fetch_workday, "smartrecruiters": fetch_smartrecruiters, "oracle": fetch_oracle,
            "eightfold": fetch_eightfold, "amazon": fetch_amazon, "icims": fetch_icims}


@dataclass
class FetchResult:
    jobs: list[Job]
    missing: list[str]          # boards that 404: the token is wrong or the company left that ATS
    failed: list[str]           # boards that errored, with the reason
    throttled: list[str]        # boards whose host kept answering 429 (also in failed)
    attempted: list[str]
    kept: dict[str, int] = field(default_factory=dict)     # board -> jobs it returned
    seconds: dict[str, float] = field(default_factory=dict)  # board -> time its fetch took


def _timed(fn: Callable[[str], list[Job]], tok: str) -> tuple[list[Job], float]:
    t = time.monotonic()
    try:
        return fn(tok), time.monotonic() - t
    except Exception as e:
        e.seconds = time.monotonic() - t
        raise


def fetch_all(companies: dict[str, list[str]], skip: set[str] = frozenset(),
              simplify: bool = True, progress: Callable[[int, int, int], None] | None = None) -> FetchResult:
    """progress(boards done, boards in all, jobs so far) is called as boards finish."""
    tasks = [(src, tok) for src, toks in companies.items() if src in FETCHERS
             for tok in dict.fromkeys(toks) if f"{src}:{tok}" not in skip]
    if simplify:
        tasks.append(("simplify", ""))
    res = FetchResult([], [], [], [], [f"{src}:{tok}" for src, tok in tasks])
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(_timed, fetch_simplify if src == "simplify" else FETCHERS[src], tok): (src, tok)
            for src, tok in tasks
        }
        for done, fut in enumerate(as_completed(futures), 1):
            src, tok = futures[fut]
            try:
                got, res.seconds[f"{src}:{tok}"] = fut.result()
                res.jobs.extend(got)
                res.kept[f"{src}:{tok}"] = len(got)
            except BoardMissing:
                res.missing.append(f"{src}:{tok}")
            except Exception as e:  # one bad board must not sink the run
                if isinstance(e, Throttled):
                    res.throttled.append(f"{src}:{tok}")
                res.failed.append(f"{src}:{tok} ({type(e).__name__}: {e})")
            if progress:
                progress(done, len(tasks), len(res.jobs))
    res.missing.sort()
    res.failed.sort()
    return res


def dedupe(jobs: list[Job]) -> list[Job]:
    """Same role can arrive from an ATS board and from the Simplify feed.
    Keep the ATS copy (it carries the real description) and borrow the
    Simplify sponsorship label when the description said nothing."""
    by_key: dict[str, Job] = {}
    for job in jobs:
        cur = by_key.get(job.key)
        if cur is None:
            by_key[job.key] = job
            continue
        primary, other = (job, cur) if cur.source == "simplify" else (cur, job)
        if primary.sponsorship == "unknown" and other.sponsorship != "unknown":
            primary.sponsorship, primary.sponsor_note = other.sponsorship, other.sponsor_note
        primary.listing_key = primary.listing_key or other.listing_key
        by_key[job.key] = primary
    return list(by_key.values())


# --------------------------------------------------------------------------
# Storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key          TEXT PRIMARY KEY,
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    location     TEXT NOT NULL,
    remote       INTEGER NOT NULL,
    url          TEXT NOT NULL,
    source       TEXT NOT NULL,
    posted_at    REAL,
    posted_exact INTEGER NOT NULL,
    sponsorship  TEXT NOT NULL,
    sponsor_note TEXT NOT NULL,
    min_yoe      INTEGER,
    entry_hint   INTEGER NOT NULL,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_posted ON jobs(posted_at);
CREATE INDEX IF NOT EXISTS jobs_first_seen ON jobs(first_seen);
CREATE INDEX IF NOT EXISTS jobs_last_seen ON jobs(last_seen);
CREATE TABLE IF NOT EXISTS runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started  REAL NOT NULL,
    finished REAL NOT NULL,
    boards   INTEGER NOT NULL,
    failed   INTEGER NOT NULL,
    matched  INTEGER NOT NULL,
    new      INTEGER NOT NULL
);
"""

# Run-to-run state besides the jobs themselves: posting details fetched once
# (cached_posting), robots.txt files (robots_for), and boards sitting out a
# cooldown after their host throttled them.
STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS details (
    url     TEXT PRIMARY KEY,
    fetched REAL NOT NULL,
    posting TEXT NOT NULL           -- build_job() arguments as JSON, or {"gone": true} / {"unavailable": true}
);
CREATE INDEX IF NOT EXISTS details_fetched ON details(fetched);
CREATE TABLE IF NOT EXISTS robots (
    host    TEXT PRIMARY KEY,
    fetched REAL NOT NULL,
    body    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_funnel (
    started REAL NOT NULL,          -- runs.started of the run
    source  TEXT NOT NULL,
    stage   TEXT NOT NULL,          -- FUNNEL_STAGES
    n_all   INTEGER NOT NULL,       -- every posting seen
    n_fresh INTEGER NOT NULL,       -- postings dated within FRESH_DAYS
    PRIMARY KEY (started, source, stage)
);
CREATE TABLE IF NOT EXISTS board_state (
    board      TEXT PRIMARY KEY,    -- "<source>:<token>"
    strikes    INTEGER NOT NULL,    -- throttled runs in a row
    skip_until REAL NOT NULL,
    last_error TEXT NOT NULL
);
"""
COOLDOWN_BASE = 3600        # a throttled board sits out an hour, doubling per strike
COOLDOWN_MAX = 86400

# Columns added after the first release; created on demand so existing
# databases keep their history.
MIGRATIONS = {
    "apply_url": "TEXT NOT NULL DEFAULT ''",
    "verify_url": "TEXT NOT NULL DEFAULT ''",
    "posted_field": "TEXT NOT NULL DEFAULT ''",
    "posted_raw": "TEXT NOT NULL DEFAULT ''",
    "skills": "TEXT NOT NULL DEFAULT '{}'",
    "description": "TEXT NOT NULL DEFAULT ''",
    "no_opt": "INTEGER NOT NULL DEFAULT 0",
    # The dashboard's keyword checklist and degree requirement (JSON), saved so
    # server.py can score jobs exactly as the dashboard does.
    "kw": "TEXT NOT NULL DEFAULT ''",
    "degree": "TEXT NOT NULL DEFAULT ''",
    # Roles (filters.py): the source's country field, stated pay, and the
    # roles that want the job (JSON list, refreshed every render).
    "country": "TEXT NOT NULL DEFAULT ''",
    "pay_min": "REAL",
    "pay_max": "REAL",
    "roles": "TEXT NOT NULL DEFAULT '[]'",
    # Reposts: a role at one company and place keeps its row (its key), so a
    # posting taken down and put up again shows as its date moving forward.
    # first_posted is the earliest date seen, reposts how often it moved.
    "first_posted": "REAL",
    "reposts": "INTEGER NOT NULL DEFAULT 0",
}


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    have = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "profiles" in have and "roles" not in have:
        # Search profiles were renamed roles; the tags carry over.
        conn.execute("ALTER TABLE jobs RENAME COLUMN profiles TO roles")
        have = (have - {"profiles"}) | {"roles"}
    for col, decl in MIGRATIONS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
    # server.py finds the posting behind an application form by its apply_url.
    conn.execute("CREATE INDEX IF NOT EXISTS jobs_apply_url ON jobs(apply_url)")
    conn.execute("CREATE TABLE IF NOT EXISTS corpus (id INTEGER PRIMARY KEY CHECK (id = 1), "
                 "updated REAL NOT NULL, skill_w TEXT NOT NULL, term_w TEXT NOT NULL)")
    had_details = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'details'").fetchone()
    conn.executescript(STATE_SCHEMA)
    if not had_details:
        # Workday details fetched before the cache existed live in jobs rows;
        # seeding from them spares refetching every one.
        conn.execute(
            """INSERT OR IGNORE INTO details (url, fetched, posting)
               SELECT url, last_seen, json_object(
                   'title', title, 'location', location, 'country', NULL,
                   'remote', json(CASE WHEN remote THEN 'true' ELSE 'false' END), 'url', url,
                   'source', source, 'posted_at', posted_at, 'posted_exact', json('false'),
                   'description', description, 'apply_url', apply_url, 'verify_url', verify_url,
                   'posted_field', posted_field, 'posted_raw', posted_raw)
               FROM jobs WHERE source = 'workday' AND instr(description, char(10)) > 0""")


def store(conn: sqlite3.Connection, jobs: list[Job], now: float) -> int:
    existing = {row[0] for row in conn.execute(
        "SELECT key FROM jobs WHERE key IN (SELECT value FROM json_each(?))",
        (json.dumps([j.key for j in jobs] + [j.listing_key for j in jobs if j.listing_key]),))}
    # A job first stored as a bare Simplify listing moves to its posting's key
    # once that posting is read, keeping its row and first_seen; if the posting
    # is already stored (from its own board), the listing row folds into it.
    for j in jobs:
        if not j.listing_key:
            continue
        conn.execute("UPDATE OR IGNORE jobs SET key = ?, company = ?, title = ?, location = ?, remote = ? "
                     "WHERE key = ?", (j.key, j.company, j.title, j.location, j.remote, j.listing_key))
        conn.execute("UPDATE jobs SET first_seen = MIN(first_seen, (SELECT first_seen FROM jobs WHERE key = ?)) "
                     "WHERE key = ? AND EXISTS (SELECT 1 FROM jobs WHERE key = ?)",
                     (j.listing_key, j.key, j.listing_key))
        conn.execute("DELETE FROM jobs WHERE key = ?", (j.listing_key,))
    # posted_at and its evidence always mirror what the source says right now,
    # so the dashboard never shows a timestamp the Verify link can't back up.
    # first_seen is never overwritten: it is the radar's own independent record.
    conn.executemany(
        """INSERT INTO jobs (key, company, title, location, remote, url, source, posted_at,
                             posted_exact, sponsorship, sponsor_note, min_yoe, entry_hint,
                             first_seen, last_seen, apply_url, verify_url, posted_field,
                             posted_raw, skills, description, no_opt, country, pay_min, pay_max, roles,
                             first_posted)
           VALUES (:key, :company, :title, :location, :remote, :url, :source, :posted_at,
                   :posted_exact, :sponsorship, :sponsor_note, :min_yoe, :entry_hint,
                   :now, :now, :apply_url, :verify_url, :posted_field, :posted_raw, :skills,
                   :description, :no_opt, :country, :pay_min, :pay_max, :roles, :posted_at)
           ON CONFLICT(key) DO UPDATE SET
               -- A source's own posting date (not an update time) a day or more
               -- later than before: the role was posted again.
               reposts = jobs.reposts + (excluded.posted_exact AND jobs.posted_at IS NOT NULL
                                         AND excluded.posted_at > jobs.posted_at + 86400),
               first_posted = MIN(COALESCE(jobs.first_posted, jobs.posted_at, jobs.first_seen),
                                  COALESCE(excluded.posted_at, jobs.first_seen)),
               url = excluded.url, source = excluded.source,
               posted_at = excluded.posted_at, posted_exact = excluded.posted_exact,
               posted_field = excluded.posted_field, posted_raw = excluded.posted_raw,
               apply_url = excluded.apply_url, verify_url = excluded.verify_url,
               sponsorship = excluded.sponsorship, sponsor_note = excluded.sponsor_note,
               no_opt = excluded.no_opt, min_yoe = excluded.min_yoe, skills = excluded.skills,
               country = excluded.country, pay_min = excluded.pay_min, pay_max = excluded.pay_max,
               roles = excluded.roles,
               description = CASE WHEN excluded.description != '' THEN excluded.description
                                  ELSE jobs.description END,
               last_seen = excluded.last_seen""",
        [{**asdict(j), "now": now} for j in jobs],
    )
    conn.execute("DELETE FROM jobs WHERE last_seen < ?", (now - PURGE_AFTER_DAYS * 86400,))
    return len({j.key for j in jobs if j.key not in existing and j.listing_key not in existing})


DASHBOARD_COLS = [
    "key", "company", "title", "location", "remote", "url", "source", "posted_at", "posted_exact",
    "sponsorship", "sponsor_note", "no_opt", "min_yoe", "entry_hint", "first_seen", "apply_url",
    "verify_url", "posted_field", "posted_raw", "skills", "country", "pay_min", "pay_max",
    "first_posted", "reposts",
]


# --------------------------------------------------------------------------
# Corpus statistics for match scoring
#
# A skill that nearly every posting mentions (Git, "backend") says little about
# fit; a rare one (CUDA, Figma) says a lot. Weights come from how many stored
# postings mention each skill. The fixed taxonomy also can't see tools it
# doesn't list, so tech terms are learned from the postings themselves:
# capitalized mid-sentence tokens that recur across postings and are almost
# never written in lowercase (so ordinary English words drop out).

GENERIC_SKILLS = {"Backend", "Frontend", "Full Stack", "OOP", "Git", "Data Structures & Algorithms",
                  "System Design", "Agile", "Testing", "CI/CD", "REST APIs", "Linux", "SQL"}
TERM = re.compile(r"(?<=[a-z,;(/] )([A-Z][A-Za-z0-9.+#-]*[A-Za-z0-9+#])")
TERM_STOP = {
    "AI", "ML", "API", "APIs", "UI", "UX", "US", "USA", "SF", "NYC", "LA", "EEO", "PTO", "HR", "IT",
    "CEO", "CTO", "VP", "PhD", "BS", "MS", "BA", "MA", "CS", "SaaS", "B2B", "B2C", "GPU", "GPUs", "CPU",
    "OS", "SDK", "SDKs", "ID", "OK", "ET", "PT", "PST", "EST", "Q1", "Q2", "Q3", "Q4", "II", "III",
    "LLC", "Inc", "Fortune", "Series", "Remote", "Hybrid", "Senior", "Staff", "Engineer", "Engineering",
    "Software", "Team", "Teams", "Product", "Products", "Customer", "Customers", "Platform", "Our",
    "We", "You", "Your", "This", "The", "In", "At", "As", "For", "With", "And", "Or", "If", "Bonus",
    "Equal", "Opportunity", "Employer", "Benefits", "Medical", "Dental", "Vision", "California",
    "New", "York", "San", "Francisco", "Seattle", "Boston", "Austin", "United", "States", "America",
    "January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
    "November", "December", "Monday", "Friday", "English", "Spanish", "French", "German", "Mandarin",
    "Bachelor", "Master", "Masters", "Bachelors", "Computer", "Science", "Degree", "Applicants",
    "Nice", "Buffers", "Compose", "Bricks", "Genie", "Euclidean", "Cross-context", "First-hand",
    "Gaia", "DB", "Microsoft", "Google", "Amazon", "Meta", "Apple",
}


def corpus_stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT skills, description, company FROM jobs WHERE description != ''").fetchall()
    n = len(rows) or 1
    skill_df: dict[str, int] = {}
    term_df: dict[str, int] = {}
    term_lower: dict[str, int] = {}
    tech_df: dict[str, int] = {}
    for skills, desc, company in rows:
        s = json.loads(skills or "{}")
        for k in set(s.get("t", []) + s.get("r", []) + s.get("p", [])):
            skill_df[k] = skill_df.get(k, 0) + 1
        company_words = {w.lower() for w in re.findall(r"[A-Za-z]+", company or "")}
        doc_terms: set[str] = set()
        doc_tech: set[str] = set()
        # A term is "in a tech context" when its sentence also names 2+ known skills.
        for seg in re.split(r"[.;:\n•]+", desc):
            terms = {t for t in TERM.findall(seg)
                     if t not in TERM_STOP and t.lower() not in company_words and len(t) >= 2}
            if not terms:
                continue
            doc_terms |= terms
            if len(find_skills(seg)) >= 2:
                doc_tech |= terms
        for t in doc_terms:
            term_df[t] = term_df.get(t, 0) + 1
        for t in doc_tech:
            tech_df[t] = tech_df.get(t, 0) + 1
        for w in set(re.findall(r"[a-z][a-z0-9.+#-]*", desc)):   # lowercase usage, once per doc
            term_lower[w] = term_lower.get(w, 0) + 1
    vocab = {t: c for t, c in term_df.items()
             if tech_df.get(t, 0) >= 3 and tech_df[t] >= 0.6 * c        # mostly appears among skills
             and term_lower.get(t.lower(), 0) <= 0.15 * c               # not an ordinary word
             and c <= 0.4 * n and not find_skills(t)}

    def weight(df: int) -> float:
        idf = math.log((n + 1) / (df + 1))
        return round(min(1.5, max(0.25, idf / math.log(10))), 2)

    skill_w = {k: (min(weight(c), 0.3) if k in GENERIC_SKILLS else weight(c)) for k, c in skill_df.items()}
    term_w = {t: weight(c) for t, c in vocab.items()}
    return {"n": n, "skill_w": skill_w, "term_w": term_w}


def term_patterns(term_w: dict[str, float]) -> dict[str, re.Pattern]:
    """Learned terms are matched case-sensitively as whole tokens, as written."""
    return {t: re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])") for t in term_w}


def shown_days(title: str) -> int:
    """How long a posting stays on the dashboard: longer for hiring programs."""
    return max(DASHBOARD_DAYS, PROGRAM_DAYS) if filters.is_program(title) else DASHBOARD_DAYS


def closed_postings(conn: sqlite3.Connection, now: float) -> dict[str, list[float]]:
    """When each closed posting still stored (until it is purged) went up,
    by its identity, oldest first. An open posting with the identity of a
    closed one is that role taken down and put up again. Two open postings
    with one title are left alone: they're often two teams hiring."""
    by: dict[str, list[float]] = {}
    for company, title, location, posted in conn.execute(
            "SELECT company, title, location, COALESCE(posted_at, first_seen) FROM jobs WHERE last_seen < ?",
            (now - STALE_AFTER_DAYS * 86400,)):
        by.setdefault(filters.posting_identity(company, title, location), []).append(posted)
    for xs in by.values():
        xs.sort()
    return by


def load_for_dashboard(conn: sqlite3.Connection, now: float, prev_run: float | None,
                       h1b: "H1BIndex", stats: dict | None = None) -> list[dict]:
    rows = conn.execute(
        f"""SELECT {", ".join(DASHBOARD_COLS)}, length(description) > 200, description
           FROM jobs
           WHERE last_seen >= ? AND COALESCE(posted_at, first_seen) >= ?
           ORDER BY COALESCE(posted_at, first_seen) DESC""",
        (now - STALE_AFTER_DAYS * 86400, now - max(DASHBOARD_DAYS, PROGRAM_DAYS) * 86400),
    ).fetchall()
    term_rx = term_patterns(stats["term_w"]) if stats else {}
    closed = closed_postings(conn, now)
    out = []
    for r in rows:
        d = dict(zip(DASHBOARD_COLS + ["has_jd", "_desc"], r))
        posted = d["posted_at"] or d["first_seen"]
        if now - posted > shown_days(d["title"]) * 86400:
            continue
        # Reposted: the same row's date moved forward, or a closed posting
        # of this role went up earlier under another ID.
        earlier = [p for p in closed.get(filters.posting_identity(d["company"], d["title"], d["location"]), [])
                   if p < posted]
        first, times = d.pop("first_posted"), d.pop("reposts") or 0
        if earlier:
            first, times = min(earlier[0], first or posted), times + len(earlier)
        d["program"] = filters.is_program(d["title"])
        d["repost"] = {"first": first, "times": times} if times and first and first < posted - 86400 else None
        desc = d.pop("_desc") or ""
        d["has_jd"] = bool(d["has_jd"])
        d["skills"] = json.loads(d["skills"] or "{}")
        d["fresh"] = prev_run is not None and d["first_seen"] > prev_run
        d["h1b"] = h1b.lookup(d["company"])
        # The checklist the dashboard scores against: concrete keywords from
        # the posting's requirement lines, plus the degree it asks for.
        d["kw"] = reqparse.keywords(desc, d["title"], find_skills, term_rx) if d["has_jd"] else []
        d["degree"] = reqparse.degree(desc) if d["has_jd"] else None
        # Tagged again with the current roles, so a changed role shows on the
        # next render without refetching. Jobs no role wants stay stored (a
        # role may want them again) but are not shown.
        pay = filters.pay_range(desc)       # rows stored before pay was read have none saved
        d["pay_min"], d["pay_max"] = pay or (None, None)
        d["roles"] = SEARCH.matching(filters.Facts(
            d["company"], d["title"], d["location"], d.pop("country") or None, bool(d["remote"]), desc,
            d["min_yoe"], bool(d["entry_hint"]), pay))
        if d["roles"]:
            out.append(d)
    return out


def save_scoring(conn: sqlite3.Connection, rows: list[dict], stats: dict, now: float) -> None:
    """Checklists and keyword weights as the dashboard got them, for the
    helper's preparation queue (server.py scores with match.py)."""
    with conn:
        conn.executemany("UPDATE jobs SET kw = ?, degree = ?, roles = ? WHERE key = ?",
                         [(json.dumps(r["kw"]), json.dumps(r["degree"]) if r["degree"] else "",
                           json.dumps(r["roles"]), r["key"]) for r in rows])
        # Jobs no current role wants lose their tags, so the queue skips them.
        conn.execute("UPDATE jobs SET roles = '[]' WHERE roles != '[]' "
                     "AND key NOT IN (SELECT value FROM json_each(?))", (json.dumps([r["key"] for r in rows]),))
        conn.execute("INSERT OR REPLACE INTO corpus (id, updated, skill_w, term_w) VALUES (1, ?, ?, ?)",
                     (now, json.dumps(stats["skill_w"]), json.dumps(stats["term_w"])))


# --------------------------------------------------------------------------
# H-1B employer history (USCIS H-1B Employer Data Hub)

EMPLOYER_NOISE = re.compile(
    r"\b(inc|incorporated|llc|l l c|ltd|limited|corp|corporation|co|company|lp|llp|plc|"
    r"pbc|pc|holdings?|group|usa|us|u s|america|americas|international|global|"
    r"technologies|technology|tech|labs?|software|systems|services|solutions|the|and)\b")


def norm_employer(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = EMPLOYER_NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def raw_employer(name: str) -> str:
    """USCIS spells one employer many ways ("AMAZON.COM SERVICES LLC",
    "AMAZON COM SERVICES LLC"); collapse punctuation so they merge."""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9&]+", " ", name.upper())).strip()


class H1BIndex:
    """Approval counts per USCIS employer, looked up by company name.

    Brand names and legal names differ, so lookup goes: explicit alias from
    companies.json, then exact normalized name, then (for names of 5+
    characters) employers whose normalized name starts with it. When several
    legal entities collide, the largest wins and the match is marked
    ambiguous. The matched legal name always travels with the result so a
    bad match is visible on the dashboard rather than hidden.

    Each employer keeps every loaded fiscal year, because the newest export
    is often a partial year and would understate a company on its own."""

    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self.by_raw: dict[str, dict] = {}            # collapsed USCIS name -> record
        self.by_key: dict[str, list[dict]] = {}      # brand-normalized name -> records
        self.sorted_keys: list[str] = []
        self.aliases = {k.lower(): raw_employer(v) for k, v in (aliases or {}).items()}
        self.years: list[int] = []

    @classmethod
    def load(cls, aliases: dict[str, str] | None = None) -> "H1BIndex":
        idx = cls(aliases)
        for year in H1B_YEARS:
            path = H1B_DIR / f"h1b_datahubexport-{year}.csv"
            if not path.exists():
                continue
            idx.years.append(year)
            with path.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    raw = raw_employer(row.get("Employer") or "")
                    if not raw:
                        continue
                    rec = idx.by_raw.get(raw)
                    if rec is None:
                        rec = idx.by_raw[raw] = {"employer": raw, "years": {}}
                        # "X LLC DBA Y": the brand after DBA is indexed too.
                        for name in [raw] + re.split(r"\bD ?B ?A\b", raw)[1:]:
                            key = norm_employer(name)
                            if key:
                                idx.by_key.setdefault(key, []).append(rec)
                    y = rec["years"].setdefault(str(year), {"initial": 0, "continuing": 0})
                    y["initial"] += int(row.get("Initial Approval") or 0)
                    y["continuing"] += int(row.get("Continuing Approval") or 0)
        idx.sorted_keys = sorted(idx.by_key)
        return idx

    def lookup(self, company: str) -> dict | None:
        alias = self.aliases.get(company.lower())
        if alias:
            rec = self.by_raw.get(alias)
            return _summary(rec, "alias", 1) if rec and _approvals(rec) else None
        key = norm_employer(company)
        if not key:
            return None
        cands = list(self.by_key.get(key, []))
        how = "exact"
        if not cands and len(key) >= 5:
            how = "prefix"
            i = bisect.bisect_left(self.sorted_keys, key + " ")
            while i < len(self.sorted_keys) and self.sorted_keys[i].startswith(key + " "):
                cands.extend(self.by_key[self.sorted_keys[i]])
                i += 1
        cands = [c for c in cands if _approvals(c) > 0]
        if not cands:
            return None
        best = max(cands, key=_approvals)
        distinct = len({id(c) for c in cands})
        return _summary(best, how if distinct == 1 else "ambiguous", distinct)


def _approvals(rec: dict) -> int:
    """Strongest single year, so a partial newest year doesn't drag it down."""
    return max((y["initial"] + y["continuing"] for y in rec["years"].values()), default=0)


def _summary(rec: dict, match: str, candidates: int) -> dict:
    year, best = max(rec["years"].items(), key=lambda kv: kv[1]["initial"] + kv[1]["continuing"])
    return {"employer": rec["employer"], "year": int(year), "initial": best["initial"],
            "continuing": best["continuing"], "years": rec["years"],
            "match": match, "candidates": candidates}


def update_h1b() -> None:
    """USCIS sits behind a bot filter that rejects Python's TLS fingerprint,
    so the yearly files are fetched with curl, which ships with Windows."""
    import subprocess
    H1B_DIR.mkdir(exist_ok=True)
    for year in H1B_YEARS:
        url = H1B_URL.format(year=year)
        dest = H1B_DIR / f"h1b_datahubexport-{year}.csv"
        tmp = dest.with_suffix(".tmp")
        subprocess.run(["curl", "-sSLf", "--max-time", "180", "-A",
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "-o", str(tmp), url],
                       check=True)
        with tmp.open(encoding="utf-8-sig") as f:
            if not f.readline().startswith('"Fiscal Year"'):
                tmp.unlink()
                raise RuntimeError(f"unexpected content from {url}")
        tmp.replace(dest)
        log(f"h1b: saved FY{year} ({dest.stat().st_size // 1024} KB)")


# --------------------------------------------------------------------------
# Dashboard

def render(jobs: list[dict], meta: dict) -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps({"jobs": jobs, "meta": meta}, separators=(",", ":")).replace("</", "<\\/")
    page = template.replace("/*__DATA__*/null", payload)
    DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    # The helper re-renders after a settings change while a scheduled run may
    # be rendering too; each writes its own temp file.
    tmp = DASHBOARD_PATH.with_name(f"{DASHBOARD_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(DASHBOARD_PATH)


def public_settings(s: dict) -> dict:
    """The part of settings.json the dashboard page carries: roles for the
    switcher and visa badges, scoring weights, tailoring defaults, notices.
    No paths or notification topics."""
    return {"roles": s["roles"], "active_role": s["active_role"], "scoring": s["scoring"],
            "tailoring": {k: s["tailoring"][k] for k in ("backend", "draft_backend")}, "notices": s["notices"],
            "setup": s["setup"], "window": {"days": s["radar"]["dashboard_days"], "program_days": s["radar"]["program_days"]}}


def needs_visa_data(s: dict) -> bool:
    return any(r["visa"]["status"] != "authorized" for r in s["roles"])


def build_dashboard(conn: sqlite3.Connection, h1b: "H1BIndex", run: dict) -> list[dict]:
    """Scores, tags and renders the stored jobs. run: started, generated,
    previous_run, first_run, boards, failed, missing, new."""
    stats = corpus_stats(conn)
    rows = load_for_dashboard(conn, run["started"], run["previous_run"], h1b, stats)
    save_scoring(conn, rows, stats, run["generated"])
    render(rows, {
        **{k: run[k] for k in ("generated", "previous_run", "first_run", "boards", "failed", "missing", "new")},
        "h1b_years": h1b.years,
        "skills": json.loads(SKILLS_PATH.read_text(encoding="utf-8")),
        "skill_w": stats["skill_w"],
        "term_w": {t: stats["term_w"][t] for t in {k[0] for r in rows for k in r["kw"]} if t in stats["term_w"]},
        "settings": public_settings(SETTINGS),
    })
    return rows


def rerender(store_: config.Store | None = None) -> int:
    """Rebuilds dashboard.html from stored jobs without fetching, so changed
    settings (roles, visa, scoring) show at once. Returns jobs shown."""
    configure(store_ or config.store())
    companies = load_companies(SETTINGS)
    DISPLAY_NAMES.update(companies.pop("display_names", {}))
    h1b = H1BIndex.load(companies.pop("h1b_aliases", {})) if needs_visa_data(SETTINGS) else H1BIndex()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        with conn:
            migrate(conn)
        last = conn.execute("SELECT id, started, finished, boards, failed, new FROM runs "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        if not last:
            # Nothing fetched yet: an empty page, so a new install opens on its setup steps.
            render([], {"generated": None, "previous_run": None, "first_run": None, "boards": 0, "failed": 0,
                        "missing": 0, "new": 0, "h1b_years": list(h1b.years),
                        "skills": json.loads(SKILLS_PATH.read_text(encoding="utf-8")), "skill_w": {}, "term_w": {},
                        "settings": public_settings(SETTINGS)})
            return 0
        prev = conn.execute("SELECT MAX(finished) FROM runs WHERE id < ?", (last[0],)).fetchone()[0]
        first_run = conn.execute("SELECT MIN(started) FROM runs").fetchone()[0]
        with conn:
            rows = build_dashboard(conn, h1b, {
                "started": last[1], "generated": last[2], "previous_run": prev, "first_run": first_run,
                "boards": last[3], "failed": last[4], "missing": 0, "new": last[5] if prev is not None else 0})
        return len(rows)
    finally:
        conn.close()


# --------------------------------------------------------------------------

def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    if sys.stdout:  # None under pythonw (scheduled, windowless runs)
        print(line)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


class RunStatus:
    """This run's progress as a JSON file, replaced atomically: phase
    (starting, visa-data, fetching, saving, done, skipped, error), boards
    done and in all, jobs found, and the totals once done."""

    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self.data: dict = {"pid": os.getpid(), "started": time.time(), "phase": "starting"}
        self._last = 0.0

    def set(self, throttle: bool = False, **kw) -> None:
        self.data.update(kw, updated=time.time())
        if not self.path or (throttle and time.monotonic() - self._last < 0.5):
            return
        self._last = time.monotonic()
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            for _ in range(5):
                try:
                    tmp.replace(self.path)
                    break
                except PermissionError:     # Windows: the reader has it open this instant
                    time.sleep(0.05)
        except OSError:
            pass


STATUS = RunStatus(None)
SKIPPED = 3     # exit code when another run holds the lock


def load_state(now: float) -> dict[str, tuple[float, str]]:
    """Fills the detail and robots.txt caches from the db. Returns the boards
    still cooling down after a throttle: board -> (skip until, last error)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:
            migrate(conn)
        DETAILS.update((url, json.loads(p)) for url, p in conn.execute("SELECT url, posting FROM details"))
        ROBOTS.update((host, (fetched, body)) for host, fetched, body in
                      conn.execute("SELECT host, fetched, body FROM robots"))
        return {board: (until, err) for board, until, err in conn.execute(
            "SELECT board, skip_until, last_error FROM board_state WHERE skip_until > ?", (now,))}
    finally:
        conn.close()


def save_state(conn: sqlite3.Connection, res: FetchResult, now: float) -> None:
    conn.executemany("INSERT OR REPLACE INTO details (url, fetched, posting) VALUES (?, ?, ?)",
                     [(url, now, json.dumps(p, separators=(",", ":"))) for url, p in NEW_DETAILS.items()])
    conn.execute("DELETE FROM details WHERE fetched < ?", (now - PURGE_AFTER_DAYS * 86400,))
    conn.executemany("INSERT OR REPLACE INTO robots (host, fetched, body) VALUES (?, ?, ?)",
                     [(host, *ROBOTS[host]) for host in ROBOTS_FETCHED])
    conn.execute("DELETE FROM robots WHERE fetched < ?", (now - 7 * 86400,))
    # A board whose host keeps throttling is left alone for a while instead of
    # being asked again every run; any run without a throttle clears it.
    strikes = dict(conn.execute("SELECT board, strikes FROM board_state"))
    for board in res.throttled:
        reason = next((f for f in res.failed if f.startswith(board + " (")), "")
        n = strikes.get(board, 0) + 1
        conn.execute("INSERT OR REPLACE INTO board_state (board, strikes, skip_until, last_error) "
                     "VALUES (?, ?, ?, ?)",
                     (board, n, now + min(COOLDOWN_MAX, COOLDOWN_BASE * 2 ** (n - 1)), reason))
    conn.executemany("DELETE FROM board_state WHERE board = ?",
                     [(b,) for b in res.attempted if b not in res.throttled])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="list board tokens that 404")
    ap.add_argument("--update-h1b", action="store_true", help="re-download USCIS H-1B data")
    ap.add_argument("--render", action="store_true", help="rebuild the dashboard from stored jobs, no fetching")
    ap.add_argument("--status-file", help="keep this run's progress in this JSON file")
    ap.add_argument("--funnel", action="store_true", help="show where the last run's postings dropped out")
    ap.add_argument("--feed-out", metavar="DIR", help="after the run, publish the shared job feed into DIR")
    args = ap.parse_args(argv)

    global STATUS
    STATUS = RunStatus(args.status_file)
    settings = config.store()
    configure(settings)
    for d in {DB_PATH.parent, LOG_PATH.parent, DASHBOARD_PATH.parent}:
        d.mkdir(parents=True, exist_ok=True)
    for w in settings.warnings:
        log(f"settings: {w}")
    if args.render:
        log(f"rendered {rerender(settings)} jobs")
        return 0
    if args.funnel:
        print(funnel_report())
        return 0

    lock = oslock.FileLock(DB_PATH.parent / "radar.lock")
    if not lock.acquire():
        log("another run is still going; skipping this one")
        STATUS.set(phase="skipped")
        return SKIPPED
    STATUS.set()

    have_h1b = any((H1B_DIR / f"h1b_datahubexport-{y}.csv").exists() for y in H1B_YEARS)
    if args.update_h1b or (not have_h1b and needs_visa_data(SETTINGS)):
        STATUS.set(phase="visa-data")
        update_h1b()
        if args.update_h1b:
            return 0

    started = time.time()
    cooling = load_state(started)
    import discover     # imports radar itself, so not at module level
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        discover.migrate(conn)
        tracked, tracked_names = discover.tracked(conn, started)
        # The shared feed, when there is one: it searched the boards, so this
        # copy only reads the companies its user added. A feed that can't be
        # read or has gone stale means searching everything here, as before.
        pulled = None
        url = SETTINGS["radar"]["feed_url"]
        if url and not args.check and not args.feed_out:
            STATUS.set(phase="syncing")
            try:
                pulled = feed.pull(conn, url, started)
            except feed.FeedError as e:
                log(f"feed: {e}; searching every board from here this run")
    finally:
        conn.close()
    companies = load_companies(SETTINGS, None if pulled else tracked, None if pulled else tracked_names)
    DISPLAY_NAMES.update(companies.pop("display_names", {}))
    h1b_aliases = companies.pop("h1b_aliases", {})
    if pulled:
        added = SETTINGS["companies"]["added"]
        companies = {src: [t for t in toks if t in added.get(src, [])] if src in FETCHERS else toks
                     for src, toks in companies.items()}
        log(f"feed: version {pulled['version']}{' (snapshot)' if pulled['snapshot'] else ''}, "
            f"{pulled['upserted']} postings added or changed, {pulled['closed']} closed")
    if args.check:
        cooling = {}        # --check tries every board, cooling down or not
    STATUS.set(phase="fetching", done=0, total=0, jobs=0)
    res = fetch_all(companies, set(cooling), simplify=SETTINGS["radar"]["simplify_feed"] and not pulled,
                    progress=lambda done, total, n: STATUS.set(done < total, done=done, total=total, jobs=n))
    jobs = dedupe(res.jobs)
    missing, failed = res.missing, res.failed

    if args.check:
        conn = sqlite3.connect(DB_PATH)
        try:
            with conn:
                save_state(conn, res, time.time())
        finally:
            conn.close()
        print(f"{len(missing)} missing boards:")
        for m in missing:
            print("  ", m)
        print(f"{len(failed)} failed boards:")
        for f in failed:
            print("  ", f)
        print(f"{len(jobs)} matching jobs")
        return 0

    STATUS.set(phase="saving")
    h1b = H1BIndex.load(h1b_aliases) if needs_visa_data(SETTINGS) else H1BIndex()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        migrate(conn)
        prev, first_run = conn.execute("SELECT MAX(finished), MIN(started) FROM runs").fetchone()
        with conn:
            new = store(conn, jobs, started)
            save_state(conn, res, started)
            discover.record_run(conn, [b for b in res.attempted if b in res.kept or b in res.failed],
                                res.kept, res.missing, started)
            finished = time.time()
            boards = (sum(len(set(v)) for k, v in companies.items() if k in FETCHERS)
                      + int(SETTINGS["radar"]["simplify_feed"] and not pulled))
            if pulled:
                boards += pulled["boards"]
                new += pulled["new"]
            n_failed = len(failed) + len(missing) + (pulled["failed"] if pulled else 0)
            conn.execute(
                "INSERT INTO runs (started, finished, boards, failed, matched, new) VALUES (?,?,?,?,?,?)",
                (started, finished, boards, n_failed, len(jobs), new),
            )
            conn.executemany("INSERT OR REPLACE INTO run_funnel VALUES (?, ?, ?, ?, ?)",
                             [(started, *row) for row in FUNNEL.rows()])
            conn.execute("DELETE FROM run_funnel WHERE started < ?", (started - PURGE_AFTER_DAYS * 86400,))
        with conn:
            build_dashboard(conn, h1b, {
                "started": started, "generated": finished, "previous_run": prev, "first_run": first_run or started,
                "boards": boards, "failed": n_failed - len(missing), "missing": len(missing),
                "new": new if prev is not None else 0})
        if args.feed_out:
            STATUS.set(phase="publishing")
            index = feed.publish(conn, args.feed_out, finished, boards=boards, failed=n_failed,
                                 stale_days=STALE_AFTER_DAYS, window_days=max(DASHBOARD_DAYS, PROGRAM_DAYS))
            log(f"feed: published version {index['version']}, {index['postings']} postings")
    finally:
        conn.close()

    log(f"run ok: {len(jobs)} matches, {new} new, {len(failed)} failed, "
        f"{len(missing)} missing boards, {finished - started:.1f}s")
    log("  funnel " + funnel_line(FUNNEL.totals()) + "; dated this week: " + funnel_line(FUNNEL.totals(True)))
    slow = sorted(res.seconds.items(), key=lambda kv: -kv[1])[:5]
    log(f"  boards: {len(res.attempted)} read, {sum(len(b) for b in tracked.values())} of them discovered; "
        "slowest " + ", ".join(f"{b} {s:.0f}s" for b, s in slow))
    if not pulled:
        discover_step(discover)     # with a feed, its search finds new boards
    for f in failed:
        log(f"  failed {f}")
    for m in missing:
        log(f"  missing {m}")
    for board, (until, err) in sorted(cooling.items()):
        log(f"  skipped {board} until {datetime.fromtimestamp(until):%m-%d %H:%M}, last: {err}")
    STATUS.set(phase="done", finished=finished, matched=len(jobs), new=new, failed=len(failed),
               missing=len(missing), boards=boards)
    return 0


DISCOVER_BUDGET = 60        # seconds per run spent checking newly found boards


def discover_step(discover) -> None:
    """Once a day, collect candidate boards from the public lists; every run,
    check a minute's worth of them. Never fails the run."""
    STATUS.set(phase="discovering")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        now = time.time()
        if discover.harvest_due(conn, now):
            known = {f"{s}:{t}" for s, toks in load_companies(SETTINGS).items() if s in FETCHERS for t in toks}
            with conn:
                added = discover.harvest(conn, known, now)
            log("discover: new candidate boards " + ", ".join(f"{k} {v}" for k, v in added.items()))
        tally = discover.check_new(conn, DISCOVER_BUDGET)
        if tally:
            log("discover: checked " + ", ".join(f"{n} {s}" for s, n in tally.most_common()))
    except Exception as e:
        log(f"discover: stopped ({type(e).__name__}: {e})")
    finally:
        conn.close()


def funnel_line(t: dict[str, int]) -> str:
    return (f"{t['seen']} seen, -{t['title']} title, -{t['level']} level, -{t['location']} location, "
            f"-{t['details']} details, {t['kept']} kept")


def funnel_report(started: float | None = None) -> str:
    """The funnel of one stored run (the latest by default), per source."""
    conn = sqlite3.connect(DB_PATH)
    try:
        migrate(conn)
        started = started or conn.execute("SELECT MAX(started) FROM run_funnel").fetchone()[0]
        rows = conn.execute("SELECT source, stage, n_all, n_fresh FROM run_funnel WHERE started = ?",
                            (started,)).fetchall() if started else []
    finally:
        conn.close()
    if not rows:
        return "No funnel recorded yet; it is kept from the next full run on."
    table: dict[str, dict[str, tuple[int, int]]] = {}
    for src, stage, a, f in rows:
        table.setdefault(src, {})[stage] = (a, f)
    cols = FUNNEL_STAGES
    out = [f"Run of {datetime.fromtimestamp(started):%Y-%m-%d %H:%M}. Each cell: all seen / dated in the "
           f"last {FRESH_DAYS} days. Drop columns count where a posting left.",
           f"{'source':16}" + "".join(f"{c:>16}" for c in cols)]
    total = {c: [0, 0] for c in cols}
    for src in sorted(table, key=lambda s: -table[s].get("seen", (0, 0))[0]):
        cells = []
        for c in cols:
            a, f = table[src].get(c, (0, 0))
            total[c][0] += a
            total[c][1] += f
            cells.append(f"{a}/{f}")
        out.append(f"{src:16}" + "".join(f"{x:>16}" for x in cells))
    out.append(f"{'total':16}" + "".join(f"{f'{a}/{f}':>16}" for a, f in total.values()))
    return "\n".join(out)


def run(argv: list[str] | None = None) -> int:
    """main(), with a crash logged and reported in the status file."""
    try:
        return main(argv)
    except Exception as e:
        import traceback
        log("run failed:\n" + traceback.format_exc())
        STATUS.set(phase="error", error=f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(run())
