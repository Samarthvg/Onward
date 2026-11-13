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
    name = re.sub(r"[-_]+", " ", token).strip()
    # A short single word is almost always initials: "sig" -> SIG, "ibm" -> IBM.
    return name.upper() if re.fullmatch(r"[A-Za-z]{2,3}", name) else name.title()


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
    # (careers-acme.icims.com), not iCIMS's own site or the script and policy
    # hosts every iCIMS career site loads (www., cookie-policy-scripts.).
    (re.compile(r"https?://(?!(?:www|cookie-policy-scripts|static|cdn|assets|login|api|developers?|help|care|"
                r"community|marketplace|status|media|images|go|info)\.icims\.com)([\w-]+\.icims\.com)\b", re.I), "icims",
     lambda m: m[1].lower()),
    # Workable: apply.workable.com/<account>/ (and /j/<code> postings), or the
    # older <account>.workable.com, which redirects there.
    (re.compile(r"https?://apply\.workable\.com/(?!j/|api/)([\w-]+)", re.I), "workable", lambda m: m[1].lower()),
    (re.compile(r"https?://apply\.workable\.com/api/v\d/(?:widget/)?accounts/([\w-]+)", re.I), "workable",
     lambda m: m[1].lower()),
    (re.compile(r"https?://(?!(?:www|apply|resources|help|jobs|blog|careers|developers|engineering)\.)"
                r"([\w-]+)\.workable\.com\b", re.I), "workable", lambda m: m[1].lower()),
    # BambooHR: <company>.bamboohr.com/careers (www. is BambooHR's own careers page)
    (re.compile(r"https?://(?!(?:www|api|help|marketplace|partners)\.)([\w-]+)\.bamboohr\.com/(?:careers|jobs)", re.I),
     "bamboohr", lambda m: m[1].lower()),
    # Rippling: ats.rippling.com/[<locale>/]<board>/jobs, and its board API
    (re.compile(r"https?://ats\.rippling\.com/api/v2/board/([\w-]+)", re.I), "rippling", lambda m: m[1]),
    (re.compile(r"https?://ats\.rippling\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?!api/)([\w-]+)/jobs\b", re.I), "rippling",
     lambda m: m[1]),
    # Breezy HR: <company>.breezy.hr
    (re.compile(r"https?://(?!(?:www|app|marketplace|developer|help)\.)([\w-]+)\.breezy\.hr\b", re.I), "breezy",
     lambda m: m[1].lower()),
    # Paylocity: recruiting.paylocity.com/recruiting/jobs/All/<board id>[/<name>]
    (re.compile(r"https?://recruiting\.paylocity\.com/recruiting/jobs/All/([0-9a-f]{8}-[0-9a-f-]{27})", re.I),
     "paylocity", lambda m: m[1].lower()),
    # ADP Workforce Now career centers: ...recruitment.html?cid=<id>[&ccId=<id>]
    (re.compile(r"https?://workforcenow\.adp\.com/mascsr/default/(?:mdf/recruitment/recruitment\.html|careercenter/)"
                r"[^#]*?[?&]cid=([0-9a-f-]{36})(?:[^#]*?[?&]ccId=([\w]+))?", re.I),
     "adp", lambda m: m[1].lower() + (f"|{m[2]}" if m[2] else "")),
    # Jobvite: jobs.jobvite.com/<company>/... (and the older /careers/<company>/...)
    (re.compile(r"https?://jobs\.jobvite\.com/careers/([\w-]+)", re.I), "jobvite", lambda m: m[1].lower()),
    (re.compile(r"https?://jobs\.jobvite\.com/(?!careers/|__)([\w-]+)", re.I), "jobvite", lambda m: m[1].lower()),
    # JazzHR: <company>.applytojob.com/apply
    (re.compile(r"https?://(?!www\.)([\w-]+)\.applytojob\.com/apply", re.I), "jazzhr", lambda m: m[1].lower()),
    # Pinpoint: <company>.pinpointhq.com
    (re.compile(r"https?://(?!(?:www|app|help|developers)\.)([\w-]+)\.pinpointhq\.com\b", re.I), "pinpoint",
     lambda m: m[1].lower()),
    # Comeet: comeet.com/jobs/<company>/<company uid>; the board's public token
    # is on that page (complete_board fills it in).
    (re.compile(r"https?://(?:www\.)?comeet\.com/jobs/([\w-]+)/([0-9A-F]{2}\.[0-9A-F]{3})", re.I), "comeet",
     lambda m: f"{m[2].upper()}|{COMEET_TODO}{m[1].lower()}"),
    # Taleo: <company>.taleo.net/careersection/<section>/...[?portal=<id>]
    (re.compile(r"https?://([\w-]+)\.taleo\.net/careersection/(?!rest/)([^/?#]+)/[^#]*?[?&]portal=(\d+)", re.I),
     "taleo", lambda m: f"{m[1].lower()}|{m[2]}|{m[3]}"),
    (re.compile(r"https?://([\w-]+)\.taleo\.net/careersection/(?!rest/)([^/?#]+)/", re.I),
     "taleo", lambda m: f"{m[1].lower()}|{m[2]}|"),
    # Avature portals: <company>.avature.net/[<locale>/]<portal>/...
    (re.compile(r"https?://([\w-]+\.avature\.net)/((?:[a-z]{2}_[A-Z]{2}/)?[A-Za-z0-9]+)(?=/|$|\?)", re.I),
     "avature", lambda m: f"{m[1].lower()}|/{m[2]}"),
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


# A career site iCIMS runs on the company's own domain (careers.sig.com) loads
# these; its jobs are at that domain's /api/jobs, which _icims reads.
ICIMS_SITE = re.compile(r"cookie-policy-scripts\.icims\.com/|\.icims\.tools/", re.I)
# Links on a careers page that lead to its listings ("/careers/open-positions").
LISTING_LINK = re.compile(r"open-?(?:positions|roles|jobs)|openings|opportunit|vacanc|job-?search|search-?jobs|"
                          r"all-?jobs|current-?(?:jobs|openings)|(?:^|/)jobs?(?:/|$)|join-?(?:us|our-team)", re.I)
SOURCE_NAMES = {"paylocity": "Paylocity", "adp": "ADP", "comeet": "Comeet"}
COMEET_TODO = "?"           # a Comeet board whose token isn't known yet
COMEET_TOKEN = re.compile(r'"token"\s*:\s*"([0-9A-F]{16,})"', re.I)
TALEO_PORTAL = re.compile(r"[?&;]portal=(\d+)")
SF_SITE = re.compile(r"rmkcdn\.successfactors\.com|jobs2web\.com", re.I)
AVATURE_SITE = re.compile(r"avature\.net|avaturecdn|/portal/\d+/images/", re.I)
SITE_PATH = re.compile(r"^/((?:[a-z]{2}_[A-Z]{2}/)?[A-Za-z0-9]+)/(?:SearchJobs|JobDetail|job|search|go)\b")
PHENOM = re.compile(r"cdn\.phenompeople\.com|phenompeople", re.I)
HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)
MAX_FOLLOWED = 3            # pages followed from a careers page that names no board itself


def _site(host: str) -> str:
    """"careers.acme.com" -> "acme.com", to keep followed links on the company's own site."""
    parts = (host or "").lower().split(".")
    return ".".join(parts[-3:] if len(parts) > 2 and len(parts[-2]) <= 3 and len(parts[-1]) == 2 else parts[-2:])


def listing_links(url: str, page: str) -> list[str]:
    """Same-site links that look like the job listings, the likeliest first."""
    base = urllib.parse.urlparse(url)
    out: dict[str, int] = {}
    for href in HREF.findall(html.unescape(page)):
        link = urllib.parse.urljoin(url, href.strip())
        p = urllib.parse.urlparse(link)
        if p.scheme not in ("http", "https") or _site(p.hostname) != _site(base.hostname):
            continue
        if re.search(r"\.(?:png|jpe?g|svg|gif|css|js|pdf|woff2?|ico|xml)$", p.path, re.I):
            continue
        m = LISTING_LINK.search(p.path)
        clean = link.split("#")[0].rstrip("/")
        if m and clean != url.split("#")[0].rstrip("/"):
            # Open positions and job searches before a bare /jobs or "join us".
            rank = 0 if re.search(r"open|search|opening|current|all", m[0], re.I) else 1
            out[clean] = min(out.get(clean, rank), rank)
    return sorted(out, key=lambda k: out[k])


def board_on_page(url: str, page: str) -> Board | None:
    """The board a page shows: a career site the page itself is (iCIMS,
    SuccessFactors or Avature on the company's own domain), else the board
    it links to or embeds most."""
    parts = urllib.parse.urlparse(url)
    host = (parts.hostname or "").lower()
    if ICIMS_SITE.search(page) and host and not host.endswith(".icims.com"):
        return Board("icims", host)
    m = SITE_PATH.match(parts.path or "")
    if SF_SITE.search(page) and host and not host.endswith(".successfactors.com"):
        brand = f"|/{m[1]}" if m and "/" not in m[1] and m[1].lower() not in ("job", "search", "go") else ""
        return Board("successfactors", host + brand)
    if AVATURE_SITE.search(page) and m and host and not host.endswith(".avature.net") and \
            re.search(r"/(?:SearchJobs|JobDetail)\b", parts.path or ""):
        return Board("avature", f"{host}|/{m[1]}")
    return find_in_page(page)


def complete_board(b: Board, url: str) -> Board:
    """Boards a link names only in part: a Comeet board's public token and
    a Taleo career section's portal are on the board's own page."""
    if b.source == "comeet" and COMEET_TODO in b.token:
        uid, _, slug = b.token.partition("|")
        page = radar.get_text(f"https://www.comeet.com/jobs/{slug.lstrip(COMEET_TODO)}/{uid}")
        m = COMEET_TOKEN.search(page)
        if not m:
            return None
        return Board("comeet", f"{uid}|{m[1].upper()}", b.name or _title(slug.lstrip(COMEET_TODO)), b.note)
    if b.source == "taleo" and b.token.endswith("|"):
        tenant, section, _ = b.token.split("|")
        page = radar.get_text(f"https://{tenant}.taleo.net/careersection/{section}/jobsearch.ftl?lang=en")
        m = TALEO_PORTAL.search(page)
        if not m:
            return None
        return Board("taleo", f"{tenant}|{section}|{m[1]}", b.name, b.note)
    return b


def find_board(url: str) -> Board | None:
    """The board a URL points at; else the one its page shows, and when the
    page shows none (a careers page that only links on to its listings), the
    one on the first few same-site listing pages it links to."""
    b = detect(url)
    if b:
        return b
    page = radar.get_text(url)
    b = board_on_page(url, page)
    if b:
        return b
    links = listing_links(url, page)
    if PHENOM.search(page):
        # A Phenom career site, usually in front of Workday: its search page
        # embeds the first jobs with their apply links, which name the board.
        base = url.split("?")[0].split("#")[0].rstrip("/")
        prefix = re.match(r"https?://[^/]+(?:/[a-z]{2,6}/[a-z]{2})?", base, re.I).group(0)
        links = list(dict.fromkeys([f"{prefix}/search-results", f"{base}/search-results"] + links))
    for link in links[:MAX_FOLLOWED]:
        b = detect(link)
        if b:
            return b
        try:
            b = board_on_page(link, radar.get_text(link))
        except Exception:       # one link that won't open doesn't end the search
            continue
        if b:
            return b
    return None


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
    _, cxs = radar.workday_roots(host_part, site)
    try:
        data = radar.post_json(f"{cxs}/jobs",
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


def _workable(token: str) -> tuple[int, str]:
    # 404 for an account that doesn't exist; an account with nothing posted answers 200, no jobs.
    try:
        data = radar.get_json(radar.WORKABLE_WIDGET.format(urllib.parse.quote(token)), retries=2)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise radar.BoardMissing(token) from e
        raise
    return len({j.get("shortcode") for j in data.get("jobs") or []}), data.get("name") or ""


def _json_or_missing(url: str, body: dict | None = None, headers: dict | None = None):
    """JSON from a board's API; 404, or a page that isn't JSON (a redirect
    to the vendor's home page), means the board isn't there."""
    try:
        return radar.get_json(url, 2, body, headers)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise radar.BoardMissing(url) from e
        raise
    except ValueError as e:
        raise radar.BoardMissing(url) from e


def _bamboohr(token: str) -> tuple[int, str]:
    root = radar.BAMBOO_ROOT.format(token)
    info = _json_or_missing(f"{root}/company-info").get("result") or {}
    if info.get("inTrial"):
        raise radar.BoardMissing(token)         # a trial account shows BambooHR's sample openings
    return len(_json_or_missing(f"{root}/list").get("result") or []), info.get("name") or ""


def _rippling(token: str) -> tuple[int, str]:
    data = _json_or_missing(f"{radar.RIPPLING_API.format(urllib.parse.quote(token))}?page=0&pageSize={radar.RIPPLING_PAGE}")
    return len({i.get("id") for i in data.get("items") or []}) if (data.get("totalPages") or 1) <= 1 \
        else data.get("totalItems") or 0, ""


def _breezy(token: str) -> tuple[int, str]:
    data = _json_or_missing(f"https://{token}.breezy.hr/json")
    if not isinstance(data, list):
        raise radar.BoardMissing(token)
    name = next(((p.get("company") or {}).get("name") for p in data if (p.get("company") or {}).get("name")), "")
    return len(data), name


def _paylocity(token: str) -> tuple[int, str]:
    data = radar._page_json(radar.get_text(radar.PAYLOCITY_BOARD.format(token)), "window.pageData")
    if not isinstance(data, dict) or "Jobs" not in data:
        raise radar.BoardMissing(token)
    return len([j for j in data["Jobs"] if not j.get("IsInternal")]), (data.get("ModuleTitle") or "").strip()


def _adp(token: str) -> tuple[int, str]:
    data = _json_or_missing(f"{radar.ADP_API}?{radar._adp_query(token)}&$top=1&$skip=0")
    if "jobRequisitions" not in data:
        raise radar.BoardMissing(token)
    return (data.get("meta") or {}).get("totalNumber") or 0, ""


def _jobvite(token: str) -> tuple[int, str]:
    page = radar.get_text(f"https://jobs.jobvite.com/{token}/search/?p=0")
    if "jv-" not in page:           # an unknown company is sent on to Jobvite's own search
        raise radar.BoardMissing(token)
    return len(radar.JOBVITE_ROW.findall(page)), ""         # the first page's, up to 50


def _jazzhr(token: str) -> tuple[int, str]:
    page = radar.get_text(f"https://{token}.applytojob.com/apply")
    if "applytojob.com/apply" not in page and "resumator" not in page:
        raise radar.BoardMissing(token)
    return len(radar.JAZZ_ROW.findall(page)), ""


def _pinpoint(token: str) -> tuple[int, str]:
    return len(_json_or_missing(f"https://{token}.pinpointhq.com/postings.json").get("data") or []), ""


def _comeet(token: str) -> tuple[int, str]:
    uid, _, key = token.partition("|")
    url = radar.COMEET_API.format(urllib.parse.quote(uid)) + "?" + urllib.parse.urlencode({"token": key})
    try:
        data = radar.get_json(url, 2)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):        # "Account uid or token are not valid"
            raise radar.BoardMissing(token) from e
        raise
    return len({(p.get("name") or "").strip().lower() for p in data}) if isinstance(data, list) else 0, ""


def _successfactors(token: str) -> tuple[int, str]:
    root, prefix = radar._split_site(token)
    page = radar.get_text(f"{root}{prefix}/search/?q=")
    m = radar.SF_TOTAL.search(page)
    if not m and not radar.SF_ROW.search(page):
        raise radar.BoardMissing(token)
    return int(m.group(2).replace(",", "")) if m else len(radar.SF_ROW.findall(page)), ""


def _avature(token: str) -> tuple[int, str]:
    root, prefix = radar._split_site(token)
    locs = radar.SITEMAP_URL.findall(radar.get_text(f"{root}{prefix}/sitemap.xml"))
    jobs = {m.group(2) for loc, _ in locs if (m := radar.AVATURE_JOB.search(loc))}
    if not locs:
        raise radar.BoardMissing(token)
    return len(jobs), ""


def _taleo(token: str) -> tuple[int, str]:
    tenant, section, portal = (token.split("|") + ["", ""])[:3]
    data = _json_or_missing(f"https://{tenant}.taleo.net/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}",
                            {**radar.TALEO_BODY, "pageNo": 1}, {"tz": "GMT-05:00"})
    if data.get("careerSectionUnAvailable") or data.get("requisitionList") is None:
        raise radar.BoardMissing(token)
    return (data.get("pagingData") or {}).get("totalCount") or 0, ""


def _amazon(category: str) -> tuple[int, str]:
    q = urllib.parse.urlencode({"category[]": category, "result_limit": 1})
    data = radar.get_json(f"{radar.AMAZON_SEARCH}?{q}", retries=2)
    if not data.get("hits"):
        raise radar.BoardMissing(category)
    return data["hits"], "Amazon"


CHECKS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "workday": _workday,
          "smartrecruiters": _smartrecruiters, "oracle": _oracle, "eightfold": _eightfold,
          "icims": _icims, "amazon": _amazon, "workable": _workable, "bamboohr": _bamboohr, "rippling": _rippling,
          "breezy": _breezy, "paylocity": _paylocity, "adp": _adp, "jobvite": _jobvite, "jazzhr": _jazzhr,
          "pinpoint": _pinpoint, "comeet": _comeet, "successfactors": _successfactors, "avature": _avature,
          "taleo": _taleo}


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
    if b.source in ("successfactors", "avature"):
        return radar.site_name(b.token.split("|")[0])
    if b.source in ("taleo",):
        return _title(b.token.split("|")[0])
    if b.source in ("paylocity", "adp", "comeet"):
        # Ids, not names: the board's own name (Paylocity's), else the one typed in when adding it.
        return f"{SOURCE_NAMES[b.source]} board {b.token.split('|')[0][:8]}"
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
        if b:
            b = complete_board(b, url)
    except radar.Disallowed:
        return {"ok": False, "error": "That page's robots.txt doesn't allow reading it, so its job board can't be found."}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429):
            return {"ok": False, "error": f"That site turns away automated readers (HTTP {e.code}), so Onward can't look "
                                          "for its board there. Paste the link of one of its job postings instead."}
        return {"ok": False, "error": f"That page answered HTTP {e.code}. Check the link, or paste the job board's own URL."}
    except Exception as e:
        return {"ok": False, "error": f"Couldn't open that page ({type(e).__name__}). Paste the job board's own URL instead."}
    if not b:
        return {"ok": False, "error": "No supported job board found on that page. Supported: Greenhouse, Lever, Ashby, "
                                      "Workday, SmartRecruiters, Oracle, Eightfold, iCIMS, Workable, BambooHR, Rippling, "
                                      "Breezy HR, Paylocity, ADP, Jobvite, JazzHR, Pinpoint, Comeet, SuccessFactors, "
                                      "Avature and Taleo. Try the link of one job posting."}
    out = {"ok": True, **asdict(b), "board": b.board}
    if live:
        res = check(b)
        out.update(ok=res["ok"], jobs=res["jobs"], error=res["error"])
        if res["name"]:
            b.name = res["name"]
    out["name"] = default_name(b)
    return out
