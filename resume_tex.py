"""Parse a LaTeX resume into editable pieces, apply tailoring edits back into
the original source, validate them, and compile.

The source is never regenerated: every edit replaces an exact character span,
so preamble, macros and formatting survive untouched.

Model of a resume:
  entry   = one itemize block plus the header text above it (a job or project)
  bullet  = one \\item inside an entry
  tech    = the "| \\textit{...}" tool list in an entry header, if present
  skills  = rows of a section whose title mentions "skill"

Hidden tags (LaTeX comments, so they never print):
  % skills: React, Redux     inside an entry: skills you really used there
  % inventory: Kafka, Go     anywhere: extra skills allowed in the skills section
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import skillset
import texengine
import voice

ROOT = Path(__file__).resolve().parent
_TAXONOMY = skillset.TAXONOMY
SKILLS = skillset.SKILLS
SKILL_NAMES = {name.lower(): name for name, _ in SKILLS}
IMPLIES = {s["n"]: set(s.get("implies", [])) for s in _TAXONOMY}
# Skills that name a field of work (DevOps, Frontend, Machine Learning), not
# a language or practice everyone uses.
AREAS = {s["n"] for s in _TAXONOMY if s.get("area")}


find_skills = skillset.find


def with_implied(skills: set[str]) -> set[str]:
    """Concrete tools imply what using them involves, all the way along
    (Helm -> Kubernetes -> Docker -> DevOps, React -> Frontend). The
    dashboard's detectSkills follows the same chains."""
    out = set(skills)
    todo = list(skills)
    while todo:
        for to in IMPLIES.get(todo.pop(), ()):
            if to not in out:
                out.add(to)
                todo.append(to)
    if {"Frontend", "Backend"} <= out:
        out.add("Full Stack")
    return out


def strip_comments(tex: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", tex)


def _strip_env_args(s: str) -> str:
    """Remove \\begin{env}{arg}[opt]{arg}... and \\end{env}, however deeply the
    argument braces nest (tabular column specs like {@{}l@{\\extracolsep{\\fill}}r@{}})."""
    out, i = [], 0
    for m in re.finditer(r"\\(?:begin|end)\{[^}]*\}", s):
        if m.start() < i:
            continue
        out.append(s[i:m.start()] + " ")
        j = m.end()
        if s.startswith("\\begin", m.start()):
            while True:
                k = j
                while k < len(s) and s[k] in " \t":
                    k += 1
                if k < len(s) and s[k] == "[":
                    close = s.find("]", k)
                    j = close + 1 if close >= 0 else len(s)
                elif k < len(s) and s[k] == "{":
                    try:
                        j = _brace_end(s, k)
                    except ValueError:
                        j = len(s)
                else:
                    break
        i = j
    out.append(s[i:])
    return "".join(out)


def plain(tex: str) -> str:
    """Rough LaTeX -> text, good enough for skill detection and prompts."""
    s = _strip_env_args(strip_comments(tex))
    s = re.sub(r"\\(?:vspace|hspace)\*?\{[^}]*\}", " ", s)
    s = re.sub(r"\\href\{[^}]*\}\{", "{", s)
    s = s.replace("\\\\", " ").replace(" & ", " ")
    s = re.sub(r"\\(?:textbf|textit|emph|underline|small|large|Large|textsc)\b", "", s)
    s = re.sub(r"\\textasciitilde\{\}|\\textasciitilde", "~", s)
    s = re.sub(r"\\([&%$#_])", r"\1", s)
    s = re.sub(r"\\[a-zA-Z@]+\*?(?:\[[^\]]*\])?", " ", s)
    s = re.sub(r"[{}]", "", s)
    s = re.sub(r"(?<!\S)@\S*", " ", s)  # leftover tabular column specs like @l@
    return re.sub(r"\s+", " ", s).strip()


def words(text: str) -> set[str]:
    """Lowercased word-like tokens, tool names with dots and symbols intact
    ("node.js", "c++"). A sentence's full stop is not part of its last word,
    so a term that ends a sentence ("... on Kafka.") still counts."""
    return {w.lower().rstrip(".") for w in re.findall(r"[A-Za-z0-9.+#-]+", text)} - {""}


def tag_skills(text: str, tag: str) -> set[str]:
    out: set[str] = set()
    for m in re.finditer(rf"(?<!\\)%\s*{tag}\s*:\s*(.+)", text, re.I):
        for raw in re.split(r"[,;]", m.group(1)):
            name = raw.strip()
            if not name:
                continue
            out.add(SKILL_NAMES.get(name.lower(), name))
            out |= find_skills(name)
    return out


# --------------------------------------------------------------------------
# Parsing

@dataclass
class Span:
    start: int
    end: int


@dataclass
class Bullet:
    id: str
    span: Span          # text after "\item" up to the next \item / \end{itemize}
    tex: str


@dataclass
class Entry:
    id: str
    kind: str           # "experience", "project" or "other"
    section: str
    title: str
    span: Span          # header start .. \end{itemize} end (what gets dropped)
    tech: Span | None
    tech_tex: str
    bullets: list[Bullet]
    allowed: set[str]   # skills that may appear in this entry


@dataclass
class SkillRow:
    id: str
    label: str
    span: Span
    tex: str


@dataclass
class Resume:
    tex: str
    entries: list[Entry] = field(default_factory=list)
    skill_rows: list[SkillRow] = field(default_factory=list)
    inventory: set[str] = field(default_factory=set)
    vocabulary: set[str] = field(default_factory=set)   # lowercased words in the master + tags

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.tex.encode("utf-8")).hexdigest()[:16]

    def bullet(self, bid: str) -> tuple[Entry, Bullet] | None:
        for e in self.entries:
            for b in e.bullets:
                if b.id == bid:
                    return e, b
        return None


SECTION = re.compile(r"\\section\*?\{([^}]*)\}")
ITEMIZE = re.compile(r"\\begin\{itemize\}(?:\[[^\]]*\])?(.*?)\\end\{itemize\}", re.S)
ITEM = re.compile(r"\\item\b")
TECH = re.compile(r"\|\s*\\textit\{")


def _brace_end(tex: str, open_idx: int) -> int:
    """Index just past the brace that closes the one at open_idx."""
    depth = 0
    i = open_idx
    while i < len(tex):
        c = tex[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced braces")


def _section_kind(title: str) -> str:
    t = title.lower()
    if re.search(r"experience|employment|work", t):
        return "experience"
    if re.search(r"project", t):
        return "project"
    return "other"


def parse(tex: str) -> Resume:
    if "\\begin{document}" not in tex:
        raise ValueError("This doesn't look like a full LaTeX document (no \\begin{document}).")
    res = Resume(tex=tex)
    body_start = tex.index("\\begin{document}")
    sections = [(m.start(), m.end(), m.group(1).strip()) for m in SECTION.finditer(tex, body_start)]
    doc_end = tex.find("\\end{document}")
    doc_end = doc_end if doc_end >= 0 else len(tex)

    def section_at(pos: int) -> tuple[str, int]:
        cur, start = "", body_start
        for s, e, title in sections:
            if s <= pos:
                cur, start = title, e
        return cur, start

    b_n = e_n = 0
    prev_end = body_start
    for m in ITEMIZE.finditer(tex, body_start, doc_end):
        title, sec_start = section_at(m.start())
        if re.search(r"skill", title, re.I):
            continue
        header_start = max(prev_end, sec_start)
        # Header begins at the first non-blank line after the previous block.
        while header_start < m.start() and tex[header_start] in " \t\r\n":
            header_start += 1
        header = tex[header_start:m.start()]
        e_n += 1
        eid = f"E{e_n}"

        tech_span, tech_tex = None, ""
        tm = TECH.search(header)
        if tm:
            open_idx = header_start + tm.end() - 1
            close = _brace_end(tex, open_idx)
            tech_span = Span(open_idx + 1, close - 1)
            tech_tex = tex[tech_span.start:tech_span.end]

        inner_start = m.start(1)
        inner = m.group(1)
        starts = [inner_start + im.end() for im in ITEM.finditer(inner)]
        bullets = []
        for i, s in enumerate(starts):
            e = (starts[i + 1] - len("\\item")) if i + 1 < len(starts) else m.end(1)
            raw = tex[s:e]
            lead = len(raw) - len(raw.lstrip())
            trail = len(raw) - len(raw.rstrip())
            span = Span(s + lead, e - trail)
            b_n += 1
            bullets.append(Bullet(f"B{b_n}", span, tex[span.start:span.end]))

        block = tex[header_start:m.end()]
        allowed = with_implied(find_skills(plain(block)) | tag_skills(block, "skills"))
        res.entries.append(Entry(
            id=eid, kind=_section_kind(title), section=title,
            title=plain(header)[:140] or f"Entry {e_n}",
            span=Span(header_start, m.end()), tech=tech_span, tech_tex=tech_tex,
            bullets=bullets, allowed=allowed,
        ))
        prev_end = m.end()

    # Skills rows: "\textbf{Label:} & content \\" or "\textbf{Label:} content \\"
    s_n = 0
    for idx, (s, e, title) in enumerate(sections):
        if not re.search(r"skill", title, re.I):
            continue
        end = sections[idx + 1][0] if idx + 1 < len(sections) else doc_end
        for rm in re.finditer(r"\\textbf\{([^}]*)\}\s*&?\s*(.+?)\s*(?:\\\\|$)", tex[e:end], re.M):
            content_start = e + rm.start(2)
            content_end = e + rm.end(2)
            content = tex[content_start:content_end]
            if not content.strip() or content.strip().startswith("\\end"):
                continue
            s_n += 1
            res.skill_rows.append(SkillRow(f"S{s_n}", plain(rm.group(1)).rstrip(":"),
                                           Span(content_start, content_end), content))

    everything = plain(tex)
    tags = " ".join(re.findall(r"(?<!\\)%\s*(?:skills|inventory)\s*:\s*(.+)", tex, re.I))
    res.inventory = with_implied(find_skills(everything) | tag_skills(tex, "skills") | tag_skills(tex, "inventory"))
    res.vocabulary = words(everything + " " + tags)
    if not res.entries:
        raise ValueError("No itemize blocks found. Bullets need to be inside \\begin{itemize} ... \\end{itemize}.")
    return res


def confirm(res: Resume, skills) -> Resume:
    """Add skills the person confirmed they have (the dashboard's Review
    skills) to what the skills section may list, and to the bullets and tech
    line of each entry that shows work in the same field: Kubernetes where an
    entry shows DevOps (its CI/CD or Docker work), not in a frontend one."""
    text = ", ".join(str(x).strip() for x in skills or () if str(x).strip())
    if text:
        named = tag_skills("% inventory: " + text, "inventory")
        res.inventory = with_implied(res.inventory | named)
        res.vocabulary |= words(text)
        for e in res.entries:
            for s in named:
                if with_implied({s}) & AREAS & e.allowed:
                    e.allowed = e.allowed | with_implied({s})
    return res


def describe(res: Resume) -> str:
    """The annotated view the model sees: every editable piece with its ID."""
    out = []
    for e in res.entries:
        out.append(f"ENTRY {e.id} [{e.kind}] {e.title}")
        if e.tech is not None:
            out.append(f"  TECH LINE {e.id}: {e.tech_tex}")
        out.append(f"  VERIFIED SKILLS FOR {e.id}: {', '.join(sorted(e.allowed)) or '(none detected)'}")
        for b in e.bullets:
            out.append(f"  {b.id}: {b.tex}")
        out.append("")
    if res.skill_rows:
        out.append("SKILLS SECTION")
        for r in res.skill_rows:
            out.append(f"  {r.id} ({r.label}): {r.tex}")
        out.append(f"  VERIFIED INVENTORY FOR SKILLS SECTION: {', '.join(sorted(res.inventory))}")
    return "\n".join(out)


def summary(res: Resume) -> dict:
    return {
        "digest": res.digest,
        "entries": [{"id": e.id, "kind": e.kind, "title": e.title, "bullets": len(e.bullets),
                     "tagged": sorted(tag_skills(res.tex[e.span.start:e.span.end], "skills"))}
                    for e in res.entries],
        "skill_rows": [r.label for r in res.skill_rows],
        "inventory": sorted(res.inventory),
    }


# --------------------------------------------------------------------------
# Work history and education, for filling application forms field by field

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE_RANGE = re.compile(
    rf"(?:(?P<sm>{_MON})\s+)?(?P<sy>(?:19|20)\d\d)\s*(?:-+|–|—|to)\s*"
    rf"(?:(?:(?P<em>{_MON})\s+)?(?P<ey>(?:19|20)\d\d)|(?P<cur>present|current|now|ongoing|today))", re.I)
EDUCATION = re.compile(r"educat|academic", re.I)
TABULAR = re.compile(r"\\begin\{tabular\*?\}.*?\\end\{tabular\*?\}", re.S)


def _month(s: str | None) -> int | None:
    return MONTHS.get(s[:3].lower()) if s else None


def _dates(text: str) -> dict:
    m = DATE_RANGE.search(text)
    if not m:
        return {"start": None, "end": None, "current": False}
    current = bool(m.group("cur"))
    return {"start": {"month": _month(m.group("sm")), "year": int(m.group("sy"))},
            "end": None if current else {"month": _month(m.group("em")), "year": int(m.group("ey"))},
            "current": current}


def _rows(header: str) -> list[list[str]]:
    """Table rows of an entry header, each split into cells (LaTeX source)."""
    body = header
    m = TABULAR.search(header)
    if m:
        # Skip the width and column spec, e.g. {\textwidth}{@{}l@{\extracolsep{\fill}}r@{}}.
        i = header.index("}", m.start()) + 1
        while i < len(header) and header[i] == "{":
            i = _brace_end(header, i)
        body = header[i:header.rindex("\\end{tabular", 0, m.end())]
    rows = re.split(r"(?<!\\)\\\\(?:\[[^\]]*\])?", body)
    out = []
    for row in rows:
        if not plain(row).strip():
            continue
        cells = re.split(r"(?<!\\)&|\\hfill\b", row)
        out.append([c.strip() for c in cells])
    return out


def _name_and_place(cell: str) -> tuple[str, str]:
    """"\\href{..}{\\textbf{Company}} - Dallas, TX" -> ("Company", "Dallas, TX")."""
    bold = re.search(r"\\textbf\{", cell)
    if bold:
        end = _brace_end(cell, bold.end() - 1)
        name = plain(cell[bold.end():end - 1])
        rest = plain(cell[end:])
    else:
        text = plain(cell)
        name, _, rest = text.rpartition(" - ") if " - " in text else (text, "", "")
    place = rest.strip().lstrip("-–—|,").strip().rstrip("}").strip()
    return name.strip(), place


def _entry_header(res: Resume, e: Entry) -> str:
    block = res.tex[e.span.start:e.span.end]
    cut = block.find("\\begin{itemize}")
    return block[:cut] if cut >= 0 else block


def history(res: Resume) -> dict:
    """The resume's jobs and schools as form fields: company, title,
    location, dates and the job's bullets as plain text; school, degree,
    field of study, GPA and years. Nothing is invented: a value the resume
    doesn't state is left empty."""
    work = []
    for e in res.entries:
        if e.kind != "experience":
            continue
        rows = _rows(_entry_header(res, e))
        company, location, title = "", "", ""
        if rows:
            company, location = _name_and_place(rows[0][0])
        if len(rows) > 1:
            title = plain(rows[1][0].split("|")[0]).strip()
        work.append({"company": company, "location": location, "title": title,
                     **_dates(plain(_entry_header(res, e))),
                     "bullets": [plain(b.tex) for b in e.bullets]})

    education = []
    tex = res.tex
    sections = [(m.start(), m.end(), m.group(1)) for m in SECTION.finditer(tex)]
    for i, (s, e_, title) in enumerate(sections):
        if not EDUCATION.search(title):
            continue
        end = sections[i + 1][0] if i + 1 < len(sections) else len(tex)
        for tm in TABULAR.finditer(tex, e_, end):
            rows = _rows(tm.group(0))
            if not rows:
                continue
            full, location = _name_and_place(rows[0][0])
            # "Khoury College of Computer Sciences, Northeastern University":
            # school lists on forms carry the university.
            parts = [p.strip() for p in full.split(",")]
            school = next((p for p in reversed(parts) if re.search(r"universit|institut|polytechnic", p, re.I)), full)
            degree_text = plain(rows[1][0]) if len(rows) > 1 else ""
            dm = re.match(r"^(.*?)\s+in\s+(.+)$", degree_text)
            degree, field_ = (dm.group(1), dm.group(2)) if dm else (degree_text, "")
            gpa = re.search(r"GPA\s*:?\s*([0-9.]+)(?:\s*/\s*([0-9.]+))?", plain(tm.group(0)), re.I)
            education.append({"school": school, "school_full": full, "location": location, "degree": degree.strip(),
                              "field": field_.strip(), "gpa": gpa.group(1) if gpa else "",
                              "gpa_scale": (gpa.group(2) or "") if gpa else "",
                              **_dates(plain(tm.group(0)))})
    projects = []
    for e in res.entries:
        if e.kind != "project":
            continue
        header = _entry_header(res, e)
        name, _ = _name_and_place(header.split("|")[0])
        tech = re.search(r"\\textit\{", header)
        link = re.search(r"\\href\{([^}]*)\}", header)
        span = _dates(plain(header))
        if not span["start"]:
            d = ONE_DATE.search(plain(header).split("|")[-1])
            if d:
                span = {"start": {"month": _month(d.group("m")), "year": int(d.group("y"))}, "end": None, "current": False}
        projects.append({"name": name or plain(header.split("|")[0]).strip(),
                         "tech": plain(header[tech.end():_brace_end(header, tech.end() - 1) - 1]) if tech else "",
                         "link": link.group(1) if link else "", **span,
                         "bullets": [plain(b.tex) for b in e.bullets]})
    return {"work": work, "education": education, "projects": projects,
            "skills": skill_list(res), "certifications": certifications(res)}


CERT_SECTION = re.compile(r"certif|licen", re.I)
ONE_DATE = re.compile(rf"(?:(?P<m>{_MON})\s+)?(?P<y>(?:19|20)\d\d)", re.I)


def certifications(res: Resume) -> list[dict]:
    """Entries of a Certifications (or Licenses) section, one per \\item or
    line: "AWS Certified Developer - Amazon Web Services, Mar 2025"."""
    tex = res.tex
    sections = [(m.start(), m.end(), m.group(1)) for m in SECTION.finditer(tex)]
    out = []
    for i, (s, e, title) in enumerate(sections):
        if not CERT_SECTION.search(title):
            continue
        end = sections[i + 1][0] if i + 1 < len(sections) else tex.find("\\end{document}", e)
        body = tex[e:end if end > e else len(tex)]
        body = re.sub(r"\\(begin|end)\{[^}]*\}(\[[^\]]*\])?", "\n", body)
        lines = re.split(r"\\item\b|(?<!\\)\\\\|\n\s*\n", body)
        for raw in lines:
            text = plain(raw).strip(" -–•|")
            if len(text) < 3:
                continue
            d = ONE_DATE.search(text)
            issued = {"month": _month(d.group("m")), "year": int(d.group("y"))} if d else None
            rest = (text[:d.start()] + text[d.end():] if d else text).strip(" ,|-–")
            parts = re.split(r"\s+[-–|]\s+|,\s+(?=[A-Z])", rest, maxsplit=1)
            name, issuer = (parts[0], parts[1]) if len(parts) > 1 else (rest, "")
            out.append({"name": name.strip(), "issuer": issuer.strip(" ,"), "issued": issued})
    return out


def skill_list(res: Resume) -> list[str]:
    """Every skill in the skills section, in order, for a form's skills box:
    "CI/CD (GitHub Actions)" counts as both CI/CD and GitHub Actions."""
    out: list[str] = []
    seen: set[str] = set()
    for row in res.skill_rows:
        text = plain(row.tex)
        depth, cur, items = 0, "", []
        for ch in text:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            if ch == "," and depth == 0:
                items.append(cur)
                cur = ""
            else:
                cur += ch
        items.append(cur)
        for item in items:
            m = re.match(r"^\s*(.*?)\s*\((.*)\)\s*$", item)
            for part in ([m.group(1)] + re.split(r"\s*,\s*", m.group(2))) if m else [item]:
                s = part.strip().strip(".;")
                if s and len(s) <= 60 and s.lower() not in seen:
                    seen.add(s.lower())
                    out.append(s)
    return out


# --------------------------------------------------------------------------
# Validation of model output

ALLOWED_CMDS = {"textbf", "textit", "emph", "href", "textasciitilde", "underline"}
TECHY = re.compile(r"\b(?:[A-Za-z]+\.js|[A-Za-z]*[A-Z][a-z]*[A-Z][A-Za-z]*|[A-Z]{2,}[a-z]?|[A-Za-z]+[0-9]+[A-Za-z0-9]*|[A-Za-z]+[+#]+)")


def sanitize(text: str) -> str:
    """Escape bare LaTeX specials the model may leave in prose (R&D, 40%)."""
    text = re.sub(r"\\\s+([&%$#_])", r"\\\1", text.strip())   # "\ &" -> "\&"
    text = re.sub(r"(?<!\\)([&%$#_])", r"\\\1", text)
    return re.sub(r"\s*\n\s*", " ", text)


def check_latex(text: str) -> str | None:
    depth = 0
    for i, c in enumerate(text):
        if c == "{" and (i == 0 or text[i - 1] != "\\"):
            depth += 1
        elif c == "}" and (i == 0 or text[i - 1] != "\\"):
            depth -= 1
            if depth < 0:
                return "unbalanced braces"
    if depth:
        return "unbalanced braces"
    for cmd in re.findall(r"\\([a-zA-Z@]+)", text):
        if cmd not in ALLOWED_CMDS:
            return f"uses \\{cmd}, which isn't allowed in tailored text"
    return None


def check_honesty(old: str, new: str, allowed: set[str], vocabulary: set[str], jd: str) -> str | None:
    """Reject an edit that claims something the master resume doesn't back."""
    old_p, new_p = plain(old), plain(new)
    added = find_skills(new_p) - find_skills(old_p)
    bad = sorted(added - allowed)
    if bad:
        return f"adds {', '.join(bad)}, which you haven't verified for this entry"
    old_nums = set(re.findall(r"\d+(?:\.\d+)?", old_p))
    new_nums = set(re.findall(r"\d+(?:\.\d+)?", new_p)) - old_nums
    if new_nums:
        return f"introduces numbers not in your original ({', '.join(sorted(new_nums))})"
    jd_words = words(jd)
    # A skill the entry is known to have ("LLMs" for a RAG chatbot on the
    # OpenAI API, "REST APIs" for one that built REST endpoints) may be named
    # in the job's words.
    backed = [m.span() for name, rx in SKILLS if name in allowed for m in rx.finditer(new_p)]
    for m in TECHY.finditer(new_p):
        tok, t = m.group(0), m.group(0).lower()
        if any(a <= m.start() and m.end() <= b for a, b in backed):
            continue
        if t not in vocabulary and t in jd_words and t not in old_p.lower():
            return f"adds \"{tok}\" from the job description, which appears nowhere in your master resume"
    return None


UMBRELLA = {"frontend", "backend", "full stack", "machine learning", "deep learning", "system design",
            "security", "oop", "data structures & algorithms", "testing", "agile", "llms", "nlp"}


def _split_top(text: str) -> list[str]:
    """Top-level comma-separated parts (commas inside parentheses, e.g.
    "CI/CD (GitHub Actions, Jenkins)", don't split)."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [i.strip() for i in out if i.strip()]


def _items(text: str) -> list[str]:
    """The items of a tool list, as plain text."""
    return _split_top(plain(text))


def _norm_item(s: str) -> str:
    s = re.sub(r"\.?js\b", "", s.lower())          # React.js == React
    return re.sub(r"[^a-z0-9+#&/]", "", s)


def drop_umbrella(old: str, new: str) -> tuple[str, list[str]]:
    """A tool list edit without the category names it adds ("Testing" beside
    "Unit testing, JUnit"): those are left out and the real tools kept."""
    before = {_norm_item(i) for i in _items(old)}
    keep, dropped = [], []
    for part in _split_top(new):
        name = plain(part).strip()
        if _norm_item(name) not in before and name.lower() in UMBRELLA:
            dropped.append(name)
        else:
            keep.append(part)
    return (", ".join(keep), dropped) if dropped else (new, [])


def trim_appended(old: str, new: str, chars: float) -> tuple[str, list[str]] | None:
    """`new`, a tool list that reorders and extends `old`, without enough of
    the items it added (last first) to print `chars` fewer characters:
    (text, items left out). None when the added items alone aren't enough."""
    before = {_norm_item(i) for i in _items(old)}
    parts = _split_top(new)
    added = [i for i, p in enumerate(parts) if _norm_item(plain(p).strip()) not in before]
    cut, gone = 0.0, []
    for i in reversed(added):
        if cut >= chars:
            break
        cut += len(plain(parts[i]).strip()) + 2
        gone.append(i)
    if cut < chars:
        return None
    return ", ".join(p for i, p in enumerate(parts) if i not in gone), [plain(parts[i]).strip() for i in reversed(gone)]


def _left_out(generic: list[str]) -> str:
    return f"left out {', '.join(generic)}: a category name, not a tool" if generic else ""


def check_list_edit(old: str, new: str) -> str | None:
    """Tool lists may be reordered and extended, never shortened."""
    old_items, new_items = _items(old), _items(new)
    have = {_norm_item(i) for i in new_items}
    lost = [i for i in old_items if _norm_item(i) not in have]
    if lost:
        return f"drops {', '.join(lost)} from the list"
    if len(plain(new)) > len(plain(old)) * 1.35 + 25:
        return "makes the list much longer"
    return None


@dataclass
class Change:
    kind: str           # bullet | tech | skills | drop_entry | drop_bullet | reorder
    target: str
    label: str
    before: str = ""
    after: str = ""
    status: str = "applied"   # applied | rejected
    reason: str = ""


# How far a rewritten bullet may grow, per tailoring strictness: (factor,
# extra characters) over the original's plain text. "reword" lets a bullet
# be restructured, which tends to lengthen it; "rewrite" may also draw on the
# entry's other bullets. The honesty checks above are the same at every
# level, measured against the bullet, or for "rewrite" against its entry.
# "none" rewrites no bullets.
BULLET_GROWTH = {"reword": (1.5, 40), "rewrite": (1.8, 50)}

# Words and shapes that mark a resume line as machine-written, on top of the
# user's own banned phrases (Settings, writing voice). A rewrite may not add
# one its original didn't have.
RESUME_TELLS = (
    "spearhead*", "utiliz*", "orchestrat*", "champion*", "showcas*", "harness*", "pioneer*", "revolutioniz*",
    "transformative", "pivotal", "innovative", "state-of-the-art", "best-in-class", "world-class", "meticulous*",
    "comprehensive", "holistic", "empower*", "foster*", "facilitat*", "elevat*", "bolster*", "underscor*",
    "proven track record", "results-driven", "detail-oriented", "high-impact", "game-changing", "a wide range of",
    "myriad", "plethora", "delv*", "tapestry", "seamless*", "robust", "cutting-edge", "leverag*", "synerg*",
    "not only ... but also", "in order to",
)
DASH = re.compile("[—–]")
# ", ensuring reliability" / ", enabling faster releases": a clause tacked on
# to sound like impact.
TACKED_ON = re.compile(r",\s+(?:thereby\s+)?(?:ensuring|enabling|driving|fostering|resulting in|leading to|"
                       r"allowing|empowering|showcasing|highlighting|underscoring|enhancing|boosting)\b", re.I)


def style_problems(old: str, new: str, banned=()) -> list[str]:
    """What a rewrite adds that reads as machine-written, as short phrases
    for the change log and the fix-up prompt."""
    old_p, new_p = plain(old), plain(new)
    out = []
    for rx in voice.compile_phrases(tuple(banned) + RESUME_TELLS):
        m = rx.search(new_p)
        if m and not rx.search(old_p):
            out.append(f'"{m.group(0)}"')
    if len(DASH.findall(new_p)) > len(DASH.findall(old_p)):
        out.append("a dash")
    m = TACKED_ON.search(new_p)
    if m and not TACKED_ON.search(old_p):
        out.append(f'a tacked-on "{m.group(0).strip(", ")} ..." ending')
    return out


def match_ending(old: str, new: str) -> str:
    """The rewrite ends the way the person ends their bullets: with a full
    stop or without."""
    if plain(old).rstrip().endswith("."):
        return new if plain(new).rstrip().endswith(".") else new.rstrip() + "."
    return new.rstrip()[:-1] if new.rstrip().endswith(".") else new


def entry_evidence(e: Entry) -> str:
    """Everything an entry says, which a "rewrite" bullet may draw on."""
    return "\n".join([e.title, e.tech_tex, *(b.tex for b in e.bullets)])


def plan_edits(res: Resume, out: dict, jd: str, strictness: str = "reword",
               banned=(), check: bool = True) -> tuple[list[tuple[Span, str]], list[Change], dict]:
    """Turn model output into concrete span replacements plus a change log.
    Anything that fails validation is logged as rejected and left unchanged.
    `check` False skips check_honesty (the user turned it off).
    Rewrites that read as machine-written are kept for now and listed in
    state["style"] ({id: problems}) for one fix-up pass (tailor.polish)."""
    factor, extra = BULLET_GROWTH.get(strictness, BULLET_GROWTH["reword"])
    changes: list[Change] = []
    rewrites: dict[str, str] = {}
    style: dict[str, list[str]] = {}

    for item in out.get("bullet_rewrites", []):
        found = res.bullet(item.get("id", ""))
        if not found:
            continue
        entry, b = found
        new = sanitize(item.get("text", ""))
        if not new or plain(new) == plain(b.tex):
            continue
        new = match_ending(b.tex, new)
        basis = entry_evidence(entry) if strictness == "rewrite" else b.tex
        reason = (check_latex(new)
                  or (len(plain(new)) > len(plain(b.tex)) * factor + extra and "makes the bullet much longer")
                  or (check and check_honesty(basis, new, entry.allowed, res.vocabulary, jd)))
        if reason:
            changes.append(Change("bullet", b.id, entry.title, plain(b.tex), plain(new), "rejected", reason))
        else:
            rewrites[b.id] = new
            changes.append(Change("bullet", b.id, entry.title, plain(b.tex), plain(new)))
            problems = style_problems(b.tex, new, banned)
            if problems:
                style[b.id] = problems

    tech_new: dict[str, str] = {}
    by_id = {e.id: e for e in res.entries}
    for item in out.get("tech_lines", []):
        e = by_id.get(item.get("id", ""))
        if not e or e.tech is None:
            continue
        new, generic = drop_umbrella(e.tech_tex, sanitize(item.get("text", "")))
        if not new or plain(new) == plain(e.tech_tex):
            continue
        reason = (check_latex(new) or check_list_edit(e.tech_tex, new)
                  or (check and check_honesty(e.tech_tex, new, e.allowed, res.vocabulary, jd)))
        if reason:
            changes.append(Change("tech", e.id, e.title, plain(e.tech_tex), plain(new), "rejected", reason))
        else:
            tech_new[e.id] = new
            changes.append(Change("tech", e.id, e.title, plain(e.tech_tex), plain(new), reason=_left_out(generic)))

    rows_new: dict[str, str] = {}
    rows = {r.id: r for r in res.skill_rows}
    for item in out.get("skills_rows", []):
        r = rows.get(item.get("id", ""))
        if not r:
            continue
        new, generic = drop_umbrella(r.tex, sanitize(item.get("text", "")))
        if not new or plain(new) == plain(r.tex):
            continue
        reason = (check_latex(new) or check_list_edit(r.tex, new)
                  or (check and check_honesty(r.tex, new, res.inventory, res.vocabulary, jd)))
        if reason:
            changes.append(Change("skills", r.id, r.label, plain(r.tex), plain(new), "rejected", reason))
        else:
            rows_new[r.id] = new
            changes.append(Change("skills", r.id, r.label, plain(r.tex), plain(new), reason=_left_out(generic)))

    # Nothing is removed up front. The model's drop suggestions only decide what
    # goes first if the compiled result runs past one page. Work history is
    # never suggested for removal (it would read as a gap); projects can be.
    suggest_entries = []
    for eid in out.get("drop_entries", []):
        e = by_id.get(eid)
        if e and e.kind != "experience" and eid not in suggest_entries:
            suggest_entries.append(eid)
    priority = [bid for bid in out.get("bullet_priority", []) if res.bullet(bid)]
    rank = {bid: i for i, bid in enumerate(priority)}
    suggest_bullets = [bid for bid in out.get("drop_bullets", []) if res.bullet(bid)]
    state = {"rewrites": rewrites, "tech": tech_new, "rows": rows_new,
             "drop_entries": set(), "drop_bullets": set(), "rank": rank,
             "suggest_entries": suggest_entries, "suggest_bullets": suggest_bullets, "style": style}
    return build_spans(res, state), changes, state


def project_groups(res: Resume) -> list[list[Entry]]:
    """Runs of consecutive project entries in the same section: the units
    whose order may change. Work experience is never reordered."""
    groups: list[list[Entry]] = []
    prev = None
    for e in res.entries:
        if e.kind == "project" and prev is not None and prev.kind == "project" and prev.section == e.section:
            groups[-1].append(e)
        elif e.kind == "project":
            groups.append([e])
        prev = e
    return [g for g in groups if len(g) > 1]


def entry_rank(e: Entry, rank: dict[str, int]) -> int:
    return min((rank.get(b.id, 999) for b in e.bullets), default=999)


def build_spans(res: Resume, state: dict, changes: list[Change] | None = None) -> list[tuple[Span, str]]:
    """Replacements for the current state. Bullets inside each kept entry are
    reordered most-relevant first; each entry keeps at least two bullets.
    Projects within a section are reordered by their most relevant bullet:
    each entry's text, with its own edits, moves into another entry's slot."""
    spans: list[tuple[Span, str]] = []
    rank = state["rank"]
    inner: dict[str, list[tuple[Span, str]]] = {}
    for e in res.entries:
        if e.id in state["drop_entries"]:
            spans.append((e.span, ""))
            continue
        inner[e.id] = _entry_spans(res, e, state, changes)

    moved: set[str] = set()
    for group in project_groups(res):
        kept = [e for e in group if e.id not in state["drop_entries"]]
        ordered = sorted(kept, key=lambda e: (entry_rank(e, rank), group.index(e)))
        if [e.id for e in ordered] == [e.id for e in kept]:
            continue
        # Dropped entries' slots are already emptied above; kept slots get the new order.
        for slot, e in zip(kept, ordered):
            text = apply(res.tex[e.span.start:e.span.end],
                         [(Span(s.start - e.span.start, s.end - e.span.start), t) for s, t in inner[e.id]])
            spans.append((slot.span, text))
        moved |= {e.id for e in kept}
        if changes is not None:
            changes.append(Change("reorder_entries", group[0].id, group[0].section,
                                  before="; ".join(e.title.split(" | ")[0] for e in kept),
                                  after="; ".join(e.title.split(" | ")[0] for e in ordered)))
    for eid, sp in inner.items():
        if eid not in moved:
            spans += sp

    measure = state.get("measure")
    for row in res.skill_rows:
        new = state["rows"].get(row.id)
        if new is not None or measure:
            text = row.tex if new is None else new
            spans.append((row.span, marked(row.id, text) if measure else text))
    return spans


def _entry_spans(res: Resume, e: Entry, state: dict, changes: list[Change] | None) -> list[tuple[Span, str]]:
    """Edits inside one kept entry: tech line, bullet rewrites, order and trims."""
    spans: list[tuple[Span, str]] = []
    rank = state["rank"]
    measure = state.get("measure")
    if e.tech is not None and (e.id in state["tech"] or measure):
        text = state["tech"].get(e.id, e.tech_tex)
        spans.append((e.tech, marked(e.id, text) if measure else text))
    kept = [b for b in e.bullets if b.id not in state["drop_bullets"]]
    if len(kept) < min(2, len(e.bullets)):
        kept = sorted(e.bullets, key=lambda b: rank.get(b.id, 999))[:min(2, len(e.bullets))]
    ordered = sorted(kept, key=lambda b: (rank.get(b.id, 999), e.bullets.index(b)))
    if changes is not None:
        for b in e.bullets:
            if b not in kept:
                changes.append(Change("drop_bullet", b.id, e.title, before=plain(b.tex)))
        if [b.id for b in ordered] != [b.id for b in kept]:
            changes.append(Change("reorder", e.id, e.title, after="most relevant bullets first"))
    texts = [state["rewrites"].get(b.id, b.tex) for b in ordered]
    if measure:
        texts = [marked(b.id, t) for b, t in zip(ordered, texts)]
    # Write the reordered bullets back into the original bullet slots.
    slots = e.bullets
    for i, slot in enumerate(slots):
        if i < len(texts):
            spans.append((slot.span, texts[i]))
        else:
            # Remove the whole "\item ..." for surplus slots.
            item_start = res.tex.rfind("\\item", 0, slot.span.start)
            spans.append((Span(item_start, slot.span.end), ""))
    return spans


def apply(tex: str, spans: list[tuple[Span, str]]) -> str:
    # Drop spans nested inside a dropped entry, then replace from the end.
    spans = sorted(spans, key=lambda s: (s[0].start, -s[0].end))
    flat: list[tuple[Span, str]] = []
    for sp, txt in spans:
        if flat and sp.start < flat[-1][0].end:
            continue
        flat.append((sp, txt))
    for sp, txt in reversed(flat):
        tex = tex[:sp.start] + txt + tex[sp.end:]
    return tex


# --------------------------------------------------------------------------
# Line layout: where each bullet, tech line and skills row starts and ends on
# the page, so an edit that spills a few words onto an extra line is caught.
# Each one is wrapped in \jrpos marks that save their position (\pdfsavepos,
# in pdfTeX and XeTeX) and write it to the log when the page ships. They
# print nothing and don't change the layout.

# A mark that starts a paragraph (a list item, a table cell) starts a line, so
# its lines all begin where it does; one that follows a label on the line
# ("Languages: ...") writes the paragraph's left edge, where the rest begin.
MEASURE_PREAMBLE = r"""\makeatletter
\providecommand\jrpos[1]{}%
\ifdefined\pdfsavepos
\renewcommand\jrpos[1]{\ifvmode\def\jr@left{-1pt}\else\edef\jr@left{\the\dimexpr1in+\hoffset+\oddsidemargin+\@totalleftmargin+\leftskip\relax}\fi\leavevmode\pdfsavepos\begingroup\edef\jr@pos{\endgroup\write-1{JRPOS #1 \noexpand\the\pdflastxpos\space\noexpand\the\pdflastypos\space\the\linewidth\space\the\dimexpr\baselineskip\relax\space\noexpand\the\c@page\space\jr@left}}\jr@pos}%
\fi
\makeatother
"""
JRPOS = re.compile(r"^JRPOS (\S+):([se]) (-?\d+) (-?\d+) ([\d.]+)pt ([\d.]+)pt (\d+) (-?[\d.]+)pt", re.M)


class Fit(NamedTuple):
    lines: int
    fill: float     # how much of the width the last line holds, 0 to 1
    lead: float     # how much of the first line a label before it takes


def marked(mid: str, text: str) -> str:
    """`text` with a mark before its first character and after its last.
    Trailing comments (hidden skill tags) stay after the end mark, since a
    mark inside a comment would never be read."""
    body = text.rstrip()
    while True:
        last = body.rfind("\n") + 1
        line = body[last:]
        cut = re.search(r"(?<!\\)%", line)
        if not cut:
            break
        body = body[:last + cut.start()].rstrip()
    lead = len(body) - len(body.lstrip())
    if lead == len(body):
        return text
    return (text[:lead] + f"\\jrpos{{{mid}:s}}" + body[lead:] + f"\\jrpos{{{mid}:e}}" + text[len(body):])


def with_marks(tex: str) -> str:
    """The resume with \\jrpos defined; the marks themselves come from
    build_spans with state["measure"]."""
    i = tex.find("\\begin{document}")
    return tex if i < 0 else tex[:i] + MEASURE_PREAMBLE + tex[i:]


def read_layout(log: str) -> dict[str, dict]:
    """{id: {"s": (x, y, page), "e": (x, y, page), "width", "baseline", "left"}}
    from a compile log, in sp; "left" is None when the piece starts a line."""
    out: dict[str, dict] = {}
    for m in JRPOS.finditer(log):
        mid, end, x, y, w, bs, page, left = m.groups()
        d = out.setdefault(mid, {})
        d[end] = (int(x), int(y), int(page))
        if end == "s":
            d["width"], d["baseline"] = float(w) * 65536, float(bs) * 65536
            d["left"] = None if float(left) < 0 else float(left) * 65536
    return out


def line_fit(layout: dict, mid: str) -> Fit | None:
    """How one marked piece sits on the page, or None when it wasn't
    measured (split across pages, or an engine without \\pdfsavepos)."""
    d = layout.get(mid) or {}
    s, e, w = d.get("s"), d.get("e"), d.get("width", 0)
    if not s or not e or s[2] != e[2] or w <= 0:
        return None
    dy, bs = s[1] - e[1], d.get("baseline", 0)
    lines = 1 if abs(dy) < 65536 or bs <= 0 else max(1, round(dy / bs) + 1)
    left = s[0] if d.get("left") is None else d["left"]
    clamp = lambda v: min(1.0, max(0.0, v))
    return Fit(lines, clamp((e[0] - left) / w), clamp((s[0] - left) / w))


# --------------------------------------------------------------------------
# Compilation

class CompileError(Exception):
    pass


def engine_for(tex: str) -> str:
    return "xelatex" if re.search(r"\\usepackage(\[[^\]]*\])?\{fontspec\}", tex) else "pdflatex"


def compile_pdf(tex: str, out_pdf: Path | None = None, timeout: int = 120, layout: dict | None = None) -> int:
    """Compile and return the page count. Shell escape is always off. An
    installed TeX is used when there is one, else Tectonic (texengine.py).
    `layout` gets the marked pieces' positions (read_layout)."""
    engine = engine_for(tex)
    found = texengine.pick(engine)
    if not found:
        raise CompileError(f"{engine} is not installed. Install a TeX distribution, or let Onward "
                           "download Tectonic under Settings, App.")
    kind, exe = found
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "resume.tex"
        if kind == "tex":
            src.write_text(tex, encoding="utf-8")
            cmd, env = [exe, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "resume.tex"], None
        else:
            engine = "Tectonic"
            src.write_text(texengine.PDFTEX_SHIM + tex, encoding="utf-8")
            cmd, env = [exe, "-X", "compile", "--untrusted", "--keep-logs", "resume.tex"], texengine.tectonic_env()
            timeout = max(timeout, 600)     # the first compile fetches the packages the resume uses
        try:
            proc = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace", env=env)
        except subprocess.TimeoutExpired as e:
            raise CompileError(f"{engine} timed out") from e
        log = (Path(tmp) / "resume.log").read_text(encoding="utf-8", errors="replace") \
            if (Path(tmp) / "resume.log").exists() else proc.stdout
        m = re.search(r"Output written on .*?\((\d+) pages?", log, re.S)
        if proc.returncode != 0 or not m:
            err = re.findall(r"^! .*$", log, re.M)
            raise CompileError(err[0] if err else f"{engine} failed (exit {proc.returncode})")
        if out_pdf is not None:
            out_pdf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(tmp) / "resume.pdf", out_pdf)
        if layout is not None:
            layout.clear()
            layout.update(read_layout(log))
        return int(m.group(1))
