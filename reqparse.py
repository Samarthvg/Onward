"""Turn a job description into the concrete keywords it asks for.

Postings are a sequence of headings and bullets, and a bullet means what its
heading says: "What you'll need" vs "Nice to have" vs "Benefits". Each line
inherits its section, and the skills named in it (from skills.json, plus tech
terms learned from the postings) become checklist keywords weighted by that
section. Lines with no concrete keyword ("strong problem-solving skills") are
dropped: a resume can't confirm or refute them, so they don't affect the score.
"""

from __future__ import annotations

import re

H_SKIP = re.compile(
    r"benefit|perks|compensation|salary|\bpay\b|about (?:us|the company|the team)|who we are|"
    r"(?:our|core|company) values|culture|why (?:join|work|you'll love)|what we(?:'ll| will)? (?:offer|provide)|"
    r"we offer|rewards|equal|eeo|diversity|life at|our mission|locations?|how to apply|interview|"
    r"accommodat|privacy|total rewards|the fine print|additional information", re.I)
H_PREF = re.compile(
    r"preferred|nice[- ]to[- ]have|bonus|\bplus\b|desired|extra credit|stand out|ideally|"
    r"what we value|we value|good to have|great to have|helpful|not required|nice if|would be (?:a )?plus", re.I)
# Checked before the skip list: "What You'll Bring to The Team" is a
# requirements heading even though it mentions the team.
H_REQ_STRONG = re.compile(
    r"requirement|qualification|what you(?:'ll| will)? (?:need|bring)|you (?:have|bring|might be)|"
    r"you(?:'ll| will) bring|"
    r"about you|who you are|what we(?:'re| are) looking for|must[- ]haves?|minimum|basic|"
    r"skills (?:and|&) experience|ideal candidate|you should have|your background|you'll thrive|"
    r"what we require|we require|what we need|you'll need|you will need|must have|what we look for|"
    r"clearance|citizenship", re.I)
H_REQ = re.compile(r"skills|experience|you are|the tech|tech stack|technologies", re.I)
# Benefits and company headings that would otherwise read as "what you'll do"
# ("Why you will love working here") or stay unlabelled ("About Nextdoor").
H_SKIP_STRONG = re.compile(
    r"benefit|perks|compensation|salary|why (?:you|join|work)|what we(?:'ll| will)? (?:offer|provide)|"
    r"we offer|rewards|equal|eeo|our principles|^about (?!you|the role|this role|the job|the position|the opportunity|the work)",
    re.I)
H_RESP = re.compile(
    r"responsibilit|what you(?:'ll| will)? (?:do|work on|own|build)|the role|your role|day[- ]to[- ]day|"
    r"you will|in this role|impact|the job|the work|what you'll be doing|key duties", re.I)
BULLET = re.compile(r"^\s*(?:[-*•·▪◦‣]\s*|[●○■►➢✓]\s*|\d{1,2}[.)]\s+)")
# A requirements line that says it isn't one ("Experience with Braze a plus").
LINE_PREF = re.compile(r"\ba plus\b|\bis a plus\b|\bpreferred\b|\bnice[- ]to[- ]have\b|\bbonus\b|\bhelpful\b|"
                       r"\bnot required\b|\bideally\b", re.I)
# Products a posting names in a list of examples ("software such as Braze,
# Qualtrics, or Rudderstack"): concrete tools even when too rare to be learned
# from other postings. Only after a word for tools, so "a field such as
# Computer Science" isn't one.
TOOL_LIST = re.compile(
    r"\b(?:software|tools?|platforms?|technolog(?:y|ies)|frameworks?|languages?|librar(?:y|ies)|databases?|"
    r"services|applications|products|stacks?|providers?|solutions|suites?)\b[^.;:]{0,15}?"
    r"\b(?:such as|like|including|e\.g\.|for example)[:,]?\s+([^.;:()]+)", re.I)
# A name, with the numbers that belong to it ("Office 365", "SOC 2").
TOOL_NAME = re.compile(r"[A-Z][A-Za-z0-9.+#-]*[A-Za-z0-9+#](?:/[A-Z][A-Za-z0-9.+#-]*)?"
                       r"(?:\s(?:[A-Z][A-Za-z0-9.+#-]*[A-Za-z0-9+#]|\d[\d.-]*)){0,2}")
TOOL_SECTIONS = ("req", "pref")


def _short_of(name: str, skills: set[str]) -> bool:
    """"Node", "Next", "Go" and "Spring" short for a skill the line names
    (Node.js, Next.js, Go, Spring Boot)."""
    n = name.lower()
    return any(s.lower() == n or s.lower().startswith(n + ".") or s.lower().startswith(n + " ") for s in skills)


def named_tools(text: str, find_skills, stop=frozenset()) -> set[str]:
    """Capitalized names in a posting's list of example tools that aren't
    known skills already (those are found by find_skills)."""
    out = set()
    known = set(find_skills(text))
    for m in TOOL_LIST.finditer(text):
        for item in re.split(r",\s*|\s+(?:and/or|or|and)\s+", m.group(1)):
            hit = TOOL_NAME.match(re.sub(r"^(?:or|and)\s+", "", item.strip()))
            if not hit:
                continue
            name = hit.group(0).rstrip(".")
            if (name in stop or name.split()[0] in stop or re.search(r"-[a-z]", name) or len(name) < 2
                    or find_skills(name) or _short_of(name, known)):
                continue
            out.add(name)
    return out
# Text outside any recognized section only counts when it reads like a requirement.
CUE = re.compile(
    r"\b(experience|experienced|proficien|familiar|knowledge|understanding|ability to|able to|skilled|"
    r"expertise|background in|degree|fluen|comfortable|hands-on|track record|strong|solid|"
    r"working with|built|building|you have|you've|years|shipped|developing|designing)\w*", re.I)
NOISE = re.compile(
    r"\b(we offer|benefit|salary|equal opportunity|our mission|founded|valuation|investors|"
    r"accommodation|e-verify|privacy|background check|health insurance|paid time off|401\(?k|"
    r"retirement|wellness|parental leave|stipend|offsite|in accordance with|reserves? the right|"
    r"physical requirements?|volunteer|pay (?:range|transparency)|applicable law|drug[- ]free)\b", re.I)

# Required keywords count fully; nice-to-haves and "what you'll do" half; text
# outside any labelled section in between. "stack" is an intro line that names
# the team's tools ("built on AWS: EC2, RDS, EKS") without asking for them.
WEIGHT = {"req": 1.0, "pref": 0.5, "resp": 0.5, "unknown": 0.6, "stack": 0.4}
KIND = {"req": "req", "pref": "pref", "resp": "pref", "unknown": "req", "stack": "pref"}

DEGREE = re.compile(
    r"\b(ph\.?\s?d|doctorate|master'?s?|m\.?\s?s\.?(?=\s|,|$)|bachelor'?s?|b\.?\s?s\.?(?=\s|,|$)|b\.?\s?a\.?(?=\s|,|$)|"
    r"undergraduate|associate'?s? degree)\b", re.I)
EQUIVALENT = re.compile(r"or equivalent|equivalent (?:practical |work )?experience|or related experience", re.I)


def _norm(text: str) -> str:
    return (text.replace("’", "'").replace("‘", "'").replace("“", '"')
            .replace("”", '"').replace("–", "-").replace("—", "-"))


def _is_heading(line: str) -> bool:
    if BULLET.match(line) or len(line) > 80:
        return False
    if line.endswith(":") or (line.endswith("?") and len(line.split()) <= 12):
        return True
    return not re.search(r"[.!]$", line) and len(line.split()) <= 8


def _classify(heading: str) -> str | None:
    h = heading.lower()
    if H_PREF.search(h):
        return "pref"
    if H_REQ_STRONG.search(h):
        return "req"
    if H_SKIP_STRONG.search(h):
        return "skip"
    if H_RESP.search(h):
        return "resp"
    if H_SKIP.search(h):
        return "skip"
    if H_REQ.search(h):
        return "req"
    return None


def sections(description: str) -> list[tuple[int, str]]:
    """(offset, section) where each section starts, for every heading: what
    a sentence at some offset of the description is under."""
    out, pos = [(0, "unknown")], 0
    text = _norm(description or "")         # same length: one character for one
    for raw in text.split("\n"):
        line = raw.strip()
        if line and _is_heading(line):
            cls = _classify(line)
            if cls:
                out.append((pos, cls))
        pos += len(raw) + 1
    return out


def section_at(starts: list[tuple[int, str]], offset: int) -> str:
    sec = "unknown"
    for at, cls in starts:
        if at > offset:
            break
        sec = cls
    return sec


def units(description: str, find_skills=None) -> list[tuple[str, str]]:
    """(line, section) for every line that could state a requirement.

    With find_skills, unlabelled lines that don't read like a requirement are
    still kept as "stack" when they name 2+ concrete skills."""
    section = "unknown"
    out: list[tuple[str, str]] = []
    for raw in _norm(description or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _is_heading(line):
            cls = _classify(line)
            if cls:
                section = cls
            continue
        if section == "skip":
            continue
        body = BULLET.sub("", line)
        parts = [body] if BULLET.match(line) else re.split(r"(?<=[.!?])\s+(?=[A-Z])", body)
        for part in parts:
            part = part.strip(" ;")
            if len(part) < 8 or NOISE.search(part):
                continue
            if section == "unknown" and not CUE.search(part):
                if find_skills is not None and len(find_skills(part)) >= 2:
                    out.append((part, "stack"))
                continue
            out.append((part, "pref" if section in ("req", "unknown") and LINE_PREF.search(part) else section))
    return out


def keywords(description: str, title: str, find_skills, term_rx: dict[str, re.Pattern],
             stop=frozenset()) -> list[list]:
    """[[keyword, weight, kind], ...] where kind is "title", "req" or "pref".

    find_skills maps text to taxonomy skill names; term_rx holds learned tech
    terms; tools the posting lists as examples count too (named_tools, with
    `stop` the words that are never one). A keyword named in several places
    keeps its strongest weight."""
    best: dict[str, tuple[float, str]] = {}

    def add(name: str, w: float, kind: str) -> None:
        if name not in best or w > best[name][0]:
            best[name] = (w, kind)

    for name in find_skills(title):
        add(name, 1.5, "title")
    lower = {w.rstrip(".-") for w in re.findall(r"(?<![A-Za-z])[a-z][a-z0-9.+#-]*", _norm(description or ""))}
    for text, section in units(description, find_skills):
        found = set(find_skills(text))
        found |= {t for t, rx in term_rx.items() if rx.search(text)}
        if section in TOOL_SECTIONS:
            # A word the posting also writes in lowercase ("Dashboards" /
            # "dashboards") is an ordinary word, not a product.
            found |= {n for n in named_tools(text, find_skills, stop) if " " in n or n.lower() not in lower}
        for name in found:
            add(name, WEIGHT[section], KIND[section])
    return [[n, w, k] for n, (w, k) in sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))]


def degree(description: str) -> dict | None:
    """Lowest degree a posting asks for, from its requirement lines."""
    levels = {"associate": 0, "bachelor": 1, "master": 2, "phd": 3}
    found, equivalent = None, False
    for text, section in units(description):
        if section == "pref":
            continue
        for m in DEGREE.finditer(text):
            w = m.group(1).lower().replace(".", "").replace(" ", "")
            lvl = ("phd" if w.startswith(("ph", "doc")) else "master" if w.startswith(("master", "ms"))
                   else "associate" if w.startswith("associate") else "bachelor")
            if found is None or levels[lvl] < levels[found]:
                found = lvl
            equivalent = equivalent or bool(EQUIVALENT.search(text))
    return {"level": found, "or_equivalent": equivalent} if found else None
