"""Grow the list of boards the radar reads, from public job lists.

Public new-grad and internship lists on GitHub link straight to postings on
company ATS boards, so every link names a board (ats.detect) at a company
hiring at this level. harvest() collects those boards, plus the boards
behind every job already stored, as candidates. check_new() asks each
candidate's ATS once whether the board exists and lists anything (through
radar's HTTP layer: robots.txt, per-host spacing, Retry-After), and tracked()
hands the boards that do to radar.py, which reads them alongside
companies.json.

Only the links are used. Nothing from a list is shown or republished; every
posting the radar keeps is read from the company's own board.

Run by hand:  python discover.py            harvest, then check every candidate
              python discover.py --report   counts by source and status
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import ats
import radar


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    kind: str           # "simplify" (listings.json) or "markdown" (a README table of links)
    terms: str          # what the list's licence and README allow, checked when it was added


# Checked 2026-10-21. None of the READMEs restrict use; only the ATS links
# (facts about where a company posts jobs) are taken from them.
FEEDS = [
    Feed("simplify-new-grad",
         "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
         "simplify", "No licence file; README has no usage terms. radar.py already reads this feed."),
    Feed("simplify-internships",
         "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
         "simplify", "No licence file; README has no usage terms."),
    Feed("vanshb03-new-grad", "https://raw.githubusercontent.com/vanshb03/New-Grad-2027/dev/README.md",
         "markdown", "MIT."),
    Feed("vanshb03-internships", "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/README.md",
         "markdown", "MIT."),
    Feed("speedyapply-new-grad",
         "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/NEW_GRAD_USA.md",
         "markdown", "No licence file; README has no usage terms."),
]

HARVEST_EVERY = 86400           # the lists change daily at most
RECHECK_EMPTY = 7 * 86400       # a board with nothing listed is asked again after a week
RECHECK_ERROR = 86400
RECHECK_DISALLOWED = 30 * 86400 # robots.txt rules rarely change
CHECK_WORKERS = 16

SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
    board    TEXT PRIMARY KEY,      -- "<source>:<token>", as companies.json spells it
    source   TEXT NOT NULL,
    token    TEXT NOT NULL,
    name     TEXT NOT NULL,         -- company name the lists use most for it
    origin   TEXT NOT NULL,         -- feed name, or "jobs" for a stored job's link
    links    INTEGER NOT NULL,      -- how many links in the lists point at it
    found    REAL NOT NULL,
    status   TEXT NOT NULL,         -- new, ok (tracked), empty, missing, disallowed, error
    checked  REAL,
    jobs     INTEGER,               -- postings the ATS listed at the last check
    error    TEXT NOT NULL DEFAULT '',
    fetched  REAL,                  -- last run that read it
    kept_at  REAL                   -- last run that kept a job from it
);
CREATE INDEX IF NOT EXISTS boards_status ON boards(status);
CREATE TABLE IF NOT EXISTS discover_runs (
    feed      TEXT PRIMARY KEY,
    harvested REAL NOT NULL,
    links     INTEGER NOT NULL,
    boards    INTEGER NOT NULL
);
"""


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# Harvest

MD_LINK = re.compile(r"https?://[^\s\"'<>)\]]+")
MD_NAME = re.compile(r"\*\*\[?([^*\]]+?)\]?(?:\([^)]*\))?\*\*|\[([^\]]+)\]\(")


def _markdown_links(text: str) -> list[tuple[str, str]]:
    """(company, link) from a README table. A row whose company cell is "↳"
    belongs to the company of the row above."""
    out, company = [], ""
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        first = cells[0] if cells else ""
        if first and "↳" not in first and not set(first) <= set("-: "):
            m = MD_NAME.search(first)
            company = re.sub(r"<[^>]+>", "", (m.group(1) or m.group(2)) if m else first).strip()
        for link in MD_LINK.findall(line):
            out.append((company, link))
    return out


def _feed_links(feed: Feed) -> list[tuple[str, str]]:
    raw = radar.fetch(feed.url, accept="*/*").decode("utf-8", "replace")
    if feed.kind == "simplify":
        return [(j.get("company_name") or "", j.get("url") or "") for j in json.loads(raw)]
    return _markdown_links(raw)


def _norm_board(board: str) -> str:
    """One spelling per board: Workday site names and Lever/Ashby tokens
    differ only in case between links to the same board, and an Eightfold
    token's search filters ("|filter_profession=...") don't make it another
    board."""
    source, _, token = board.lower().partition(":")
    if source == "eightfold":
        token = "|".join(token.split("|")[:2])
    return f"{source}:{token}"


def harvest(conn: sqlite3.Connection, known: set[str], now: float | None = None,
            feeds: list[Feed] = FEEDS) -> dict[str, int]:
    """Records every board the lists and stored jobs link to as a candidate.
    known: boards already read ("source:token", any case), which are skipped.
    Returns new candidates per feed."""
    now = now or time.time()
    known = {_norm_board(k) for k in known} | {_norm_board(r[0]) for r in conn.execute("SELECT board FROM boards")}
    found: dict[str, tuple[ats.Board, Counter, str]] = {}
    added: dict[str, int] = {}

    def take(origin: str, links: list[tuple[str, str]]) -> int:
        n = 0
        for company, link in links:
            b = ats.detect(link)
            if not b or b.source == "amazon":
                continue
            key = _norm_board(b.board)
            if key in known:
                continue
            if key not in found:
                found[key] = (b, Counter(), origin)
                n += 1
            if company.strip():
                found[key][1][company.strip()] += 1
        return n

    for feed in feeds:
        try:
            links = _feed_links(feed)
        except Exception as e:      # one unreachable list must not stop the others
            radar.log(f"discover: {feed.name} unreadable ({type(e).__name__}: {e})")
            continue
        added[feed.name] = take(feed.name, links)
        conn.execute("INSERT OR REPLACE INTO discover_runs VALUES (?, ?, ?, ?)",
                     (feed.name, now, len(links), added[feed.name]))
    added["jobs"] = take("jobs", [(c, u) for c, u, a in conn.execute("SELECT company, url, apply_url FROM jobs")
                                  for u in (u, a) if u])
    conn.executemany(
        "INSERT OR IGNORE INTO boards (board, source, token, name, origin, links, found, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'new')",
        [(b.board, b.source, b.token, _best_name(b, names), origin, sum(names.values()), now)
         for b, names, origin in found.values()])
    return added


def _best_name(b: ats.Board, names: Counter) -> str:
    named = [(n, c) for n, c in names.most_common() if n]
    return named[0][0] if named else ats.default_name(b)


def harvest_due(conn: sqlite3.Connection, now: float) -> bool:
    last = conn.execute("SELECT MIN(harvested) FROM discover_runs").fetchone()[0]
    return last is None or now - last >= HARVEST_EVERY


# --------------------------------------------------------------------------
# Check

def _check(source: str, token: str) -> tuple[str, int, str]:
    """(status, postings, error) from the board's own ATS."""
    try:
        jobs, _ = ats.CHECKS[source](token)
    except radar.BoardMissing:
        return "missing", 0, ""
    except radar.Disallowed as e:
        return "disallowed", 0, f"robots.txt: {e}"[:300]
    except Exception as e:      # throttled, robots.txt, network: asked again later
        return "error", 0, f"{type(e).__name__}: {e}"[:300]
    return ("ok" if jobs else "empty"), jobs, ""


def check_new(conn: sqlite3.Connection, budget: float | None = None, now: float | None = None,
              limit: int | None = None) -> Counter:
    """Checks candidates, most-linked first, until the time budget (seconds)
    runs out. Unchecked ones wait for the next call."""
    now = now or time.time()
    rows = conn.execute(
        """SELECT board, source, token FROM boards
           WHERE status = 'new'
              OR (status = 'error' AND checked < ?)
              OR (status = 'empty' AND checked < ?)
              OR (status = 'disallowed' AND checked < ?)
           ORDER BY status = 'new' DESC, links DESC""",
        (now - RECHECK_ERROR, now - RECHECK_EMPTY, now - RECHECK_DISALLOWED)).fetchall()
    if limit is not None:
        rows = rows[:limit]
    deadline = time.monotonic() + budget if budget else None
    tally: Counter = Counter()
    results = []

    def save() -> None:
        with conn:
            conn.executemany("UPDATE boards SET status = ?, checked = ?, jobs = ?, error = ? WHERE board = ?",
                             results)
        results.clear()
    with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
        it = iter(rows)
        pending = {}
        # Submit in a sliding window so a spent budget stops new checks.
        for row in it:
            pending[pool.submit(_check, row[1], row[2])] = row[0]
            if len(pending) >= CHECK_WORKERS * 2:
                break
        while pending:
            fut = next(as_completed(pending))
            board = pending.pop(fut)
            status, jobs, error = fut.result()
            results.append((status, time.time(), jobs, error, board))
            tally[status] += 1
            if len(results) >= 100:     # a long first pass keeps what it learned if stopped
                save()
            if deadline is None or time.monotonic() < deadline:
                row = next(it, None)
                if row:
                    pending[pool.submit(_check, row[1], row[2])] = row[0]
    save()
    return tally


# --------------------------------------------------------------------------
# Hand-off to radar.py

QUIET_EVERY = 2 * 3600          # a tracked board with no recent match is read every two hours
ACTIVE_DAYS = 14                # one kept job in this long makes a board read every run
NEW_PER_RUN = 400               # boards never read yet join a few hundred per run, most-linked first


def tracked(conn: sqlite3.Connection, now: float | None = None) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Tracked boards due this run, by source, and their display names.
    Boards that produced a match lately are read every run; the rest every
    QUIET_EVERY, so thousands of boards fit in a run."""
    now = now or time.time()
    rows = conn.execute(
        """SELECT source, token, name FROM boards
           WHERE status = 'ok' AND fetched IS NOT NULL AND (kept_at >= ? OR fetched <= ?)
           UNION ALL
           SELECT * FROM (SELECT source, token, name FROM boards
                          WHERE status = 'ok' AND fetched IS NULL ORDER BY links DESC LIMIT ?)""",
        (now - ACTIVE_DAYS * 86400, now - QUIET_EVERY + 300, NEW_PER_RUN)).fetchall()
    boards: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for source, token, name in rows:
        boards.setdefault(source, []).append(token)
        if name:
            names[token] = name
    return boards, names


def record_run(conn: sqlite3.Connection, read: list[str], kept: Counter, missing: list[str], now: float) -> None:
    """After a radar run: when each tracked board was read, which ones kept
    a job, and which turned out to be gone."""
    conn.executemany("UPDATE boards SET fetched = ? WHERE board = ?", [(now, b) for b in read])
    conn.executemany("UPDATE boards SET kept_at = ? WHERE board = ?", [(now, b) for b, n in kept.items() if n])
    conn.executemany("UPDATE boards SET status = 'missing', checked = ? WHERE board = ?",
                     [(now, b) for b in missing])


def report(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT source, status, COUNT(*) FROM boards GROUP BY source, status").fetchall()
    by: dict[str, Counter] = {}
    for source, status, n in rows:
        by.setdefault(source, Counter())[status] = n
    statuses = ["ok", "empty", "missing", "disallowed", "error", "new"]
    out = [f"{'source':16}" + "".join(f"{s:>11}" for s in statuses)]
    for source in sorted(by, key=lambda s: -by[s]["ok"]):
        out.append(f"{source:16}" + "".join(f"{by[source][s]:>11}" for s in statuses))
    total = sum(by.values(), Counter())
    out.append(f"{'total':16}" + "".join(f"{total[s]:>11}" for s in statuses))
    feeds = conn.execute("SELECT feed, harvested, links, boards FROM discover_runs ORDER BY feed").fetchall()
    out += [f"  {f}: {links} links, {b} new boards at {time.strftime('%Y-%m-%d %H:%M', time.localtime(h))}"
            for f, h, links, b in feeds]
    return "\n".join(out)


def known_boards(companies: dict) -> set[str]:
    return {f"{src}:{tok}" for src, toks in companies.items() if src in radar.FETCHERS for tok in toks}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--report", action="store_true", help="counts by source and status, nothing fetched")
    ap.add_argument("--budget", type=float, help="stop checking after this many seconds")
    args = ap.parse_args(argv)
    radar.configure(radar.config.store())
    conn = sqlite3.connect(radar.DB_PATH, timeout=30)
    try:
        radar.migrate(conn)
        migrate(conn)
        if not args.report:
            radar.load_state(time.time())
            with conn:
                added = harvest(conn, known_boards(radar.load_companies(radar.SETTINGS)))
            print("new candidates:", ", ".join(f"{k} {v}" for k, v in added.items()))
            tally = check_new(conn, args.budget)
            print("checked:", dict(tally))
        print(report(conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
