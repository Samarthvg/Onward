"""Job radar: pulls entry-level US software engineering postings straight from
company applicant tracking systems, keeps a SQLite history, and renders a
static dashboard of the freshest roles.

Usage:
    python radar.py            fetch, store, render dashboard.html
    python radar.py --check    list board tokens that no longer resolve
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import html
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import reqparse

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

DASHBOARD_DAYS = 7          # postings older than this are not shown at all
STALE_AFTER_DAYS = 2        # unseen for this long means the posting was closed
PURGE_AFTER_DAYS = 45       # rows older than this are deleted
WORKERS = 24
TIMEOUT = 25
USER_AGENT = "jobradar/1.0 (personal job search)"


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


# --------------------------------------------------------------------------
# Filters

SWE_TITLE = re.compile(
    r"\b(software|sde|swe|developer|back[- ]?end|front[- ]?end|full[- ]?stack|"
    r"platform|infrastructure|mobile|ios|android|devops|site reliability|sre|"
    r"machine learning|ml|ai|data|founding|forward deployed|"
    r"distributed systems|cloud|security|web|application|compiler|embedded software)\b",
    re.I,
)
ENGINEER_WORD = re.compile(r"\b(engineer|engineering|developer|sde|swe|programmer)\b", re.I)
NOT_SWE = re.compile(
    r"\b(sales|solutions?|support|customer|field|hardware|mechanical|electrical|"
    r"manufacturing|technician|recruit\w*|account|marketing|designer|analyst|"
    r"quality assurance|qa|validation|test engineer|network engineer|process|"
    r"civil|chemical|rf|optical|facilities|propulsion|avionics|structures|"
    r"scientist|mission|integration|deployment strategist|softgoods|textile|industrial design|"
    r"product design|mechanical design)\b",
    re.I,
)
NOT_ENTRY = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|management|director|head|vp|"
    r"vice president|architect|distinguished|fellow|chief|ii|iii|iv|v|"
    r"[2-6]|l[4-9]|e[4-9]|ic[3-9]|level [2-9]|intern|internship|co-?op|"
    r"apprentice|phd|postdoc|contract(or)?|part[- ]time|tech lead|expert|experienced|"
    r"internships?|smts|lmts|pmts|mts [2-6])\b",
    re.I,
)
ENTRY_HINT = re.compile(
    r"\b(new grad\w*|new college|graduate|entry[- ]level|junior|jr\.?|"
    r"early[- ]career|university|campus|associate|i|1|level 1|l3|e3|ic1|ic2|"
    r"20(2[6-8]) start)\b",
    re.I,
)
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

US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
US_STATE_NAMES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming",
]
US_CITIES = [
    "san francisco", "sf", "bay area", "new york", "nyc", "seattle", "boston", "austin",
    "chicago", "los angeles", "denver", "atlanta", "palo alto", "mountain view",
    "menlo park", "sunnyvale", "san jose", "santa clara", "redmond", "bellevue",
    "kirkland", "san diego", "portland", "pittsburgh", "philadelphia", "miami", "dallas",
    "houston", "phoenix", "salt lake city", "raleigh", "durham", "nashville",
    "minneapolis", "detroit", "ann arbor", "washington dc", "arlington", "reston",
    "mclean", "baltimore", "columbus", "boulder", "irvine", "san mateo", "redwood city",
    "foster city", "south san francisco", "oakland", "berkeley", "brooklyn", "jersey city",
    "hoboken", "stamford", "cambridge, ma", "somerville", "waltham", "hawthorne",
    "el segundo", "costa mesa", "culver city", "santa monica", "playa vista",
]
US_MARKER = re.compile(
    r"\b(united states|usa|u\.s\.a?\.?|us|america|amer|north america|"
    + "|".join(re.escape(s) for s in US_STATE_NAMES + US_CITIES)
    + r")\b|,\s*(" + "|".join(US_STATES) + r")\b",
    re.I,
)
NON_US = re.compile(
    r"\b(canada|toronto|vancouver|montreal|ontario|quebec|london|uk|united kingdom|"
    r"england|scotland|ireland|dublin|india|bangalore|bengaluru|hyderabad|pune|"
    r"chennai|mumbai|delhi|gurgaon|gurugram|noida|germany|berlin|munich|hamburg|"
    r"france|paris|singapore|japan|tokyo|australia|sydney|melbourne|brazil|"
    r"são paulo|sao paulo|mexico|poland|warsaw|krakow|netherlands|amsterdam|spain|"
    r"madrid|barcelona|israel|tel aviv|emea|apac|latam|europe|china|beijing|"
    r"shanghai|shenzhen|korea|seoul|taiwan|taipei|philippines|manila|argentina|"
    r"buenos aires|colombia|bogota|romania|bucharest|switzerland|zurich|geneva|"
    r"sweden|stockholm|denmark|copenhagen|portugal|lisbon|italy|milan|czech|prague|"
    r"ukraine|serbia|belgrade|costa rica|chile|santiago|uae|dubai|abu dhabi|"
    r"new zealand|hong kong|vietnam|indonesia|jakarta|malaysia|thailand|bangkok|"
    r"turkey|istanbul|egypt|nigeria|kenya|south africa|finland|helsinki|norway|"
    r"oslo|austria|vienna|belgium|brussels|hungary|budapest|greece|estonia|"
    r"lithuania|latvia|bulgaria|croatia|luxembourg|saudi|qatar|pakistan|"
    r"bangladesh|sri lanka|peru|uruguay)\b",
    re.I,
)
REMOTE = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh)\b", re.I)


STRONG_SWE = re.compile(
    r"\b(software|sde|swe|developer|back[- ]?end|front[- ]?end|full[- ]?stack)\b", re.I)


def is_swe_title(title: str) -> bool:
    if not ENGINEER_WORD.search(title):
        return False
    if STRONG_SWE.search(title):
        return not re.search(r"\b(sales|solutions|support|test|qa)\s+engineer|\b(marketing|recruit\w*)\b",
                             title, re.I)
    return bool(SWE_TITLE.search(title) and not NOT_SWE.search(title))


def is_entry_title(title: str) -> bool:
    if ENTRY_HINT.search(title) and re.search(r"new grad|entry|junior|early career|university", title, re.I):
        return True
    return not NOT_ENTRY.search(title)


def us_location(location: str, country: str | None) -> bool:
    if country:
        c = country.strip().lower()
        if c in {"us", "usa", "united states", "united states of america"}:
            return True
        if c and not REMOTE.search(location):
            return False
    has_us = bool(US_MARKER.search(location))
    if has_us:
        return True
    if NON_US.search(location):
        return False
    # Bare "Remote" with no country is treated as eligible.
    return bool(REMOTE.search(location))


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


def build_job(*, company, title, location, country, remote, url, source,
              posted_at, posted_exact, description, apply_url, verify_url,
              posted_field, posted_raw, sponsor_hint=None) -> Job | None:
    title = (title or "").strip()
    location = (location or "").strip()
    if not title or not is_swe_title(title) or not is_entry_title(title):
        return None
    if not us_location(location, country):
        return None
    yoe = min_years(description)
    entry = bool(ENTRY_HINT.search(title))
    if yoe is not None and yoe >= 3 and not entry:
        return None
    spons, note = sponsorship(description)
    # "e.g." / "i.e." would end the sentence early for the [^.] window.
    no_opt = bool(NO_OPT.search(re.sub(r"\b(e\.g|i\.e)\.", "eg", description or "", flags=re.I)))
    if spons == "unknown" and sponsor_hint:
        spons, note = sponsor_hint
    return Job(
        key=norm_key(company, title, location),
        company=company.strip(),
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
    )


# --------------------------------------------------------------------------
# HTTP

class BoardMissing(Exception):
    pass


def post_json(url: str, body: dict, retries: int = 3):
    return get_json(url, retries, body)


def get_json(url: str, retries: int = 3, body: dict | None = None):
    delay = 2.0
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise BoardMissing(url) from e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


# --------------------------------------------------------------------------
# Sources

def fetch_greenhouse(token: str) -> list[Job]:
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        first = parse_iso(j.get("first_published"))
        jid = j.get("id")
        job = build_job(
            company=j.get("company_name") or token,
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
        if job:
            out.append(job)
    return out


def fetch_lever(token: str) -> list[Job]:
    data = get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        if (cats.get("commitment") or "").lower() in {"intern", "internship", "part-time", "contract"}:
            continue
        locs = cats.get("allLocations") or [cats.get("location") or ""]
        created = j.get("createdAt")
        # Lever keeps requirement lists apart from the body, each with its own
        # heading ("Core Requirements", "Preferred"). They go in with those
        # headings and before the closing text (salary, benefits), so the
        # requirement parser reads them as the sections they are.
        lists = "\n".join(f"{l.get('text') or ''}:\n{strip_html(l.get('content'))}"
                          for l in j.get("lists") or [])
        desc = "\n".join(filter(None, [j.get("descriptionPlain"), lists, j.get("additionalPlain")]))
        job = build_job(
            company=display_name(token),
            title=j.get("text"),
            location=" / ".join(l for l in locs if l),
            country=j.get("country"),
            remote=(j.get("workplaceType") == "remote"),
            url=j.get("hostedUrl"),
            source="lever",
            posted_at=created / 1000 if created else None,
            posted_exact=created is not None,
            description=desc,
            apply_url=j.get("applyUrl"),
            verify_url=f"https://api.lever.co/v0/postings/{token}/{j.get('id')}",
            posted_field="createdAt",
            posted_raw=created,
        )
        if job:
            out.append(job)
    return out


def fetch_ashby(token: str) -> list[Job]:
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        if (j.get("employmentType") or "") in {"Intern", "PartTime", "Contract", "Temporary"}:
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry"))
        if len(locs) > 1:
            country = None  # multi-location postings: judge by the strings
        job = build_job(
            company=display_name(token),
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
            verify_url=f"https://api.ashbyhq.com/posting-api/job-board/{token}",
            posted_field="publishedAt",
            posted_raw=j.get("publishedAt"),
        )
        if job:
            out.append(job)
    return out


WORKDAY_MAX_PAGES = 25
WORKDAY_KNOWN: dict[str, dict] = {}   # posting URL -> stored fields, filled in main()
_POSTED_ON = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)


WORKDAY_UNDATED_PAGES = 5            # sites that hide "Posted N days ago" in listings


def _workday_days(posted_on: str | None) -> int | None:
    m = _POSTED_ON.search(posted_on or "")
    if not m:
        return None
    word = m.group(1).lower()
    return 0 if word == "today" else 1 if word == "yesterday" else int(m.group(2))


def fetch_workday(token: str) -> list[Job]:
    """token: "<tenant>.<wdN>/<site>", e.g. "nvidia.wd5/NVIDIAExternalCareerSite".

    With an empty search the CXS feed lists newest postings first (a few
    sites pin older "featured" postings on top), so pages are read until the
    last posting on a page is past the dashboard window. Detail records
    (location, description, posting date) are fetched only for new postings
    whose titles pass the filters; known ones reuse stored data."""
    host_part, site = token.split("/", 1)
    tenant = host_part.split(".")[0]
    base = f"https://{host_part}.myworkdayjobs.com"
    cxs = f"{base}/wday/cxs/{tenant}/{site}"
    company = display_name(tenant)
    cutoff = time.time() - DASHBOARD_DAYS * 86400
    out: list[Job] = []
    total = 0
    for page in range(WORKDAY_MAX_PAGES):
        data = post_json(f"{cxs}/jobs", {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": ""})
        postings = data.get("jobPostings") or []
        page_days = [_workday_days(p.get("postedOn")) for p in postings]
        undated = all(d is None for d in page_days)
        for p, days in zip(postings, page_days):
            if days is not None and days > DASHBOARD_DAYS:
                continue
            title = p.get("title") or ""
            if not (is_swe_title(title) and is_entry_title(title)):
                continue
            path = p.get("externalPath") or ""
            url = f"{base}/{site}{path}"
            known = WORKDAY_KNOWN.get(url)
            if known:
                location, desc, start, country = known["location"], known["description"], known["posted_raw"], None
            else:
                try:
                    info = get_json(f"{cxs}{path}").get("jobPostingInfo") or {}
                except BoardMissing:
                    continue
                locs = [info.get("location") or p.get("locationsText") or ""] + list(info.get("additionalLocations") or [])
                location = " / ".join(l for l in locs if l)
                desc = strip_html(info.get("jobDescription"))
                start = info.get("startDate") or ""
                country = None if len(locs) > 1 else ((info.get("country") or {}).get("descriptor"))
            posted = None
            if start:
                try:
                    posted = datetime.strptime(start[:10], "%Y-%m-%d").timestamp()
                except ValueError:
                    posted = None
            if posted is not None and posted < cutoff - 86400:
                continue
            job = build_job(
                company=company,
                title=title,
                location=location,
                country=country,
                remote=bool(REMOTE.search(location)),
                url=url,
                source="workday",
                posted_at=posted,
                posted_exact=False,          # Workday gives a date, never a time
                description=desc,
                apply_url=f"{url}/apply",
                verify_url=f"{cxs}{path}",
                posted_field="startDate",
                posted_raw=start,
            )
            if job:
                out.append(job)
        if page == 0:
            total = data.get("total") or 0   # Workday reports total on the first page only
        last = page_days[-1] if page_days else None
        if (not postings or (page + 1) * 20 >= total
                or (last is not None and last > DASHBOARD_DAYS)
                or (undated and page + 1 >= WORKDAY_UNDATED_PAGES)):
            break
    return out


SIMPLIFY_SPONSOR = {
    "Does Not Offer Sponsorship": ("no", "Listing says no sponsorship"),
    "U.S. Citizenship is Required": ("no", "Listing says U.S. citizenship required"),
    "Offers Sponsorship": ("yes", "Listing says sponsorship offered"),
}


def fetch_simplify(_: str = "") -> list[Job]:
    data = get_json(SIMPLIFY_URL)
    cutoff = time.time() - DASHBOARD_DAYS * 86400
    out = []
    for j in data:
        if not j.get("active") or not j.get("is_visible", True):
            continue
        posted = j.get("date_posted")
        if not posted or posted < cutoff:
            continue
        locs = j.get("locations") or []
        job = build_job(
            company=j.get("company_name", ""),
            title=j.get("title"),
            location=" / ".join(locs),
            country=None,
            remote=any(REMOTE.search(l) for l in locs),
            url=j.get("url"),
            source="simplify",
            posted_at=float(posted),
            posted_exact=True,
            description="",
            apply_url=j.get("url"),
            verify_url=SIMPLIFY_VERIFY_URL,
            posted_field="date_posted",
            posted_raw=posted,
            sponsor_hint=SIMPLIFY_SPONSOR.get(j.get("sponsorship", "")),
        )
        if job:
            out.append(job)
    return out


DISPLAY_NAMES: dict[str, str] = {}


def display_name(token: str) -> str:
    return DISPLAY_NAMES.get(token) or token.replace("-", " ").title()


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "workday": fetch_workday}


def fetch_all(companies: dict[str, list[str]]) -> tuple[list[Job], list[str], list[str]]:
    tasks = [(src, tok) for src, toks in companies.items() if src in FETCHERS
             for tok in dict.fromkeys(toks)]
    tasks.append(("simplify", ""))
    jobs: list[Job] = []
    missing: list[str] = []
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(fetch_simplify if src == "simplify" else FETCHERS[src], tok): (src, tok)
            for src, tok in tasks
        }
        for fut in as_completed(futures):
            src, tok = futures[fut]
            try:
                jobs.extend(fut.result())
            except BoardMissing:
                missing.append(f"{src}:{tok}")
            except Exception as e:  # one bad board must not sink the run
                failed.append(f"{src}:{tok} ({type(e).__name__}: {e})")
    return jobs, sorted(missing), sorted(failed)


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
}


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    have = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for col, decl in MIGRATIONS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
    # server.py finds the posting behind an application form by its apply_url.
    conn.execute("CREATE INDEX IF NOT EXISTS jobs_apply_url ON jobs(apply_url)")


def store(conn: sqlite3.Connection, jobs: list[Job], now: float) -> int:
    existing = {row[0] for row in conn.execute(
        "SELECT key FROM jobs WHERE key IN (SELECT value FROM json_each(?))",
        (json.dumps([j.key for j in jobs]),))}
    # posted_at and its evidence always mirror what the source says right now,
    # so the dashboard never shows a timestamp the Verify link can't back up.
    # first_seen is never overwritten: it is the radar's own independent record.
    conn.executemany(
        """INSERT INTO jobs (key, company, title, location, remote, url, source, posted_at,
                             posted_exact, sponsorship, sponsor_note, min_yoe, entry_hint,
                             first_seen, last_seen, apply_url, verify_url, posted_field,
                             posted_raw, skills, description, no_opt)
           VALUES (:key, :company, :title, :location, :remote, :url, :source, :posted_at,
                   :posted_exact, :sponsorship, :sponsor_note, :min_yoe, :entry_hint,
                   :now, :now, :apply_url, :verify_url, :posted_field, :posted_raw, :skills,
                   :description, :no_opt)
           ON CONFLICT(key) DO UPDATE SET
               url = excluded.url, source = excluded.source,
               posted_at = excluded.posted_at, posted_exact = excluded.posted_exact,
               posted_field = excluded.posted_field, posted_raw = excluded.posted_raw,
               apply_url = excluded.apply_url, verify_url = excluded.verify_url,
               sponsorship = excluded.sponsorship, sponsor_note = excluded.sponsor_note,
               no_opt = excluded.no_opt, min_yoe = excluded.min_yoe, skills = excluded.skills,
               description = CASE WHEN excluded.description != '' THEN excluded.description
                                  ELSE jobs.description END,
               last_seen = excluded.last_seen""",
        [{**asdict(j), "now": now} for j in jobs],
    )
    conn.execute("DELETE FROM jobs WHERE last_seen < ?", (now - PURGE_AFTER_DAYS * 86400,))
    return len({j.key for j in jobs} - existing)


DASHBOARD_COLS = [
    "key", "company", "title", "location", "remote", "url", "source", "posted_at", "posted_exact",
    "sponsorship", "sponsor_note", "no_opt", "min_yoe", "entry_hint", "first_seen", "apply_url",
    "verify_url", "posted_field", "posted_raw", "skills",
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


def load_for_dashboard(conn: sqlite3.Connection, now: float, prev_run: float | None,
                       h1b: "H1BIndex", stats: dict | None = None) -> list[dict]:
    rows = conn.execute(
        f"""SELECT {", ".join(DASHBOARD_COLS)}, length(description) > 200, description
           FROM jobs
           WHERE last_seen >= ? AND COALESCE(posted_at, first_seen) >= ?
           ORDER BY COALESCE(posted_at, first_seen) DESC""",
        (now - STALE_AFTER_DAYS * 86400, now - DASHBOARD_DAYS * 86400),
    ).fetchall()
    term_rx = term_patterns(stats["term_w"]) if stats else {}
    out = []
    for r in rows:
        d = dict(zip(DASHBOARD_COLS + ["has_jd", "_desc"], r))
        desc = d.pop("_desc") or ""
        d["has_jd"] = bool(d["has_jd"])
        d["skills"] = json.loads(d["skills"] or "{}")
        d["fresh"] = prev_run is not None and d["first_seen"] > prev_run
        d["h1b"] = h1b.lookup(d["company"])
        # The checklist the dashboard scores against: concrete keywords from
        # the posting's requirement lines, plus the degree it asks for.
        d["kw"] = reqparse.keywords(desc, d["title"], find_skills, term_rx) if d["has_jd"] else []
        d["degree"] = reqparse.degree(desc) if d["has_jd"] else None
        out.append(d)
    return out


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
    tmp = DASHBOARD_PATH.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(DASHBOARD_PATH)


# --------------------------------------------------------------------------

def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    if sys.stdout:  # None under pythonw (scheduled, windowless runs)
        print(line)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_workday_known() -> None:
    """Workday detail records cost one request each; reuse what's stored."""
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        if "description" not in cols:
            return
        for url, location, desc, raw in conn.execute(
                "SELECT url, location, description, posted_raw FROM jobs "
                # Descriptions stored before line structure was kept are re-fetched once.
                "WHERE source = 'workday' AND instr(description, char(10)) > 0"):
            WORKDAY_KNOWN[url] = {"location": location, "description": desc, "posted_raw": raw}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="list board tokens that 404")
    ap.add_argument("--update-h1b", action="store_true", help="re-download USCIS H-1B data")
    args = ap.parse_args()

    have_h1b = any((H1B_DIR / f"h1b_datahubexport-{y}.csv").exists() for y in H1B_YEARS)
    if args.update_h1b or not have_h1b:
        update_h1b()
        if args.update_h1b:
            return 0

    companies = json.loads(COMPANIES_PATH.read_text(encoding="utf-8"))
    DISPLAY_NAMES.update(companies.pop("display_names", {}))
    h1b_aliases = companies.pop("h1b_aliases", {})
    load_workday_known()
    started = time.time()
    jobs, missing, failed = fetch_all(companies)
    jobs = dedupe(jobs)

    if args.check:
        print(f"{len(missing)} missing boards:")
        for m in missing:
            print("  ", m)
        print(f"{len(failed)} failed boards:")
        for f in failed:
            print("  ", f)
        print(f"{len(jobs)} matching jobs")
        return 0

    h1b = H1BIndex.load(h1b_aliases)
    conn = sqlite3.connect(DB_PATH)
    try:
        migrate(conn)
        prev, first_run = conn.execute("SELECT MAX(finished), MIN(started) FROM runs").fetchone()
        with conn:
            new = store(conn, jobs, started)
            finished = time.time()
            boards = sum(len(set(v)) for k, v in companies.items() if k in FETCHERS) + 1
            conn.execute(
                "INSERT INTO runs (started, finished, boards, failed, matched, new) VALUES (?,?,?,?,?,?)",
                (started, finished, boards, len(failed) + len(missing), len(jobs), new),
            )
        stats = corpus_stats(conn)
        rows = load_for_dashboard(conn, started, prev, h1b, stats)
    finally:
        conn.close()

    render(rows, {
        "generated": finished,
        "previous_run": prev,
        "first_run": first_run or started,
        "boards": boards,
        "failed": len(failed),
        "missing": len(missing),
        "new": new if prev is not None else 0,
        "h1b_years": h1b.years,
        "skills": json.loads(SKILLS_PATH.read_text(encoding="utf-8")),
        "skill_w": stats["skill_w"],
        "term_w": {t: stats["term_w"][t] for t in {k[0] for r in rows for k in r["kw"]} if t in stats["term_w"]},
    })
    log(f"run ok: {len(jobs)} matches, {new} new, {len(failed)} failed, "
        f"{len(missing)} missing boards, {finished - started:.1f}s")
    for f in failed:
        log(f"  failed {f}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        log("run failed:\n" + traceback.format_exc())
        sys.exit(1)
