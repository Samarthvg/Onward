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
# The kinds of work a tool is used for, for telling which tools stand near
# each other: the fields above (Machine Learning, Cloud, Frontend) and a few
# groupings that aren't fields (SQL databases, message queues). Languages and
# everyday tools (Python, Git) are left out: sharing Python doesn't make
# Pandas and PyTorch the same kind of tool. So is DevOps, too wide a field:
# Jenkins doesn't stand in for Kubernetes.
FAMILIES = (AREAS - {"DevOps"}) | {"SQL", "NoSQL", "CI/CD", "Message Queues", "Testing", "Data Analysis",
                    "Infrastructure as Code", "Build Systems"}
EVERYDAY = {"Python", "JavaScript", "TypeScript", "Java", "Go", "C++", "C#", "C", "Rust", "Ruby", "PHP", "Kotlin",
            "Swift", "Scala", "HTML/CSS", "Git", "Linux", "Bash", "SQL"} & set(SKILL_NAMES.values())


def related(skill: str, have) -> list[str]:
    """The skills in `have` used for the same kind of work as `skill`: one's
    own field is in the other's chain (scikit-learn is Machine Learning,
    which PyTorch's Deep Learning is part of), one runs on the other (Helm
    on Kubernetes), or `have` names the field itself. Concrete tools first."""
    up = with_implied({skill})
    near = IMPLIES.get(skill, set()) & FAMILIES
    out = []
    for h in sorted(set(have) - {skill} - EVERYDAY):
        if h in FAMILIES or h in AREAS:
            if h in FAMILIES and h in up:
                out.append(h)
        elif h in IMPLIES.get(skill, ()) or skill in IMPLIES.get(h, ())                 or (IMPLIES.get(h, set()) & FAMILIES & up) or (near & with_implied({h})):
            out.append(h)
    return sorted(out, key=lambda h: h in FAMILIES)


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
    fact: bool = False  # drawn in from the fact sheet (facts.py), not the resume's own
    whole: Span | None = None   # the bullet with its command: "\item text" or "\resumeItem{text}"


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
    fact: bool = False  # drawn in from the fact sheet (facts.py), not the resume's own
    items: Span | None = None   # its bullet list, from where it opens to where it closes


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
ITEM = re.compile(r"\\item\b")
# A tool list after the name: "| \textit{Go, SQL}", or Jake's "$|$ \emph{Go, SQL}".
TECH = re.compile(r"(?:\$\|\$|\|)\s*\\(?:textit|emph)\{")
DEFINE = re.compile(r"\\(?:re)?newcommand\*?\s*\{?\s*\\([A-Za-z@]+)\s*\}?\s*(?:\[(\d)\])?\s*(?:\[[^\]]*\])?\s*(?=\{)")


@dataclass
class ListMacros:
    """Commands a resume defines for its bullet lists, as Jake's template does:
    \resumeItemListStart opens one, \resumeItemListEnd closes it, and
    \resumeItem{..} (or \resumeSubItem{..}, which calls it) is one bullet."""
    starts: set[str] = field(default_factory=set)
    ends: set[str] = field(default_factory=set)
    items: set[str] = field(default_factory=set)


def list_macros(tex: str) -> ListMacros:
    """A command whose body only opens an itemize opens a list, one that only
    closes it closes one, and a one-argument command whose body is an \item
    holding its argument, or calls such a command with it, is a bullet. A
    command that draws a table (a heading) is none of these."""
    defs = {}
    for m in DEFINE.finditer(tex):
        end = _brace_end(tex, m.end())
        defs[m.group(1)] = (int(m.group(2) or 0), tex[m.end() + 1:end - 1])
    out = ListMacros()
    for name, (_, body) in defs.items():
        opens, closes = "\\begin{itemize}" in body, "\\end{itemize}" in body
        if opens and not closes:
            out.starts.add(name)
        elif closes and not opens:
            out.ends.add(name)
    grew = True
    while grew:
        grew = False
        for name, (nargs, body) in defs.items():
            if name in out.items or nargs != 1 or "#1" not in body or "tabular" in body:
                continue
            if ITEM.search(body) or any(re.search(rf"\\{re.escape(x)}(?![A-Za-z@])", body) for x in out.items):
                out.items.add(name)
                grew = True
    return out


def _commented(tex: str, pos: int) -> bool:
    line = tex[tex.rfind("\n", 0, pos) + 1:pos]
    return bool(re.search(r"(?<!\\)%", line))


def bullet_lists(tex: str, start: int, end: int, macros: ListMacros) -> list[tuple[int, int, int, int]]:
    """The innermost lists in tex[start:end] that hold bullets, as (where it
    opens, where its content starts, where its content ends, where it
    closes). A list of entries (Jake's \resumeSubHeadingListStart) holds
    lists of its own, so it isn't one."""
    names = lambda xs: "".join(rf"|\\{re.escape(x)}(?![A-Za-z@])" for x in sorted(xs, key=len, reverse=True))
    token = re.compile(rf"(?P<open>\\begin\{{itemize\}}(?:\[[^\]]*\])?{names(macros.starts)})"
                       rf"|(?P<close>\\end\{{itemize\}}{names(macros.ends)})")
    stack: list[list] = []
    out = []
    for m in token.finditer(tex, start, end):
        if _commented(tex, m.start()):
            continue
        if m.group("open"):
            if stack:
                stack[-1][2] = True                 # its parent holds a list: not innermost
            stack.append([m.start(), m.end(), False])
        elif stack:
            opened, content, nested = stack.pop()
            if not nested and _bullets_in(tex, content, m.start(), macros):
                out.append((opened, content, m.start(), m.end()))
    return sorted(out)


def _bullets_in(tex: str, start: int, end: int, macros: ListMacros) -> list[tuple[int, int, bool]]:
    """The bullets between start and end: (where the command starts, where
    its text starts, whether it's a command taking the text as its argument)."""
    names = "".join(rf"|\\{re.escape(x)}\s*\{{" for x in sorted(macros.items, key=len, reverse=True))
    out = []
    for m in re.finditer(rf"\\item\b{names}", tex[start:end]):
        if _commented(tex, start + m.start()):
            continue
        out.append((start + m.start(), start + m.end(), not m.group(0).startswith("\\item")))
    return out


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


# What facts.combine() marks the entries and bullets it draws in with.
FACT_MARK = "% from your fact sheet"

# Bullets a tailored entry shows: at least the fewest (Settings, MIN_BULLETS
# by default; all of them when the resume's own entry has fewer), at most what
# the resume's own entries of its kind show, never more than MOST_BULLETS
# (tailor.bullet_limits).
MIN_BULLETS = 3
MOST_BULLETS = 5


def _section_kind(title: str) -> str:
    t = title.lower()
    # Course projects are projects: they may be reordered and trimmed. "Work"
    # counts only as a word, since "coursework" ends in it. A section of both
    # jobs and projects stays experience, so no job is ever dropped.
    if re.search(r"coursework|\bcourses?\b", t):
        return "project"
    if re.search(r"experience|employment|\bwork\b", t):
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

    macros = list_macros(tex[:body_start])
    opener = re.compile(r"\s*(?:\\begin\{itemize\}(?:\[[^\]]*\])?" + "".join(
        rf"|\\{re.escape(x)}(?![A-Za-z@])" for x in macros.starts) + ")")
    b_n = e_n = 0
    prev_end = body_start
    for opened, content, content_end, closed in bullet_lists(tex, body_start, doc_end, macros):
        title, sec_start = section_at(opened)
        if re.search(r"skill", title, re.I):
            continue
        header_start = max(prev_end, sec_start)
        # Header begins at the first non-blank line after the previous block, past
        # the opening of a list of entries (Jake's), which isn't this entry's.
        while True:
            while header_start < opened and tex[header_start] in " \t\r\n":
                header_start += 1
            om = opener.match(tex, header_start, opened)
            if not om or om.end() == header_start:
                break
            header_start = om.end()
        header = tex[header_start:opened]
        e_n += 1
        eid = f"E{e_n}"

        tech_span, tech_tex = None, ""
        tm = TECH.search(header)
        if tm:
            open_idx = header_start + tm.end() - 1
            close = _brace_end(tex, open_idx)
            tech_span = Span(open_idx + 1, close - 1)
            tech_tex = tex[tech_span.start:tech_span.end]

        found = _bullets_in(tex, content, content_end, macros)
        bullets = []
        for i, (cmd, s, braced) in enumerate(found):
            nxt = found[i + 1][0] if i + 1 < len(found) else content_end
            if braced:
                # \resumeItem{text}: the text is the argument; what follows it on its line may be a mark.
                close = _brace_end(tex, s - 1)
                raw, tail = tex[s:close - 1], tex[close:nxt]
                whole_end = close
            else:
                raw, tail = tex[s:nxt], ""
            # A fact sheet bullet's mark stays in the file, outside the bullet's own text.
            fact = FACT_MARK in raw or FACT_MARK in tail.split("\n")[0]
            if FACT_MARK in raw:
                raw = raw[:raw.index(FACT_MARK)]
            lead = len(raw) - len(raw.lstrip())
            trail = len(raw) - len(raw.rstrip())
            span = Span(s + lead, s + len(raw) - trail)
            b_n += 1
            bullets.append(Bullet(f"B{b_n}", span, tex[span.start:span.end], fact,
                                  Span(cmd, whole_end if braced else span.end)))

        block = tex[header_start:closed]
        parts, is_call = header_parts(header)
        allowed = with_implied(find_skills(plain(block)) | tag_skills(block, "skills"))
        res.entries.append(Entry(
            id=eid, kind=_section_kind(title), section=title,
            title=(" | ".join(parts) if is_call else plain(header))[:140] or f"Entry {e_n}",
            span=Span(header_start, closed), tech=tech_span, tech_tex=tech_tex,
            bullets=bullets, allowed=allowed, fact=FACT_MARK in header, items=Span(opened, closed),
        ))
        prev_end = closed

    # Skills rows: "\textbf{Label:} & content \\" or "\textbf{Label:} content \\"
    s_n = 0
    for idx, (s, e, title) in enumerate(sections):
        if not re.search(r"skill", title, re.I):
            continue
        end = sections[idx + 1][0] if idx + 1 < len(sections) else doc_end
        for rm in re.finditer(r"\\textbf\{([^}]*)\}\s*&?\s*(.+?)\s*(?:\\\\|$)", tex[e:end], re.M):
            content_start = e + rm.start(2)
            content_end = e + rm.end(2)
            # Jake's "\textbf{Languages}{: Python, Go}": the items are what follows the colon.
            jm = re.match(r"\{\s*:\s*", tex[content_start:content_end])
            if jm and tex[content_end - 1] == "}":
                content_start, content_end = content_start + jm.end(), content_end - 1
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
        raise ValueError("No bullet lists found. Bullets need to be in lists, \\begin{itemize} with \\item, "
                         "or commands the resume defines for them, like \\resumeItem.")
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
        out.append(f"ENTRY {e.id} [{e.kind}]{' (fact sheet)' if e.fact else ''} {e.title}")
        if e.tech is not None:
            out.append(f"  TECH LINE {e.id}: {e.tech_tex}")
        out.append(f"  VERIFIED SKILLS FOR {e.id}: {', '.join(sorted(e.allowed)) or '(none detected)'}")
        for b in e.bullets:
            out.append(f"  {b.id}{' (fact sheet)' if b.fact and not e.fact else ''}: {b.tex}")
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


BAR = re.compile(r"\s*(?:\\textbar\b(?:\{\})?|\$\|\$)\s*")
FORMATTING = {"href", "hyperlink", "url", "textbf", "textit", "emph", "underline", "textsc", "small", "large",
              "Large", "textcolor", "color", "mbox", "makebox"}


def header_parts(header: str) -> tuple[list[str], bool]:
    """An entry header's parts as plain text, and whether they're a command's
    arguments: a table's cells, or the arguments of the one call the header
    is, such as \\job{Acme -- Boston}{2024 -- 2025}{Engineer \\textbar{} Go}
    or Jake's \\resumeSubheading{Engineer}{2024 -- 2025}{Acme}{Boston, MA}.
    "|" separators come out as " | ", and LaTeX's -- and --- as dashes."""
    h = strip_comments(header)
    # The last call with two or more arguments is the entry's own header line;
    # spacing macros and a section's intro may come before it.
    call = []
    for m in re.finditer(r"\\([A-Za-z]+)\*?\s*(?=\{)", h):
        if m.group(1) in FORMATTING:         # \href{url}{Acme} is a link in a header, not the header
            continue
        args, at = [], m.end()
        while at < len(h) and h[at] == "{":
            end = _brace_end(h, at)
            args.append(h[at + 1:end - 1])
            at = end
            while at < len(h) and h[at] in " \t\r\n":
                at += 1
        if len(args) >= 2 and not plain(h[at:]).strip():
            call = args
    raw = call or [c for row in _rows(header) for c in row]
    cells = [re.sub(r"\s+", " ", plain(BAR.sub(" | ", c)).replace("---", "—").replace("--", "–")).strip() for c in raw]
    return [c for c in cells if c], bool(call)


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
    if e.items is not None:
        return res.tex[e.span.start:e.items.start]
    block = res.tex[e.span.start:e.span.end]
    cut = block.find("\\begin{itemize}")
    return block[:cut] if cut >= 0 else block


# What a header's parts are, told by how they read, for a header that's a
# command's arguments in an order of its own (Jake's \resumeSubheading{Role}
# {Dates}{Company}{Place}, \resumeSubheading{School}{Place}{Degree}{Dates}).
_DATE_PART = rf"(?:(?:{_MON}|spring|summer|fall|autumn|winter)\s+)?(?:19|20)\d\d|present|current|now|ongoing"
DATES_ONLY = re.compile(rf"\s*(?:expected\s+)?(?:{_DATE_PART}|{_MON})(?:\s*(?:-{{1,3}}|–|—|to)\s*(?:{_DATE_PART}))?\s*", re.I)
# Between a name and its place in one part: "Acme - Dallas, TX".
PART_DASH = r"\s+(?:-{1,3}|–|—)\s+"
ROLE = re.compile(r"\b(?:engineer|developer|intern|internship|assistant|analyst|researcher|scientist|manager|"
                  r"consultant|associate|lead|architect|designer|specialist|fellow|programmer|administrator|"
                  r"coordinator|director|officer|technician|founder|co-founder|tutor|instructor|ta)\b", re.I)
PLACE = re.compile(r"(?:remote|[A-Z][\w.'-]*(?: [A-Z][\w.'-]*)*, (?:[A-Z]{2}|USA|U\.S\.A?\.?|United States|India|Canada|UK|"
                   r"United Kingdom|Germany|France|Ireland|Netherlands|Spain|Italy|China|Japan|Singapore|Australia|"
                   r"Israel|Mexico|Brazil))(?:\s*\(remote\))?", re.I)
SCHOOL = re.compile(r"universit|college|institut|school|academy|polytechnic", re.I)
DEGREE = re.compile(r"\b(?:bachelor|master|doctor|ph\.?\s?d|b\.?\s?s|m\.?\s?s|b\.?\s?a|m\.?\s?a|b\.?\s?tech|m\.?\s?tech|"
                    r"b\.?\s?e|m\.?\s?e|mba|associate|diploma|degree)\b", re.I)


def is_dates(text: str) -> bool:
    """A part that's only dates: "May 2024 - Aug 2024", "2024", "Feb - Apr 2026", "Spring 2025"."""
    return bool(DATES_ONLY.fullmatch(text))


def header_fields(parts: list[str], kind: str) -> dict[str, str]:
    """{"name", "place", "title", "tools", "dates", "link"} of a header's
    parts: dates, a link and a place by their look, for a job the part naming
    a role, then the name (a job's "Acme - Dallas" gives both) and a comma
    list of tools."""
    f = dict.fromkeys(("name", "place", "title", "tools", "dates", "link"), "")
    rest = []
    for p in (x.strip() for c in parts for x in c.split(" | ")):
        if not p:
            continue
        if not f["dates"] and is_dates(p):
            f["dates"] = p
        elif not f["link"] and re.match(r"https?://\S+$", p):
            f["link"] = p
        elif not f["place"] and PLACE.fullmatch(p):
            f["place"] = p
        elif kind == "experience" and not f["title"] and ROLE.search(p):
            f["title"] = p
        else:
            rest.append(p)
    if rest:
        name = rest.pop(0)
        if kind != "project" and not f["place"]:
            name, *place = re.split(PART_DASH, name, maxsplit=1)
            f["place"] = place[0].strip() if place else ""
        f["name"] = name.strip()
    for p in rest:
        key = "tools" if not f["tools"] and "," in p else "title" if not f["title"] else "tools" if not f["tools"] else ""
        if key:
            f[key] = p
    return f


def _calls(text: str, least: int = 2):
    """The arguments of each command call in `text` with at least `least` of them, as LaTeX."""
    for m in re.finditer(r"\\([A-Za-z]+)\*?\s*(?=\{)", text):
        if m.group(1) in FORMATTING:
            continue
        args, at = [], m.end()
        while at < len(text) and text[at] == "{":
            end = _brace_end(text, at)
            args.append(text[at + 1:end - 1])
            at = end
            while at < len(text) and text[at] in " \t\r\n":
                at += 1
        if len(args) >= least:
            yield m.start(), at, args


def history(res: Resume) -> dict:
    """The resume's jobs and schools as form fields: company, title,
    location, dates and the job's bullets as plain text; school, degree,
    field of study, GPA and years. Nothing is invented: a value the resume
    doesn't state is left empty."""
    work = []
    for e in res.entries:
        if e.kind != "experience":
            continue
        header = _entry_header(res, e)
        parts, is_call = header_parts(header)
        company, location, title = "", "", ""
        if is_call:
            # A command's arguments in an order of its own: told apart by how they read.
            f = header_fields(parts, "experience")
            company, location, title = f["name"], f["place"], f["title"]
        else:
            rows = _rows(header)
            if rows:
                company, location = _name_and_place(rows[0][0])
            if len(rows) > 1:
                title = plain(rows[1][0].split("|")[0]).strip()
        work.append({"company": company, "location": location, "title": title,
                     **_dates(" ".join(parts) if is_call else plain(header)),
                     "bullets": [plain(b.tex) for b in e.bullets]})

    education = []
    tex = res.tex
    sections = [(m.start(), m.end(), m.group(1)) for m in SECTION.finditer(tex)]
    for i, (s, e_, title) in enumerate(sections):
        if not EDUCATION.search(title):
            continue
        end = sections[i + 1][0] if i + 1 < len(sections) else len(tex)
        if not TABULAR.search(tex, e_, end):
            education += _schools_from_calls(tex[e_:end])
            continue
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
        tech = TECH.search(header) or re.search(r"\\textit\{", header)
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


def _schools_from_calls(text: str) -> list[dict]:
    """Schools written as commands (Jake's \\resumeSubheading{School}{Place}
    {Degree}{Dates}): each call of three or more arguments naming a school,
    its parts told apart by how they read."""
    out = []
    for _, _, args in _calls(text, 3):
        parts = [re.sub(r"\s+", " ", plain(BAR.sub(" | ", a)).replace("--", "–")).strip() for a in args]
        school = next((p for p in parts if SCHOOL.search(p)), "")
        if not school:
            continue
        degree_text = next((p for p in parts if p != school and not PLACE.fullmatch(p) and DEGREE.search(p)), "")
        place = next((p for p in parts if PLACE.fullmatch(p)), "")
        dm = re.match(r"^(.*?)\s+in\s+(.+?)(?:,\s*minor.*)?$", degree_text, re.I)
        degree, field_ = (dm.group(1), dm.group(2)) if dm else (degree_text, "")
        gpa = re.search(r"GPA\s*:?\s*([0-9.]+)(?:\s*/\s*([0-9.]+))?", " ".join(parts), re.I)
        out.append({"school": school, "school_full": school, "location": place, "degree": degree.strip(),
                    "field": field_.strip(), "gpa": gpa.group(1) if gpa else "",
                    "gpa_scale": (gpa.group(2) or "") if gpa else "", **_dates(" ".join(parts))})
    return out


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
    fact: bool = False        # about a piece from the fact sheet


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
    if new_p.count(";") > old_p.count(";"):
        out.append("a semicolon joining two clauses")
    return out


def _opening(text: str) -> str:
    m = re.match(r"\s*([A-Za-z][A-Za-z-]*)", plain(text))
    return m.group(1).lower() if m else ""


def repeated_opening(res: Resume, rewrites: dict[str, str], bid: str, text: str) -> str | None:
    """The word a reworded bullet opens with when another bullet of its entry
    (as the entry stands with `rewrites`) opens with it too: two "Built ..."
    in a row read as generated. None when the person's own bullet opened that
    way already."""
    found = res.bullet(bid)
    w = _opening(text)
    if not found or not w or w == _opening(found[1].tex):
        return None
    entry, _ = found
    for o in entry.bullets:
        if o.id != bid and _opening(rewrites.get(o.id, o.tex)) == w:
            return plain(text).split()[0]
    return None


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

    for bid, new in rewrites.items():
        word = repeated_opening(res, rewrites, bid, new)
        if word:
            style.setdefault(bid, []).append(f'opens with "{word}" like another bullet of this entry')

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
    reordered most-relevant first; each entry keeps at least the fewest it may show.
    Projects within a section are reordered by their most relevant bullet:
    each entry's text, with its own edits, moves into another entry's slot."""
    spans: list[tuple[Span, str]] = []
    rank = state["rank"]
    inner: dict[str, list[tuple[Span, str]]] = {}
    whole = {e.id for _, _, mine in emptied_sections(res, state) for e in mine}
    for e in res.entries:
        if e.id in state["drop_entries"]:
            # A "%" in its place, so the lines either side of it don't meet in a blank
            # line, which would start a paragraph and indent the next entry (tidy()).
            # A section that goes whole takes the lines around it too (_layout_spans).
            keep_apart = e.id not in whole and res.tex[e.span.end:e.span.end + 1] == "\n"
            spans.append((e.span, DROPPED if keep_apart else ""))
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
    spans += _layout_spans(res, state)

    measure = state.get("measure")
    for row in res.skill_rows:
        new = state["rows"].get(row.id)
        if new is not None or measure:
            text = row.tex if new is None else new
            spans.append((row.span, marked(row.id, text) if measure else text))
    return spans


DROPPED = "%"
_GAP = r"[ \t]*\\(?:vspace\*?\{[^}]*\}|[A-Za-z]*(?:gap|skip)\b)[^\n]*\n"


def tidy(tex: str) -> str:
    """A dropped entry's "%" with the spacing lines either side of it: one
    gap, as between any two entries, rather than two."""
    return re.sub(rf"({_GAP})(?:[ \t]*%[ \t]*\n(?:{_GAP})?)+", r"\1", tex)


PAGE_BREAK = re.compile(r"\\(?:clearpage|newpage|pagebreak)\b(?:\[[^\]]*\])?")


def _layout_spans(res: Resume, state: dict) -> list[tuple[Span, str]]:
    """Forced page breaks between entries go: a master's breaks fall where its
    own length put them, and after trimming they'd start a page early. A
    section whose entries were all dropped goes whole: its heading and any
    text of its own, which was about those entries."""
    tex = res.tex
    body = tex.find("\\begin{document}")
    end = tex.find("\\end{document}")
    end = end if end >= 0 else len(tex)
    spans: list[tuple[Span, str]] = []
    for start, stop, mine in emptied_sections(res, state):
        # Entries' own spans are emptied already; the gaps around them go here.
        at = start
        for e in mine:
            spans.append((Span(at, e.span.start), ""))
            at = e.span.end
        spans.append((Span(at, stop), ""))
    gone = lambda pos: (any(sp.start <= pos < sp.end for sp, _ in spans)
                        or any(e.span.start <= pos < e.span.end for e in res.entries))
    spans += [(Span(m.start(), m.end()), "") for m in PAGE_BREAK.finditer(tex, body, end) if not gone(m.start())]
    return spans


def emptied_sections(res: Resume, state: dict) -> list[tuple[int, int, list[Entry]]]:
    """Sections whose entries were all dropped: (heading start, section end, its entries)."""
    tex = res.tex
    body = tex.find("\\begin{document}")
    end = tex.find("\\end{document}")
    end = end if end >= 0 else len(tex)
    heads = list(SECTION.finditer(tex, body, end))
    out = []
    for i, h in enumerate(heads):
        stop = heads[i + 1].start() if i + 1 < len(heads) else end
        mine = sorted((e for e in res.entries if h.end() <= e.span.start < stop), key=lambda e: e.span.start)
        if mine and all(e.id in state["drop_entries"] for e in mine):
            out.append((h.start(), stop, mine))
    return out


def _entry_spans(res: Resume, e: Entry, state: dict, changes: list[Change] | None) -> list[tuple[Span, str]]:
    """Edits inside one kept entry: tech line, bullet rewrites, order and trims."""
    spans: list[tuple[Span, str]] = []
    rank = state["rank"]
    measure = state.get("measure")
    if e.tech is not None and (e.id in state["tech"] or measure):
        text = state["tech"].get(e.id, e.tech_tex)
        spans.append((e.tech, marked(e.id, text) if measure else text))
    kept = [b for b in e.bullets if b.id not in state["drop_bullets"]]
    low = min(state.get("min_bullets", MIN_BULLETS), len(e.bullets))
    if len(kept) < low:
        kept = sorted(e.bullets, key=lambda b: rank.get(b.id, 999))[:low]
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
            # Remove the whole bullet, its command too ("\item ..." or "\resumeItem{...}").
            spans.append((slot.whole, ""))
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
