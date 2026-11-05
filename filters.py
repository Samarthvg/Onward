"""Roles: which postings each of the user's searches wants.

The user creates roles ("Full-stack", "ML") and gives each title keywords,
seniority levels, countries and cities, on-site/hybrid/remote, a pay floor
(for postings that state pay), words the posting must or must not mention,
and companies to keep or drop. radar.py keeps a posting when at least one
role wants it and tags it with every role that does; the dashboard's role
switcher shows one role, or all of them.

A new install starts with one role, SOFTWARE_KEYWORDS at entry level in the
US, which is the radar's original filter: it keeps exactly what the radar
kept before roles existed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# --------------------------------------------------------------------------
# Title keywords
#
# A role's title keywords are lines, read top to bottom; the first line a
# title fits decides, and a title that fits none is left out.
#   full stack/fullstack            a phrase; "/" separates alternatives
#   ios/android + engineer/developer  "+" joins parts that must all appear
#   - manager/director              a leading "-" leaves matching titles out
#   recruit*                        "*" ends a word with any ending
# Words of a phrase must appear together, in order. Case and punctuation
# don't matter: "Full-Stack" is the words "full stack".

WORD = re.compile(r"[a-z0-9]+")


# Language names that punctuation would otherwise erase: "C++" reads as the
# word "cpp" in titles and keywords alike, so "c++ + developer" works too.
LANG_NAMES = ((re.compile(r"(?<![a-z0-9])c\+\+", re.I), " cpp "), (re.compile(r"(?<![a-z0-9])([cf])#", re.I), r" \1sharp "),
              (re.compile(r"\.net(?![a-z0-9])", re.I), " dotnet "))


def spell_languages(text: str) -> str:
    for rx, sub in LANG_NAMES:
        text = rx.sub(sub, text)
    return text


def title_words(title: str) -> list[str]:
    return WORD.findall(spell_languages((title or "").lower()))


def _phrase(text: str) -> tuple[tuple[str, bool], ...] | None:
    words = []
    for w in text.split():
        star = w.endswith("*")
        ws = title_words(w)
        if not ws:
            continue
        words += [(x, False) for x in ws[:-1]] + [(ws[-1], star)]
    return tuple(words) or None


def _has(words: list[str], phrase: tuple[tuple[str, bool], ...]) -> bool:
    n = len(phrase)
    for i in range(len(words) - n + 1):
        if all(words[i + k] == w or (star and words[i + k].startswith(w)) for k, (w, star) in enumerate(phrase)):
            return True
    return False


class TitleRules:
    """A role's title keywords, compiled. Lines that hold no words are
    ignored."""

    def __init__(self, lines: Iterable[str]) -> None:
        self.rules: list[tuple[bool, list[list[tuple]]]] = []
        self.line_of: list[int] = []    # each rule's index among the given lines
        for i, line in enumerate(lines):
            line = spell_languages((line or "").strip())
            drop = line.startswith("-")
            parts = [[p for p in (_phrase(alt) for alt in part.split("/")) if p]
                     for part in (line[1:] if drop else line).split("+")]
            parts = [p for p in parts if p]
            if parts:
                self.rules.append((not drop, parts))
                self.line_of.append(i)

    def decide(self, title: str) -> tuple[int | None, bool]:
        """The index of the line that decides the title, and whether it keeps
        it; (None, False) when no line fits."""
        words = title_words(title)
        for (keep, parts), i in zip(self.rules, self.line_of):
            if all(any(_has(words, ph) for ph in alts) for alts in parts):
                return i, keep
        return None, False

    def matches(self, title: str) -> bool:
        return self.decide(title)[1]


# Suggestions for the role editor: words a role's title keywords could add.
# Only a title's role part counts ("Platform Engineer" in "Platform Engineer -
# Payments, NYC (Hybrid)"): what follows a dash, comma, bracket or bar is a
# team, a place or a level.
ROLE_PART = re.compile(r"\s+[-–—|]\s+|[,(\[|:]")
NOT_SUGGESTED = {
    "and", "or", "of", "the", "for", "to", "in", "with", "on", "at", "a", "an", "new", "grad", "graduate", "graduates",
    "early", "career", "careers", "entry", "level", "junior", "jr", "intern", "internship", "interns", "co", "op",
    "summer", "fall", "spring", "winter", "remote", "hybrid", "onsite", "part", "full", "time", "us", "usa", "i", "ii",
    "iii", "iv", "team", "program", "year", "years", "based", "university", "college", "student", "students",
}


def _role_words(title: str) -> list[str]:
    return title_words(ROLE_PART.split(title, maxsplit=1)[0])


def _candidates(title: str, skip: set[str] = frozenset()) -> set[str]:
    """Words and two-word phrases of a title's role part that could be keywords:
    no level codes ("P19", "L3"), and a phrase only of words written next to
    each other ("Datacenter & Agentic" is none)."""
    out, prev = set(), None
    for tok in ROLE_PART.split(title, maxsplit=1)[0].split():
        ws = title_words(tok)
        w = ws[0] if len(ws) == 1 else None
        if w and (len(w) < 2 or any(c.isdigit() for c in w) or w in NOT_SUGGESTED or w in skip):
            w = None
        if w:
            out.add(w)
            if prev:
                out.add(f"{prev} {w}")
        prev = w
    return out


def _with_word(lines: list[str], line: int, group: int, word: str) -> list[str]:
    """`lines` with `word` as one more alternative in group `group` of line `line`."""
    out = list(lines)
    text = out[line].strip()
    drop = text.startswith("-")
    parts = [p.strip() for p in (text[1:] if drop else text).split("+")]
    parts[group] += "/" + word
    out[line] = ("- " if drop else "") + " + ".join(parts)
    return out


def _best(found: list[dict], limit: int) -> list[dict]:
    """The strongest suggestions, one per set of titles they change: of a word
    and a phrase that change the same titles ("site", "reliability", "site
    reliability"), the phrase, as it says what it's for."""
    by_set: dict[tuple, dict] = {}
    for s in found:
        key = s.pop("_set")
        have = by_set.get(key)
        if not have or len(s["word"].split()) > len(have["word"].split()):
            by_set[key] = s
    out = sorted(by_set.values(), key=lambda s: (s.get("lost", 0), -s["count"], s["word"]))
    seen, best = set(), []
    for s in out:
        if s["word"] not in seen:
            seen.add(s["word"])
            best.append(s)
    return best[:limit]


def _examples(titles) -> list[str]:
    out = []
    for t in titles:
        if t not in out:
            out.append(t)
        if len(out) == 3:
            break
    return out


def keep_suggestions(lines: list[str], misses: list[tuple[str, int]], seniority: Iterable[str],
                     limit: int = 8) -> list[dict]:
    """Words that would let a role's keep lines find titles they miss now:
    {"line", "group", "word", "count", "examples"}, most postings first.
    `misses` is (title, postings) for recent titles no line keeps. A title
    counts for a word (or two-word phrase) when it's in its role part, its
    line's other groups all fit it, and the line with the word added would
    keep it (a leave-out line above still wins)."""
    rules = TitleRules(lines)
    wanted = set(seniority)
    titles = [(t, n) for t, n in misses if title_levels(t) & wanted and not rules.matches(t)]
    found = []
    for (keep, parts), line in zip(rules.rules, rules.line_of):
        if not keep:
            continue
        # A word the line already has in a group fits that group, not the missing one:
        # "analyst" for "credit/risk + analyst" would keep any analyst at all.
        used = {w for alts in parts for ph in alts for w, _ in ph}
        cands: dict[tuple[int, str], list[tuple[str, int]]] = {}
        for t, n in titles:
            words = title_words(t)
            failing = [g for g, alts in enumerate(parts) if not any(_has(words, ph) for ph in alts)]
            if len(failing) != 1:
                continue
            for w in _candidates(t, used):
                cands.setdefault((failing[0], w), []).append((t, n))
        # The most common first; each is tried with the real matcher, so only so many.
        top = sorted(cands.items(), key=lambda kv: -sum(n for _, n in kv[1]))[:80]
        for (group, word), hits in top:
            if sum(n for _, n in hits) < 2:
                continue
            trial = TitleRules(_with_word(lines, line, group, word))
            kept = sorted(((t, n) for t, n in hits if trial.decide(t) == (line, True)), key=lambda x: -x[1])
            count = sum(n for _, n in kept)
            if count >= 2:
                found.append({"line": line + 1, "group": group + 1, "word": word, "count": count,
                              "examples": _examples(t for t, _ in kept), "_set": (line, tuple(sorted(t for t, _ in kept)))})
    return _best(found, limit)


def drop_suggestions(lines: list[str], unwanted: list[str], others: list[str], limit: int = 8) -> list[dict]:
    """Words that would leave out titles the person passed on (dismissed,
    or a low match against their resume) and little else the rules keep:
    {"word", "count", "lost", "examples", "line"}, count being the unwanted
    titles the rules keep now that would go, lost the others that would go
    with them (ones gone for and ones in between). Only words mostly in
    unwanted ones are suggested: "development" in "Software Development
    Engineer" would cost too much. The word goes into the first leave-out
    line of one group (in one with a "+", it would leave out only titles
    with the rest too), or a new one at the top ("line": None)."""
    rules = TitleRules(lines)
    bad = [t for t in unwanted if rules.matches(t)]
    good = [t for t in others if rules.matches(t)]
    if not bad:
        return []
    first_drop = next((i for (keep, parts), i in zip(rules.rules, rules.line_of) if not keep and len(parts) == 1), None)
    used = {w for keep, parts in rules.rules if keep for alts in parts for ph in alts for w, _ in ph}
    counts: dict[str, int] = {}
    for t in bad:
        for w in _candidates(t, used):
            counts[w] = counts.get(w, 0) + 1
    # A leave-out word can only cost titles that have it, so only those are tried.
    bad_words = [title_words(t) for t in bad]
    good_words = [title_words(t) for t in good]
    found = []
    for word, n in sorted(counts.items(), key=lambda kv: -kv[1])[:80]:
        if n < 2:
            continue
        phrase = _phrase(word)
        trial_lines = _with_word(lines, first_drop, 0, word) if first_drop is not None else ["- " + word, *lines]
        trial = TitleRules(trial_lines)
        gone = [t for t, ws in zip(bad, bad_words) if _has(ws, phrase) and not trial.matches(t)]
        lost = sum(1 for t, ws in zip(good, good_words) if _has(ws, phrase) and not trial.matches(t))
        if len(gone) >= 2 and lost * 3 < len(gone):
            found.append({"word": word, "count": len(gone), "lost": lost, "examples": _examples(gone),
                          "line": first_drop + 1 if first_drop is not None else None, "_set": tuple(sorted(set(gone)))})
    return _best(found, limit)


def keyword_warnings(lines: list[str]) -> list[str]:
    """Plain-language notes on title keyword lines that won't do what they
    look like they do. Line numbers count from 1, blank lines included."""
    out = []
    rules = TitleRules(lines)
    used = set(rules.line_of)
    last_keep = max((i for (keep, _), i in zip(rules.rules, rules.line_of) if keep), default=None)
    for i, line in enumerate(lines):
        line = (line or "").strip()
        if not line:
            continue
        n = f"Line {i + 1} (“{line if len(line) <= 40 else line[:38] + '…'}”)"
        body = spell_languages(line[1:] if line.startswith("-") else line)
        if i not in used:
            out.append(f"{n} has no words in it, so it does nothing.")
        elif "+" in body and any(not p.strip() or p.strip()[0] == "/" or p.strip()[-1] == "/" for p in body.split("+")):
            out.append(f"{n}: “+” means “and also”, but one side of a “+” here is empty, "
                       "so the line may not find what you expect.")
        elif line.startswith("-") and last_keep is not None and i > last_keep:
            out.append(f"{n} leaves titles out, but no line below it looks for any, so it changes nothing. "
                       "Move it above the lines it should win over.")
    if rules.rules and last_keep is None:
        out.append("Every line starts with “-”, so this role finds nothing. Add a line for the titles you want.")
    return out


ROLE_WORD = "engineer/engineering/developer/sde/swe/programmer"

# The radar's original software filter, written as title keywords: a
# software word (or, failing that, a broader tech word without a non-software
# word) next to an engineer word. The first role of a new install uses it, so
# it keeps exactly what the radar kept before roles existed.
SOFTWARE_KEYWORDS = [
    "- sales engineer*/solution engineer*/solutions engineer*/support engineer*/test engineer*/qa engineer*/marketing/recruit*",
    "software/sde/swe/developer/backend/back end/frontend/front end/fullstack/full stack + " + ROLE_WORD,
    "- sales/solution/solutions/support/customer/field/hardware/mechanical/electrical/manufacturing/technician/"
    "recruit*/account/marketing/designer/analyst/quality assurance/qa/validation/test engineer/network engineer/"
    "process/civil/chemical/rf/optical/facilities/propulsion/avionics/structures/scientist/mission/integration/"
    "deployment strategist/softgoods/textile/industrial design/product design/mechanical design",
    "platform/infrastructure/mobile/ios/android/devops/site reliability/sre/machine learning/ml/ai/data/founding/"
    "forward deployed/distributed systems/cloud/security/web/application/compiler/embedded software + " + ROLE_WORD,
]

# Finance: a finance word next to a role word (analyst, associate,
# accountant), or a finance title by itself; not sales, service, clerical,
# loan-officer or software titles, which share the words.
FINANCE_KEYWORDS = [
    "- sales/recruit*/marketing/customer service/call center/collections/teller/insurance agent/loan officer/"
    "mortgage/data entry/clerk/receptionist/administrative assistant/software/developer/engineer/engineering/"
    "it support/help desk",
    "financial/finance/fp&a/investment/investment banking/equity research/credit/risk/treasury/valuation/m&a/"
    "mergers/private equity/venture capital/portfolio/asset management/wealth management/capital markets/"
    "corporate development/fixed income/trading/quantitative/quant/underwriting/audit/tax/accounting/fund/"
    "hedge fund/restructuring/leveraged finance/real estate finance/pricing/actuarial + "
    "analyst/associate/specialist/accountant/researcher/trader/strategist/underwriter/auditor/consultant/"
    "rotational program/development program/leadership program",
    "accountant/auditor/underwriter/actuary/quantitative researcher/quantitative trader/quant researcher/quant trader",
]

NOT_TECH = "- sales/account/marketing/recruit*/customer/support/technician/mechanical/electrical/civil/chemical/" \
           "hardware/manufacturing/facilities/clerk/data center/solution/solutions/field"

# Starting points offered in the "Add role" form. Picking one only fills in
# the new role's title keywords; the user edits them from there.
ROLE_TEMPLATES: dict[str, tuple[str, list[str]]] = {
    "blank": ("Blank", []),
    "software": ("Software engineering", SOFTWARE_KEYWORDS),
    "fullstack": ("Full-stack", [NOT_TECH, "full stack/fullstack/frontend/front end/backend/back end + "
                                 + ROLE_WORD]),
    "data": ("Data engineering and analytics", [
        NOT_TECH + "/controller/clinical/accounting/entry operator",
        "data/analytics/etl/elt/bi/business intelligence/big data/data warehouse/database/dbt + "
        "engineer/engineering/developer/analyst/scientist/architect/modeler"]),
    "ml": ("Machine learning and AI", [
        NOT_TECH,
        "machine learning/ml/ai/artificial intelligence/deep learning/computer vision/nlp/llm/llms/generative/genai/"
        "applied scientist/research/perception/mlops/data scientist/recommendation/ranking + "
        "engineer/engineering/scientist/researcher/developer/sde/swe"]),
    "mobile": ("Mobile (iOS, Android)", [
        NOT_TECH, "mobile/ios/android/react native/flutter/swift/kotlin + " + ROLE_WORD]),
    "infra": ("SRE, DevOps and infrastructure", [
        NOT_TECH,
        "site reliability/sre/devops/dev ops/devsecops/infrastructure/infra/platform/cloud/reliability/production/"
        "systems/kubernetes/observability/build/release/storage/compute + "
        "engineer/engineering/developer/sre/administrator/architect"]),
    "finance": ("Finance (analyst, accounting, risk)", FINANCE_KEYWORDS),
}

_SOFTWARE = TitleRules(SOFTWARE_KEYWORDS)


def is_swe_title(title: str) -> bool:
    return _SOFTWARE.matches(title)


# --------------------------------------------------------------------------
# Seniority

# "manager" marks a management role; "management" usually names a domain
# ("Secrets Management", "Identity and Access Management"), so it isn't here.
NOT_ENTRY = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|[afes]?vp|"
    r"vice[- ]president|architect|distinguished|fellow|chief|ii|iii|iv|v|"
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
ENTRY_PHRASE = re.compile(r"new grad|entry|junior|early career|university", re.I)
# Hiring programs that stay open for a season ("Software Engineer, New Grad
# 2027", "Entry Level - University Graduate"): the dashboard shows them longer.
PROGRAM = re.compile(
    r"\b(20[2-3][0-9]|new[- ]?grad\w*|new college|(?:university|college|recent) grad\w*|"
    r"early[- ]career|entry[- ]level|campus|class of)\b", re.I)
# What makes an intern title a new-grad one as well.
INTERN_AND_ENTRY = re.compile(r"\b(new[- ]?grad\w*|new college|(?:university|college|recent) grad\w*|"
                              r"entry[- ]level|early[- ]career|junior|full[- ]time)\b", re.I)
# The words NOT_ENTRY rejects, so entry-level filtering stays as it was.
INTERN = re.compile(r"\b(intern|internship|internships|co-?op|apprentice)\b", re.I)
SENIOR = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|[afes]?vp|vice[- ]president|"
    r"architect|distinguished|fellow|chief|iii|iv|l[5-9]|e[5-9]|ic[4-9]|level [3-9]|tech lead|"
    r"expert|smts|lmts|pmts|mts [3-6])\b", re.I)
NOT_EMPLOYEE = re.compile(r"\b(contract(or)?|part[- ]time)\b", re.I)


# "Member of Technical Staff" is a flat title at many labs, held from new
# grad up; its "staff" is not the Staff level. It reads as "MTS", so only a
# level beside it ("Senior MTS", "MTS 3") counts.
TECH_STAFF = re.compile(r"\b(?:member\s+of\s+(?:the\s+)?)?technical\s+staff\b", re.I)


def is_entry_title(title: str) -> bool:
    title = TECH_STAFF.sub("MTS", title)
    if ENTRY_HINT.search(title) and ENTRY_PHRASE.search(title):
        return True
    return not NOT_ENTRY.search(title)


def title_levels(title: str) -> set[str]:
    """Levels a title can be, before the description is read. A title with
    no level words ("Software Engineer") could be entry or mid."""
    title = TECH_STAFF.sub("MTS", title)
    if NOT_EMPLOYEE.search(title):
        return set()
    levels = set()
    if INTERN.search(title):
        levels.add("intern")
        # Entry too only for a combined posting ("New Grad / Intern"): "Intern
        # Opportunities for University Students" is an internship.
        if not INTERN_AND_ENTRY.search(title):
            return levels
    if is_entry_title(title):
        levels.add("entry")
    if SENIOR.search(title):
        levels.add("senior")
    elif not (ENTRY_HINT.search(title) and ENTRY_PHRASE.search(title)) and not INTERN.search(title) \
            and not re.search(r"\b(phd|postdoc)\b", title, re.I):
        levels.add("mid")
    return levels


def levels(title: str, min_yoe: int | None, entry_hint: bool) -> set[str]:
    """title_levels, narrowed by the years of experience the posting asks."""
    out = title_levels(title)
    if min_yoe is not None:
        if min_yoe >= 3 and not entry_hint:
            out.discard("entry")
        if min_yoe >= 5 and "mid" in out:
            out.add("senior")
        if min_yoe >= 7:
            out.discard("mid")
        if min_yoe >= 2:
            out.discard("intern")
    return out


# --------------------------------------------------------------------------
# Location (the US rules are the radar's original filter)

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
HYBRID = re.compile(r"\bhybrid\b", re.I)

US_NAMES = {"us", "usa", "united states", "united states of america"}


def us_location(location: str, country: str | None) -> bool:
    if country:
        c = country.strip().lower()
        if c in US_NAMES:
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


# Other countries: (name, names and big cities that appear in location
# strings). The country field some sources send is matched against the code
# and the names.
COUNTRIES: dict[str, tuple[str, str]] = {
    "US": ("United States", ""),        # us_location() above
    "CA": ("Canada", r"canada|toronto|vancouver|montreal|ontario|quebec|ottawa|calgary|edmonton|waterloo|"
                     r"british columbia|alberta|mississauga|kitchener|halifax|winnipeg"),
    "GB": ("United Kingdom", r"uk|u\.k\.|united kingdom|great britain|england|scotland|wales|northern ireland|"
                             r"london|manchester|edinburgh|glasgow|bristol|cambridge, uk|oxford|leeds|birmingham, uk|belfast"),
    "IE": ("Ireland", r"ireland|dublin|cork|galway|limerick"),
    "DE": ("Germany", r"germany|deutschland|berlin|munich|münchen|hamburg|frankfurt|cologne|köln|stuttgart|dusseldorf|düsseldorf"),
    "FR": ("France", r"france|paris|lyon|toulouse|nice|grenoble"),
    "NL": ("Netherlands", r"netherlands|holland|amsterdam|rotterdam|utrecht|eindhoven|the hague"),
    "ES": ("Spain", r"spain|españa|madrid|barcelona|valencia|malaga|málaga"),
    "PT": ("Portugal", r"portugal|lisbon|lisboa|porto"),
    "IT": ("Italy", r"italy|italia|milan|milano|rome|roma|turin|torino"),
    "CH": ("Switzerland", r"switzerland|zurich|zürich|geneva|lausanne|basel|bern"),
    "SE": ("Sweden", r"sweden|stockholm|gothenburg|malmö|malmo"),
    "DK": ("Denmark", r"denmark|copenhagen|aarhus"),
    "NO": ("Norway", r"norway|oslo"),
    "FI": ("Finland", r"finland|helsinki|espoo|tampere"),
    "PL": ("Poland", r"poland|warsaw|krakow|kraków|wroclaw|wrocław|gdansk|gdańsk"),
    "IL": ("Israel", r"israel|tel aviv|haifa|jerusalem|herzliya"),
    "IN": ("India", r"india|bangalore|bengaluru|hyderabad|pune|chennai|mumbai|delhi|new delhi|gurgaon|gurugram|"
                    r"noida|kolkata|ahmedabad"),
    "SG": ("Singapore", r"singapore"),
    "JP": ("Japan", r"japan|tokyo|osaka|kyoto"),
    "KR": ("South Korea", r"korea|seoul"),
    "CN": ("China", r"china|beijing|shanghai|shenzhen|hangzhou|guangzhou"),
    "HK": ("Hong Kong", r"hong kong"),
    "TW": ("Taiwan", r"taiwan|taipei|hsinchu"),
    "AU": ("Australia", r"australia|sydney|melbourne|brisbane|perth|adelaide|canberra"),
    "NZ": ("New Zealand", r"new zealand|auckland|wellington"),
    "BR": ("Brazil", r"brazil|brasil|são paulo|sao paulo|rio de janeiro|belo horizonte"),
    "MX": ("Mexico", r"mexico|méxico|mexico city|guadalajara|monterrey"),
    "AR": ("Argentina", r"argentina|buenos aires"),
    "CO": ("Colombia", r"colombia|bogota|bogotá|medellin|medellín"),
    "AE": ("United Arab Emirates", r"uae|united arab emirates|dubai|abu dhabi"),
}
COUNTRY_RX = {code: re.compile(rf"\b(?:{rx})\b", re.I) for code, (_, rx) in COUNTRIES.items() if rx}
COUNTRY_NAMES = {code: {code.lower(), name.lower()} for code, (name, _) in COUNTRIES.items()}
COUNTRY_NAMES["US"] |= US_NAMES
COUNTRY_NAMES["GB"] |= {"uk", "great britain", "england"}


def country_code(name: str | None) -> str | None:
    c = (name or "").strip().lower()
    if not c:
        return None
    return next((code for code, names in COUNTRY_NAMES.items() if c in names), "??")


def in_country(code: str, location: str, country: str | None) -> bool:
    """The US rule generalized: the source's country field decides when there
    is one (unless the location says remote), else the location names the
    country, else a bare "Remote" naming no country counts."""
    if code == "US":
        return us_location(location, country)
    rx = COUNTRY_RX.get(code)
    if not rx:
        return False
    given = country_code(country)
    if given:
        if given == code:
            return True
        if not REMOTE.search(location):
            return False
    if rx.search(location):
        return True
    if US_MARKER.search(location) or NON_US.search(location) or any(
            r.search(location) for r in COUNTRY_RX.values()):
        return False
    return bool(REMOTE.search(location))


def workplace(location: str, remote: bool) -> set[str]:
    """On-site, hybrid and/or remote, from the location text. "Remote / New
    York, NY" is both remote and on-site."""
    kinds = set()
    if remote or REMOTE.search(location):
        kinds.add("remote")
    if HYBRID.search(location):
        kinds.add("hybrid")
    rest = [p for p in re.split(r"\s*(?:/|;|\||\bor\b)\s*", location)
            if p.strip() and not REMOTE.search(p) and not HYBRID.search(p)]
    if rest or not kinds:
        kinds.add("onsite")
    return kinds


# --------------------------------------------------------------------------
# Pay

# The number can't be cut short ("$121B" is not "$12"), and an amount
# followed by million/billion/trillion is company news, not pay.
_AMOUNT = (r"(\d{1,3}(?:[,.]\d{3})+(?:\.\d{2})?|\d+(?:\.\d+)?)(?![.,]?\d)\s?([kK])?"
           r"(?!\+?\s*(?:million|billion|trillion|mm|bn|[mbt])\b)")
_CUR = r"(?:US\$|C\$|CA\$|A\$|\$|USD\s?|CAD\s?|£|€|GBP\s?|EUR\s?)"
PAY = re.compile(
    _CUR + r"\s?" + _AMOUNT
    + r"(?:\s*(?:-|–|—|to|and)\s*" + _CUR + r"?\s?" + _AMOUNT + r")?"
    + r"(\s*(?:/|per|an|a)?\s*(?:hour|hr|hourly)\b)?",
    re.I)
# Amounts that are perks or company figures, not pay: "a $15,000 signing
# bonus", "$2,000 learning budget", "an additional $20,000", "raised $50M".
# "$150k-$190k plus bonus" is still pay.
PERK_AFTER = re.compile(r"\s*(?:usd\s+)?(?:annual\s+|one[- ]time\s+|yearly\s+)?(?:signing\s+|sign[- ]on\s+|relocation\s+|"
                        r"referral\s+|learning\s+|education\s+|wellness\s+|home office\s+)?"
                        r"(?:bonus|stipend|budget|allowance|credit|grant|reimbursement|per (?:month|week))\b", re.I)
PERK_BEFORE = re.compile(r"(?:bonus|stipend|budget|allowance|additional|differential|reimburs\w*|401\(?k|referral|"
                         r"raised|funding|valuation|revenue|grant|donat\w*|match(?:es|ing)? up to|up to)\W+(?:\w+\W+){0,3}$", re.I)
HOURS_PER_YEAR = 2080


def _amount(num: str, k: str | None) -> float:
    if re.fullmatch(r"\d{1,3}(?:[,.]\d{3})+(?:\.\d{2})?", num):
        num = re.sub(r"[,.](?=\d{3}\b)", "", num)
    try:
        v = float(num.replace(",", ""))
    except ValueError:
        return 0.0
    return v * 1000 if k else v


def pay_range(text: str | None) -> tuple[float, float] | None:
    """(low, high) yearly pay the posting states, or None. Hourly rates are
    annualized at 2080 hours; amounts that can't be yearly pay (funding
    rounds, bonuses, budgets) are skipped."""
    text = text or ""
    lo = hi = None
    for m in PAY.finditer(text):
        if PERK_BEFORE.search(text[max(0, m.start() - 60):m.start()]) or PERK_AFTER.match(text, m.end()):
            continue
        a = _amount(m.group(1), m.group(2))
        b = _amount(m.group(3), m.group(4) or m.group(2)) if m.group(3) else a
        if m.group(5):          # hourly
            if not (7 <= a <= 500 and 7 <= b <= 500):
                continue
            a, b = a * HOURS_PER_YEAR, b * HOURS_PER_YEAR
        if b < a:
            a, b = b, a
        if not (15_000 <= a and b <= 2_000_000):
            continue
        lo = a if lo is None else min(lo, a)
        hi = b if hi is None else max(hi, b)
    return (lo, hi) if lo is not None else None


# --------------------------------------------------------------------------
# Roles

def _words_rx(words: Iterable[str]) -> re.Pattern | None:
    ws = [re.escape(w.strip()) for w in words if w and w.strip()]
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(ws) + r")(?![A-Za-z0-9])", re.I) if ws else None


def norm_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def is_program(title: str) -> bool:
    return bool(PROGRAM.search(title or ""))


# Requisition numbers in titles ("Software Engineer (R123456)", "- 2024-18732"):
# a repost usually gets a new one.
REQ_ID = re.compile(r"[(\[]?\b(?:req(?:uisition)?\s*#?\s*)?[A-Z]{0,4}[-_]?\d{5,}(?:[-_]\d+)?\b[)\]]?", re.I)


def posting_identity(company: str, title: str, location: str) -> str:
    """The same role at the same company and place, whatever its posting ID:
    a posting taken down and put up again keeps this."""
    place = re.split(r"[;,|/]", location or "", maxsplit=1)[0]
    return "|".join((norm_company(company), norm_company(REQ_ID.sub(" ", title or "")), norm_company(place)))


@dataclass
class Facts:
    """What a role looks at in one posting."""
    company: str
    title: str
    location: str
    country: str | None
    remote: bool
    description: str
    min_yoe: int | None
    entry_hint: bool
    pay: tuple[float, float] | None = None


@dataclass
class Role:
    id: str
    name: str
    title_keywords: list[str]
    seniority: set[str]
    countries: list[str]
    cities: list[str]
    workplace: set[str]
    salary_min: int = 0
    must_have: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    allow: set[str] = field(default_factory=set)
    block: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._titles = TitleRules(self.title_keywords)
        self._city = _words_rx(self.cities)
        self._must = [_words_rx([w]) for w in self.must_have if w.strip()]
        self._excl = _words_rx(self.exclude)

    @classmethod
    def from_settings(cls, r: dict) -> "Role":
        return cls(id=r["id"], name=r["name"], title_keywords=list(r["title_keywords"]),
                   seniority=set(r["seniority"]), countries=list(r["countries"]), cities=list(r["cities"]),
                   workplace=set(r["workplace"]), salary_min=r["salary_min"], must_have=list(r["must_have"]),
                   exclude=list(r["exclude"]), allow={norm_company(c) for c in r["companies_allow"]},
                   block={norm_company(c) for c in r["companies_block"]})

    # Checks that need only the listing (title, location, company), so board
    # fetchers can skip a posting before paying for its details.
    def title_ok(self, title: str) -> bool:
        return self._titles.matches(title) and bool(title_levels(title) & self.seniority)

    def location_ok(self, location: str, country: str | None, remote: bool = False) -> bool:
        kinds = workplace(location, remote)
        if not kinds & self.workplace:
            return False
        if self.countries and not any(in_country(c, location, country) for c in self.countries):
            return False
        if self._city and not self._city.search(location):
            # A remote posting can be taken from any of the user's cities.
            return "remote" in kinds and "remote" in self.workplace
        return True

    def company_ok(self, company: str) -> bool:
        c = norm_company(company)
        if c in self.block:
            return False
        return not self.allow or c in self.allow

    def matches(self, f: Facts) -> bool:
        if not (self.company_ok(f.company) and self.title_ok(f.title)
                and self.location_ok(f.location, f.country, f.remote)):
            return False
        if not (levels(f.title, f.min_yoe, f.entry_hint) & self.seniority):
            return False
        text = f"{f.title}\n{f.description}"
        if self._excl and self._excl.search(text):
            return False
        if any(not rx.search(text) for rx in self._must):
            return False
        if self.salary_min and f.pay and f.pay[1] < self.salary_min:
            return False
        return True


# The role a new install starts with: the radar's original search.
DEFAULT_ROLE = {
    "id": "software", "name": "Software, entry level, US", "title_keywords": SOFTWARE_KEYWORDS,
    "seniority": ["entry"], "countries": ["US"], "cities": [], "workplace": ["onsite", "hybrid", "remote"],
    "salary_min": 0, "must_have": [], "exclude": [], "companies_allow": [], "companies_block": [],
}


class Search:
    """All of the user's roles together, as radar.py uses them."""

    def __init__(self, roles: list[dict] | None = None, blocked: Iterable[str] = ()) -> None:
        self.roles = [Role.from_settings(r) for r in (roles or [DEFAULT_ROLE])]
        self.blocked = {norm_company(c) for c in blocked}

    def title_stage(self, title: str) -> str | None:
        """Why no role wants a title: "title" when no role's keywords fit it,
        "level" when some do but none of those roles wants its seniority.
        None when a role wants it."""
        fits = [r for r in self.roles if r._titles.matches(title)]
        if not fits:
            return "title"
        levels_ = title_levels(title)
        return None if any(levels_ & r.seniority for r in fits) else "level"

    def level_and_place_fit(self, title: str, location: str | None = None, country: str | None = None,
                            remote: bool = False) -> bool:
        """Whether some role would take a posting's level and place, whatever
        its title: one the title rules leave out that a role could want."""
        levels_ = title_levels(title)
        return any(levels_ & r.seniority and (location is None or r.location_ok(location, country, remote))
                   for r in self.roles)

    def location_wanted(self, location: str, country: str | None = None, remote: bool = False) -> bool:
        return any(r.location_ok(location, country, remote) for r in self.roles)

    def wants_level(self, level: str) -> bool:
        return any(level in r.seniority for r in self.roles)

    def only_country(self) -> str | None:
        """The one country every role is limited to, so sources that can
        search by country (SmartRecruiters, Eightfold, Amazon, iCIMS) ask for
        just that one. None when any role looks wider."""
        sets = {tuple(r.countries) for r in self.roles}
        return sets.pop()[0] if len(sets) == 1 and len(next(iter(sets))) == 1 else None

    def matching(self, f: Facts) -> list[str]:
        if norm_company(f.company) in self.blocked:
            return []
        return [r.id for r in self.roles if r.matches(f)]
