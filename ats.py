"""Add a company: find the applicant tracking system and board token behind a
careers page URL, and check the board answers before it is saved.

detect() reads the URL alone. find_board() also opens the page when the URL
is a company's own careers site, and looks for the ATS links or embeds on
it. check() asks the ATS's public API for the board, through radar.py's
HTTP layer (robots.txt, per-host spacing, retries), and reports how many
postings it has and the company's name when the API gives one.

Tokens use the formats radar.py's fetchers and companies.json use.
"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
from collections import Counter
from dataclasses import asdict, dataclass

import radar


@dataclass
class Board:
    source: str
    token: str
    name: str = ""          # display name, when the URL or API tells
    note: str = ""          # caveat shown with the result

    @property
    def board(self) -> str:
        return f"{self.source}:{self.token}"


def _unq(s: str) -> str:
    return urllib.parse.unquote(s).strip()


def _domain(m: re.Match) -> str:
    """An Eightfold careers URL names the company domain in ?domain=; without
    it, <company>.eightfold.ai usually belongs to <company>.com."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(m.string).query)
    return ((q.get("domain") or [""])[0] or m[1].split(".")[0] + ".com").lower()


def _title(token: str) -> str:
    return re.sub(r"[-_]+", " ", token).strip().title()


# (pattern, source, token builder). Matched against the whole URL in order;
# the first hit wins. Hosts are matched case-insensitively.
PATTERNS: list[tuple[re.Pattern, str, callable]] = [
    # Greenhouse: board pages, embeds and the board API.
    (re.compile(r"https?://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/embed/job_(?:board|app)(?:/js)?\?(?:[^#]*&)?for=([\w-]+)", re.I),
     "greenhouse", lambda m: m[1]),
    (re.compile(r"https?://boards-api(?:\.eu)?\.greenhouse\.io/v1/boards/([\w-]+)", re.I), "greenhouse", lambda m: m[1]),
    (re.compile(r"https?://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?!embed\b)([\w-]+)", re.I), "greenhouse", lambda m: m[1]),
    # Lever
    (re.compile(r"https?://jobs\.lever\.co/([^/?#]+)", re.I), "lever", lambda m: _unq(m[1])),
    (re.compile(r"https?://api\.lever\.co/v0/postings/([^/?#]+)", re.I), "lever", lambda m: _unq(m[1])),
    # Ashby
    (re.compile(r"https?://jobs\.ashbyhq\.com/([^/?#]+)", re.I), "ashby", lambda m: _unq(m[1])),
    (re.compile(r"https?://api\.ashbyhq\.com/posting-api/job-board/([^/?#]+)", re.I), "ashby", lambda m: _unq(m[1])),
    # Workday: <tenant>.<wdN>.myworkdayjobs.com/[locale/]<site>, its CXS API,
    # and the myworkdaysite.com form of the same site.
    (re.compile(r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/wday/cxs/[\w-]+/([\w-]+)", re.I),
     "workday", lambda m: f"{m[1].lower()}.{m[2].lower()}/{m[3]}"),
    (re.compile(r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?!wday\b)([\w-]+)", re.I),
     "workday", lambda m: f"{m[1].lower()}.{m[2].lower()}/{m[3]}"),
    (re.compile(r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([\w-]+)/([\w-]+)", re.I),
     "workday", lambda m: f"{m[2].lower()}.{m[1].lower()}/{m[3]}"),
    # SmartRecruiters
    (re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)", re.I), "smartrecruiters", lambda m: _unq(m[1])),
    (re.compile(r"https?://api\.smartrecruiters\.com/v1/companies/([^/?#]+)", re.I), "smartrecruiters", lambda m: _unq(m[1])),
    # Oracle Recruiting Cloud
    (re.compile(r"https?://([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[\w-]+/sites/([\w-]+)", re.I),
     "oracle", lambda m: f"{m[1].lower()}/{m[2]}"),
    # Eightfold (Microsoft runs its careers site on it)
    (re.compile(r"https?://(apply\.careers\.microsoft\.com)\b", re.I), "eightfold", lambda m: f"{m[1].lower()}|microsoft.com"),
    (re.compile(r"https?://([\w-]+\.eightfold\.ai)\b", re.I), "eightfold", lambda m: f"{m[1].lower()}|{_domain(m)}"),
    # iCIMS portals
    (re.compile(r"https?://((?:[\w-]+\.)*[\w-]+\.icims\.com)\b", re.I), "icims",
     lambda m: re.sub(r"^www\.", "", m[1].lower())),
    # Amazon's own careers search, by job category
    (re.compile(r"https?://(?:www\.)?amazon\.jobs/[^#]*?category(?:%5B%5D|\[\])?=([\w-]+)", re.I), "amazon", lambda m: m[1].lower()),
    (re.compile(r"https?://(?:www\.)?amazon\.jobs\b", re.I), "amazon", lambda m: "software-development"),
]

UNSUPPORTED = [
    (re.compile(r"https?://jobs\.eu\.lever\.co/", re.I), "Lever's EU boards aren't supported yet."),
    (re.compile(r"https?://[^/]*(?:linkedin\.com|indeed\.com|glassdoor\.com)/", re.I),
     "Job sites like LinkedIn, Indeed and Glassdoor aren't company boards. Paste the company's own careers page."),
    (re.compile(r"https?://[^/]*(?:jobs\.apple\.com|google\.com|metacareers\.com)/", re.I),
     "This company's careers site doesn't allow automated reading, so Onward leaves it out."),
]


def detect(url: str) -> Board | None:
    """The board a URL points at, from the URL alone."""
    url = html.unescape((url or "").strip())
    for rx, source, token in PATTERNS:
        m = rx.match(url)
        if m:
            tok = token(m)
            note = ""
            if source == "amazon" and "category" not in url.lower():
                note = "Amazon is searched by job category; this adds software development."
            return Board(source, tok, "Amazon" if source == "amazon" else "", note)
    return None


def unsupported(url: str) -> str | None:
    return next((why for rx, why in UNSUPPORTED if rx.match(url or "")), None)


URL_IN_PAGE = re.compile(r"""(?:https?:)?//[^\s"'<>\\)]+""")


def find_in_page(page: str) -> Board | None:
    """The board a careers page links to or embeds most often."""
    found: Counter = Counter()
    first: dict[tuple[str, str], Board] = {}
    # Links, embeds, and URLs inside inline JSON ("https:\/\/...").
    for raw in URL_IN_PAGE.findall(html.unescape(page).replace("\\/", "/")):
        url = "https:" + raw if raw.startswith("//") else raw
        b = detect(url)
        if b:
            found[(b.source, b.token)] += 1
            first.setdefault((b.source, b.token), b)
    if not found:
        return None
    return first[found.most_common(1)[0][0]]


def find_board(url: str) -> Board | None:
    b = detect(url)
    if b:
        return b
    page = radar.get_text(url)
    return find_in_page(page)


# --------------------------------------------------------------------------
# Live check

def check(b: Board) -> dict:
    """{"ok", "jobs", "name", "error"} from the board's own API."""
    try:
        jobs, name = CHECKS[b.source](b.token)
    except radar.BoardMissing:
        return {"ok": False, "jobs": 0, "name": "", "error": "The ATS says this board doesn't exist."}
    except radar.Disallowed:
        return {"ok": False, "jobs": 0, "name": "", "error": "The site's robots.txt doesn't allow reading this board."}
    except radar.Throttled:
        return {"ok": False, "jobs": 0, "name": "", "error": "The ATS is rate limiting requests right now. Try again in a minute."}
    except Exception as e:      # network errors, unexpected API answers
        return {"ok": False, "jobs": 0, "name": "", "error": f"Couldn't read the board ({type(e).__name__}: {e})"}
    return {"ok": True, "jobs": jobs, "name": name or "", "error": ""}


def _greenhouse(token: str) -> tuple[int, str]:
    board = radar.get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}", retries=2)
    jobs = radar.get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", retries=2).get("jobs") or []
    return len(jobs), board.get("name") or ""


def _lever(token: str) -> tuple[int, str]:
    data = radar.get_json(f"https://api.lever.co/v0/postings/{urllib.parse.quote(token)}?mode=json", retries=2)
    if not isinstance(data, list):
        raise radar.BoardMissing(token)
    return len(data), ""


def _ashby(token: str) -> tuple[int, str]:
    data = radar.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(token)}", retries=2)
    return len([j for j in data.get("jobs") or [] if j.get("isListed") is not False]), ""


def _workday(token: str) -> tuple[int, str]:
    host_part, site = token.split("/", 1)
    tenant = host_part.split(".")[0]
    try:
        data = radar.post_json(f"https://{host_part}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                               {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}, retries=2)
    except urllib.error.HTTPError as e:
        if e.code in (400, 422):        # Workday's answer for a site name the tenant doesn't have
            raise radar.BoardMissing(token) from e
        raise
    if "jobPostings" not in data:
        raise radar.BoardMissing(token)
    return data.get("total") or 0, ""


def _smartrecruiters(token: str) -> tuple[int, str]:
    data = radar.get_json(f"{radar.SR_API}/{urllib.parse.quote(token)}/postings?limit=1", retries=2)
    total = data.get("totalFound") or 0
    if not total:
        # An unknown company id answers 200 with nothing in it.
        raise radar.BoardMissing(token)
    content = data.get("content") or [{}]
    return total, (content[0].get("company") or {}).get("name") or ""


def _oracle(token: str) -> tuple[int, str]:
    host, site = token.split("/", 1)
    sites = radar.get_json(f"{radar._oracle_api(host)}/recruitingCESites?onlyData=true&limit=500",
                           retries=2).get("items") or []
    hit = next((s for s in sites if s.get("StatusCode") == "ORA_ACTIVE"
                and site in (s.get("SiteNumber"), s.get("SiteURLName"))), None)
    if not hit:
        raise radar.BoardMissing(token)
    finder = f"findReqs;siteNumber={hit['SiteNumber']},limit=1"
    items = radar.get_json(f"{radar._oracle_api(host)}/recruitingCEJobRequisitions?onlyData=true&finder={finder}",
                           retries=2).get("items") or [{}]
    return items[0].get("TotalJobsCount") or 0, hit.get("SiteName") or ""


def _eightfold(token: str) -> tuple[int, str]:
    host, domain, *_ = token.split("|")
    q = urllib.parse.urlencode({"domain": domain, "query": "", "start": 0})
    data = radar.get_json(f"https://{host}/api/pcsx/search?{q}", retries=2).get("data") or {}
    if "positions" not in data:
        raise radar.BoardMissing(token)
    return data.get("count") or 0, ""


def _icims(host: str) -> tuple[int, str]:
    if host.endswith(".icims.com"):
        n = sum(1 for loc, _ in radar.SITEMAP_URL.findall(radar.get_text(f"https://{host}/sitemap.xml"))
                if radar.ICIMS_JOB.match(html.unescape(loc)))
        return n, ""
    data = radar.get_json(f"https://{host}/api/jobs?page=1&limit=1", retries=2)
    if "jobs" not in data:
        raise radar.BoardMissing(host)
    return data.get("totalCount") or 0, ""


def _amazon(category: str) -> tuple[int, str]:
    q = urllib.parse.urlencode({"category[]": category, "result_limit": 1})
    data = radar.get_json(f"{radar.AMAZON_SEARCH}?{q}", retries=2)
    if not data.get("hits"):
        raise radar.BoardMissing(category)
    return data["hits"], "Amazon"


CHECKS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "workday": _workday,
          "smartrecruiters": _smartrecruiters, "oracle": _oracle, "eightfold": _eightfold,
          "icims": _icims, "amazon": _amazon}


def default_name(b: Board) -> str:
    if b.name:
        return b.name
    if b.source == "workday":
        return _title(b.token.split(".")[0])
    if b.source == "oracle":
        return _title(b.token.split(".")[0])
    if b.source in ("icims", "eightfold"):
        host = b.token.split("|")[1] if b.source == "eightfold" else b.token     # the company domain
        return _title(re.sub(r"^(careers-|careers\.|jobs\.)", "", host).split(".")[0])
    return _title(b.token)


def lookup(url: str, live: bool = True) -> dict:
    """What the dashboard's Add a company box shows for a pasted URL."""
    url = (url or "").strip()
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    why = unsupported(url)
    if why:
        return {"ok": False, "error": why}
    try:
        b = find_board(url)
    except radar.Disallowed:
        return {"ok": False, "error": "That page's robots.txt doesn't allow reading it, so its job board can't be found."}
    except Exception as e:
        return {"ok": False, "error": f"Couldn't open that page ({type(e).__name__}). Paste the job board's own URL instead."}
    if not b:
        return {"ok": False, "error": "No supported job board found on that page. Supported: Greenhouse, Lever, Ashby, "
                                      "Workday, SmartRecruiters, Oracle, Eightfold, iCIMS. Try the link of one job posting."}
    out = {"ok": True, **asdict(b), "board": b.board}
    if live:
        res = check(b)
        out.update(ok=res["ok"], jobs=res["jobs"], error=res["error"])
        if res["name"]:
            b.name = res["name"]
    out["name"] = default_name(b)
    return out
