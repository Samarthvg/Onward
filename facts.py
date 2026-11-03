"""The fact sheet: what you've done that your resume has no room for.

Your resume (the master .tex) is the reference: its layout, its order, and
what an application sends when it isn't tailored. The fact sheet
(resume/facts.txt, next to it) holds the rest in resume_import's outline
form, so nobody writes LaTeX for it:

    # Projects
    ## Edge Detector | C++, OpenCV | Spring 2025 | github.com/me/edges
    - Wrote Sobel X/Y filters and gradient magnitude in C++ with OpenCV.
    # Work Experience
    ## Cognitus
    - Another bullet for a job the resume already has.
    # Skills
    Vision: OpenCV, Law's filters

Before tailoring, combine() draws each entry into one of the resume's own
sections: the one with the same name, else its job section for jobs and its
projects section for the rest (coursework, research), so a tailored version
has the sections the resume has. Each is drawn in the pattern of an entry
already there, its LaTeX copied with
the name, place, title, tools, dates and link swapped and the bullets
replaced. An entry named like one the resume has adds its bullets to it, and
skills lines become "% inventory:" skills the skills rows may use. Tailoring,
its claim checks and the page fit then work on one resume that has
everything, and what comes out looks like the reference. Whatever combine()
adds carries MARK, so tailoring can tell it from the resume's own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import resume_import as ri
import resume_tex as rt

FILE = "facts.txt"
MARK = rt.FACT_MARK


@dataclass
class Fact:
    section: str
    name: str
    place: str = ""
    title: str = ""
    tools: str = ""
    dates: str = ""
    link: str = ""
    bullets: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class Sheet:
    entries: list[Fact] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)       # extra skills, for the skills rows
    unused: list[str] = field(default_factory=list)       # lines it couldn't place, shown to the person


def _header(section: str, text: str) -> Fact:
    """"Name - Place | Title | Tools, More | Dates | link", every part but the
    name optional and in any order: the dates and the link are told by their
    look, a part with commas is the tools, and the rest is the title."""
    f = Fact(section=section, name="")
    rest = []
    for p in (p.strip() for p in text.split("|")):
        if not p:
            continue
        if not f.dates and ri.is_dates(p):
            f.dates = p
        elif not f.link and " " not in p and "." in p and (re.match(r"https?://", p) or ri.LINK.fullmatch(p)):
            f.link = p if p.startswith("http") else "https://" + p
        else:
            rest.append(p)
    if rest:
        first = rest.pop(0)
        # A job's "Acme - Boston" is its place; a project's "Kanbas -- Course LMS" is part of its name.
        name, *place = [first] if rt._section_kind(section) == "project" else re.split(DASH, first, maxsplit=1)
        f.name, f.place = name.strip(), (place[0] if place else "").strip()
    for p in rest:
        if not f.tools and "," in p:
            f.tools = p
        elif not f.title:
            f.title = p
        elif not f.tools:
            f.tools = p
        else:
            f.title += ", " + p
    return f


def parse(text: str) -> Sheet:
    sheet = Sheet()
    section, cur = "", None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            cur = _header(section, line[3:])
            if cur.name and section:
                sheet.entries.append(cur)
            else:
                sheet.unused.append(line)
                cur = None
        elif line.startswith("# "):
            section, cur = line[2:].strip(), None
        elif line.startswith(("- ", "* ")) and cur:
            cur.bullets.append(line[2:].strip())
        elif ri.TAG_LINE.match(line):
            if cur:
                cur.tags.append("% " + line.lstrip("% ").replace("\\", ""))
            else:
                sheet.skills.append(line.split(":", 1)[1].strip())
        elif cur is None and re.search(r"skill", section, re.I):
            m = ri.SKILL_ROW.match(line)
            sheet.skills.append(m.group(2) if m else line)
        else:
            sheet.unused.append(line)
    for f in sheet.entries:
        if not f.bullets:
            sheet.unused.append(f"## {f.name} (no bullets under it)")
    sheet.entries = [f for f in sheet.entries if f.bullets]
    return sheet


def source(master: Path) -> str:
    """The resume tailoring works on: the master at `master` with the fact
    sheet beside it drawn in. Just the master when there's no fact sheet, or
    when it can't be drawn into this master."""
    tex = master.read_text(encoding="utf-8")
    sheet = master.parent / FILE
    if not sheet.exists():
        return tex
    try:
        return combine(tex, sheet.read_text(encoding="utf-8"))[0]
    except ValueError:
        return tex


def from_sheet(tex: str) -> bool:
    return MARK in tex


# --------------------------------------------------------------------------
# Drawing entries in the resume's own pattern

DASH = r"\s+(?:-{1,3}|–|—)\s+"


def same_name(a: str, b: str) -> bool:
    """"Kanbas", "Kanbas -- Learning Management System" and "Portfolio
    Simulator / Investment Manager" name the entries "Kanbas -- LMS" and
    "Portfolio Simulator": the part before a dash, colon or slash decides."""
    key = lambda s: re.split(r"\s+(?:-{1,3}|–|—|:)\s+|\s*/\s*|:\s", s.strip(), maxsplit=1)[0].casefold().strip()
    return bool(a.strip()) and key(a) == key(b)


def _norm(title: str) -> str:
    return re.sub(r"[^a-z]", "", rt.plain(title).lower())


def _sections(tex: str) -> list[tuple[re.Match, int]]:
    start = tex.find("\\begin{document}")
    end = tex.find("\\end{document}")
    end = end if end >= 0 else len(tex)
    heads = list(rt.SECTION.finditer(tex, start, end))
    return [(h, heads[i + 1].start() if i + 1 < len(heads) else end) for i, h in enumerate(heads)]


def _name_of(res: rt.Resume, e: rt.Entry) -> str:
    rows = rt._rows(rt._entry_header(res, e))
    return rt._name_and_place(rows[0][0])[0] if rows else ""


def _date_in(cell: str) -> str:
    """The dates a header cell ends with, "Feb - Apr 2026" whole."""
    m = ri.DATE_TAIL.search(cell)
    if not m:
        return ""
    whole = re.search(rf"((?:{ri.MONTH}\s*(?:-{{1,3}}|–|—|to)\s*)?{re.escape(m.group(1))})\s*$", cell, re.I)
    return whole.group(1) if whole else m.group(1)


def _url(link: str) -> str:
    return link.replace("%", "\\%").replace("#", "\\#").replace("{", "").replace("}", "")


def _fit_tools(f: Fact, row_has_name: bool, budget: int) -> str:
    """The fact's tools, the first ones kept, so its header line is no longer
    than the longest of its kind on the resume and stays on one line."""
    if not budget or not f.tools:
        return f.tools
    fixed = len(f.title) + (len(f.name) + len(f.place) + len(f.dates) + 6 if row_has_name else 3)
    tools = [t.strip() for t in f.tools.split(",") if t.strip()]
    while len(tools) > 1 and fixed + len(", ".join(tools)) > budget:
        tools.pop()
    return ", ".join(tools)


def _fill_header(head: str, f: Fact, budget: int = 0) -> str | None:
    """The pattern entry's header with the fact's fields in place of its own,
    or None when its fields can't all be found (then the plain layout is used).
    `budget`: the longest line holding a tool list among the resume's entries
    of this kind, in characters."""
    rows = rt._rows(head)
    if not rows:
        return None
    reps: list[tuple[int, int, str]] = []
    bold = re.search(r"\\textbf\{", head)
    if not bold:
        return None
    b_end = rt._brace_end(head, bold.end() - 1)
    name_span = (bold.start(), b_end)
    for hm in re.finditer(r"\\href\{", head):
        url_end = rt._brace_end(head, hm.end() - 1)
        if hm.start() < bold.start() and url_end < len(head) and head[url_end] == "{" \
                and rt._brace_end(head, url_end) >= b_end:
            name_span = (hm.start(), rt._brace_end(head, url_end))
            break
    name = rf"\textbf{{{ri.esc(f.name)}}}"
    reps.append((*name_span, rf"\href{{{_url(f.link)}}}{{{name}}}" if f.link else name))
    after_name = ""

    # The place: " - Dallas, TX" right after the name, up to the next cell or row.
    seg = re.match(r"[^&|\\$\n]*", head[name_span[1]:]).group(0)
    pm = re.match(r"\s*[-–—,]\s*(\S.*?)\s*$", seg)
    name_end = name_span[1] + (len(seg.rstrip()) if pm else 0)    # where text that follows the name goes
    if pm:
        reps.append((name_span[1], name_end, f" - {ri.esc(f.place)}" if f.place else ""))
    elif f.place:
        after_name += f" - {ri.esc(f.place)}"

    # The dates, found by the text the pattern shows for them.
    cells = [rt.plain(c).strip() for row in rows for c in row]
    pdate = next((d for d in (_date_in(c) for c in cells) if d), "")
    if pdate:
        at = head.find(pdate, name_span[1])
        if at < 0:
            return None
        dates = f.dates
        if not re.search(r"--|–|—", pdate):         # the resume writes "Jan 2024 - May 2026"
            dates = re.sub(r"\s*(?:-{1,3}|–|—)\s*", " - ", dates)
        # No dates: the \hfill that pushed them right goes with them (a table's & stays).
        fill = re.search(r"\s*\\hfill\s*$", head[:at]) if not dates else None
        reps.append((fill.start() if fill else at, at + len(pdate), ri.esc(dates)))
    elif f.dates:
        after_name += rf" \hfill {ri.esc(f.dates)}"

    # The tools: the pattern's own \textit{...} tool list.
    tm = rt.TECH.search(head)
    if tm:
        open_ = tm.end() - 1
        close = rt._brace_end(head, open_)
        first_row = not re.search(r"(?<!\\)\\\\", head[:tm.start()])
        tools = _fit_tools(f, first_row, budget)
        if tools:
            reps.append((open_ + 1, close - 1, ri.esc(tools)))
        else:
            sep = re.search(r"\s*(?:\$\|\$|\\textbar\{\}|\|)\s*$", head[:tm.start()])
            reps.append((sep.start() if sep else tm.start(), close, ""))
    elif f.tools:
        after_name += rf" | \textit{{{ri.esc(f.tools)}}}"

    # The title: a second row's text before its tool list ("Full Stack Developer Intern | ...").
    ptitle = rt.plain(rows[1][0].split("|")[0]).strip() if len(rows) > 1 else ""
    if ptitle and not ri.DATE_TAIL.fullmatch(ptitle):
        at = head.find(ptitle, name_span[1])
        if at < 0:
            return None
        if f.title:
            reps.append((at, at + len(ptitle), ri.esc(f.title)))
        else:
            tail = re.match(r"\s*(?:\$\|\$|\|)?\s*", head[at + len(ptitle):]).group(0)
            reps.append((at, at + len(ptitle) + len(tail), ""))
    elif f.title:
        after_name = f", {ri.esc(f.title)}" + after_name
    if after_name:
        reps.append((name_end, name_end, after_name))

    reps.sort(key=lambda r: (r[0], r[1]))
    if any(a[1] > b[0] for a, b in zip(reps, reps[1:])):
        return None
    out = head
    for s, e, t in reversed(reps):
        out = out[:s] + t + out[e:]
    return out


SEP = r"\s*(?:\\textbar\b(?:\{\})?|\$\|\$|\||-{1,3}|–|—|,)\s*"


def _fill_by_values(head: str, f: Fact, kind: str, budget: int = 0) -> str | None:
    """Any header whose fields show as text, a macro's arguments included
    (\\job{\\href{..}{Acme} -- Dallas}{2024 -- 2025}{Engineer \\textbar{} Go}):
    the pattern's own fields are read from its parts, found in its LaTeX and
    swapped for the fact's. A field the fact lacks goes with the separator
    before it; one the pattern has no place for isn't shown, as the resume
    shows none there. None when a field can't be found."""
    cells = ri.header_cells(head)
    if not cells:
        return None
    own = _header("Projects" if kind == "project" else "Work Experience", " | ".join(cells))
    reps: list[tuple[int, int, str]] = []
    # Dates written with the resume's own dash ("Jan 2024 -- May 2026" or "Jan 2024 - May 2026").
    dash = " – " if re.search(r"–|—", own.dates) else " - "
    dates = re.sub(r"\s*(?:-{1,3}|–|—)\s*", dash, f.dates)
    for old, new in [(own.name, f.name), (own.place, f.place), (own.dates, dates), (own.title, f.title),
                     (own.tools, _fit_tools(f, True, budget))]:
        if not old:
            continue
        found = None
        for form in (old, old.replace("—", "---").replace("–", "--"), ri.esc(old)):
            i = head.find(form)
            if i >= 0:
                found = (i, i + len(form))
                break
        if not found:
            return None
        start, end = found
        if not new:
            sep = re.search(SEP + r"$", head[:start])
            start = sep.start() if sep and old != own.name else start
        reps.append((start, end, ri.esc(new)))
    # The pattern's link: the fact's in its place, or none (\href{url}{Acme} -> {Acme}).
    link = re.search(r"\\href\{([^}]*)\}", head)
    if link and not link.group(1).startswith("mailto:"):
        reps.append((link.start(1), link.end(1), _url(f.link)) if f.link else (link.start(), link.end(), ""))
    reps.sort(key=lambda r: (r[0], r[1]))
    if any(a[1] > b[0] for a, b in zip(reps, reps[1:])):
        return None
    out = head
    for s, e, t in reversed(reps):
        out = out[:s] + t + out[e:]
    return out


def _items(items: str, f: Fact) -> str:
    """The pattern's bullet list with the fact's bullets in it."""
    first = re.search(r"\\item\b", items)
    prefix = items[:first.start()] if first else "\\begin{itemize}\n  "
    indent = prefix[prefix.rfind("\n") + 1:] if "\n" in prefix else "  "
    head = prefix[:len(prefix) - len(indent)] if "\n" in prefix else prefix + "\n"
    end = items.rfind("\\end{itemize}")
    close_indent = items[items.rfind("\n", 0, end) + 1:end] if end > 0 else ""
    lines = [indent + t for t in f.tags] + [f"{indent}\\item {ri.esc(b)} {MARK}" for b in f.bullets]
    return head + "\n".join(lines) + "\n" + close_indent + "\\end{itemize}"


def _plain_entry(f: Fact) -> str:
    """An entry in the built-in layout, for a resume whose own pattern can't be followed."""
    head = rf"\textbf{{{ri.esc(f.name)}}}" + (f" - {ri.esc(f.place)}" if f.place else "") \
        + (f", {ri.esc(f.title)}" if f.title else "") + (rf" | \textit{{{ri.esc(f.tools)}}}" if f.tools else "") \
        + (rf" \hfill {ri.esc(f.dates)}" if f.dates else "")
    return head + "\n" + _items("\\begin{itemize}\n  \\item x\n\\end{itemize}", f)


def _budget(res: rt.Resume, kind: str) -> int:
    """The longest header line with a tool list among the resume's entries of `kind`."""
    lines = []
    for e in res.entries:
        if e.kind != kind or not e.tech:
            continue
        for row in rt._rows(rt._entry_header(res, e)):
            if any(rt.TECH.search(c) for c in row):
                lines.append(len(" ".join(rt.plain(c).strip() for c in row)))
    return max(lines, default=0)


# Spacing lines an entry starts with (\vspace{..}, \smallgap): part of its
# header as parsed, but what separates it from the entry before.
LEAD = re.compile(r"(?:[ \t]*(?:\\(?:vspace\*?\{[^}]*\}|[A-Za-z]*(?:gap|skip)\b)|%[^\n]*)[ \t]*\n\s*)*")


def _lead(res: rt.Resume, e: rt.Entry) -> str:
    return LEAD.match(res.tex, e.span.start).group(0)


def _draw(res: rt.Resume, pattern: rt.Entry | None, f: Fact, lead: str = "") -> str:
    """The fact as an entry: the pattern's LaTeX with the fact's fields and
    bullets, after `lead`, the spacing that goes between entries here."""
    if pattern is not None:
        block = res.tex[pattern.span.start + len(_lead(res, pattern)):pattern.span.end]
        cut = block.find("\\begin{itemize}")
        budget = _budget(res, pattern.kind)
        head = _fill_header(block[:cut], f, budget) if re.search(r"\\textbf\{", block[:cut]) else None
        if head is None:
            head = _fill_by_values(block[:cut], f, pattern.kind, budget)
        # Nothing of the pattern's own entry may be left in the copy.
        if head is not None and not (_name_of(res, pattern) and _name_of(res, pattern) in rt.plain(head)
                                     and _name_of(res, pattern) != f.name):
            return f"{MARK}\n{lead}{head.rstrip()}\n{_items(block[cut:], f)}"
    return f"{MARK}\n{lead}{_plain_entry(f)}"


def combine(tex: str, text: str, min_bullets: int = rt.MIN_BULLETS) -> tuple[str, dict]:
    """`tex` with the fact sheet `text` drawn in, and what was added:
    {"entries": [names], "bullets": {entry name: count}, "skills": n, "unused": [lines],
    "short": [new entries with fewer than `min_bullets`, which tailoring leaves out]}."""
    sheet = parse(text)
    report = {"entries": [], "bullets": {}, "skills": len(sheet.skills), "unused": list(sheet.unused), "short": []}
    if not sheet.entries and not sheet.skills:
        return tex, report
    res = rt.parse(tex)
    if sheet.entries and not res.entries:
        raise ValueError("Your resume has no entries with bullets to copy the layout from.")
    sections = _sections(tex)
    in_section = lambda e, h, stop: h.end() <= e.span.start < stop
    upper = bool(sections) and all(rt.plain(h.group(1)) == rt.plain(h.group(1)).upper() for h, _ in sections)
    inserts: dict[int, list[str]] = {}
    added: dict[str, list[str]] = {}            # new sections: title -> entries

    def section_for(title: str):
        """The resume's own section for an entry: the one of that name, else its
        job section for jobs and its projects section for everything else
        (coursework, research, anything with bullets), so the resume keeps its
        sections. None only when it has no such section."""
        n = _norm(title)
        for h, stop in sections:
            m = _norm(h.group(1))
            if n and m and (n == m or n in m or m in n) and any(in_section(e, h, stop) for e in res.entries):
                return h, stop
        want = "experience" if rt._section_kind(title) == "experience" else "project"
        return next(((h, stop) for h, stop in sections if rt._section_kind(h.group(1)) == want
                     and any(in_section(e, h, stop) for e in res.entries)), None)

    def pattern_for(kind: str, within=None) -> rt.Entry | None:
        if within:
            own = [e for e in res.entries if in_section(e, *within)]
            if own:
                return own[0]
        same = [e for e in res.entries if e.kind == kind] or [e for e in res.entries if e.kind == "project"] or res.entries
        return same[0] if same else None

    def between(e: rt.Entry) -> str:
        """The spacing between entries in e's section: its second entry's, else e's own."""
        sec = next(((h, stop) for h, stop in sections if in_section(e, h, stop)), None)
        mine = sorted((x for x in res.entries if sec and in_section(x, *sec)), key=lambda x: x.span.start)
        return _lead(res, mine[1] if len(mine) > 1 else e)

    for f in sheet.entries:
        sec = section_for(f.section)
        if sec:
            mine = sorted((e for e in res.entries if in_section(e, *sec)), key=lambda e: e.span.start)
            hit = next((e for e in mine if same_name(_name_of(res, e), f.name)), None)
            if hit:
                block = tex[hit.span.start:hit.span.end]
                end = hit.span.start + block.rfind("\\end{itemize}")
                line_start = tex.rfind("\n", hit.span.start, end) + 1
                indent = re.match(r"[ \t]*", tex[line_start:end]).group(0) or "  "
                inner = re.search(r"\n([ \t]*)\\item\b", block)
                item_indent = inner.group(1) if inner else indent + "  "
                lines = "".join(f"{item_indent}{t}\n" for t in f.tags) \
                    + "".join(f"{item_indent}\\item {ri.esc(b)} {MARK}\n" for b in f.bullets)
                inserts.setdefault(line_start, []).append(lines)
                report["bullets"][_name_of(res, hit)] = report["bullets"].get(_name_of(res, hit), 0) + len(f.bullets)
                continue
        # Drawn in all the same: tailoring leaves it out (tailor.bullet_limits).
        if len(f.bullets) < min_bullets:
            report["short"].append(f.name)
        if sec:
            pattern = pattern_for(rt._section_kind(f.section), sec)
            if mine:
                inserts.setdefault(mine[-1].span.end, []).append("\n" + _draw(res, pattern, f, between(mine[0])))
            else:
                inserts.setdefault(sec[0].end(), []).append("\n" + _draw(res, pattern, f, _lead(res, pattern) if pattern else ""))
        else:
            # A resume with no section for it: one new Experience or Projects section,
            # its first entry spaced from the heading as the pattern's is from its own,
            # the rest as entries are from each other.
            kind = "experience" if rt._section_kind(f.section) == "experience" else "project"
            title = "Experience" if kind == "experience" else "Projects"
            pattern = pattern_for(kind)
            lead = "" if not pattern else _lead(res, pattern) if title not in added else between(pattern)
            added.setdefault(title, []).append(_draw(res, pattern, f, lead))
        report["entries"].append(f.name)

    end = tex.find("\\end{document}")
    end = end if end >= 0 else len(tex)
    if added:
        heading = sections[0][0].group(0) if sections else r"\section*{X}"
        title_at = heading.index("{") + 1
        blocks = []
        for title, entries in added.items():
            name = ri.esc(title.upper() if upper else title)
            blocks.append("\n" + heading[:title_at] + name + heading[heading.rindex("}"):] + "\n" + "\n".join(entries) + "\n")
        inserts.setdefault(end, []).append("".join(blocks))
    if sheet.skills:
        skills = next(((h, stop) for h, stop in sections if re.search(r"skill", h.group(1), re.I)), None)
        tag = "\n% inventory: " + ", ".join(s.replace("\n", " ") for s in sheet.skills) + "\n"
        inserts.setdefault(skills[0].end() if skills else end, []).append(tag)

    out, last = [], 0
    for pos in sorted(inserts):
        out.append(tex[last:pos])
        out.extend(inserts[pos])
        last = pos
    out.append(tex[last:])
    return "".join(out), report


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+#.]+", text.casefold()) if len(w) > 2}


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _alike(a: str, b: str) -> bool:
    """Two bullets about the same thing: much of the shorter one's words are in
    the other, or they state the same results (two or more of the same numbers:
    "within ~14-15 cycles" and "approximately 14-15-cycle error")."""
    x, y = _words(a), _words(b)
    return (bool(x and y) and len(x & y) / min(len(x), len(y)) >= 0.4) or len(_numbers(a) & _numbers(b)) >= 2


def extras(tex: str, outline: str) -> str:
    """The part of a longer resume's outline that `tex` doesn't have: entries
    it lacks whole, bullets it lacks under entries it has, and skills it
    doesn't list. For filling a fact sheet from an old, longer resume; the
    header and education stay out, since the resume has its own."""
    res = rt.parse(tex)
    have = [(_name_of(res, e), {rt.plain(b.tex).strip().casefold() for b in e.bullets}) for e in res.entries]
    known = rt.plain(tex)
    listed = lambda skill: re.search(rf"(?<![\w+#]){re.escape(skill)}(?![\w+#])", known, re.I)
    out: list[str] = []
    section, block = "", []          # block: the current section's lines worth keeping

    def flush():
        if block:
            out.extend([section, *block])
        block.clear()

    entry, entry_lines = None, []
    for raw in (outline or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# "):
            block.extend(entry_lines)
            entry, entry_lines = None, []
            flush()
            section = "" if re.search(r"educat", line, re.I) else line
            continue
        if not section:
            continue
        if line.startswith("## "):
            block.extend(entry_lines)
            name = _header(section[2:], line[3:]).name
            entry, entry_lines = next((b for n, b in have if same_name(n, name)), None), [line]
            continue
        if line.startswith(("- ", "* ")) and entry_lines:
            if entry is None or not any(_alike(line[2:], b) for b in entry):
                entry_lines.append(line)
            continue
        if ri.TAG_LINE.match(line) and entry_lines:
            entry_lines.append(line)
            continue
        if re.search(r"skill", section, re.I):
            m = ri.SKILL_ROW.match(line)
            items = [x.strip() for x in (m.group(2) if m else line).split(",") if x.strip()]
            new = [x for x in items if not listed(x)]
            if new:
                block.append((m.group(1) + ": " if m else "") + ", ".join(new))
    block.extend(entry_lines)
    flush()
    # An entry the resume has, with nothing new under it, isn't worth a line.
    kept = []
    for i, line in enumerate(out):
        if line.startswith("## ") and (i + 1 >= len(out) or not out[i + 1].startswith(("- ", "* ", "%"))):
            continue
        kept.append(line)
    kept = [l for i, l in enumerate(kept) if not (l.startswith("# ") and (i + 1 >= len(kept) or kept[i + 1].startswith("# ")))]
    return "\n".join(kept) + ("\n" if kept else "")
