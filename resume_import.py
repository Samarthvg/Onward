"""Turns a resume that isn't LaTeX into a .tex master that tailoring can edit.

Tailoring edits a .tex resume in place (resume_tex.py). A PDF or text resume
has no source to edit, so it goes through two steps the user can check:

1. text_to_outline(): the resume's text (read from the PDF in the browser)
   becomes a plain outline the user can correct in a text box:

       Jane Doe
       jane@example.com | 555-0100 | linkedin.com/in/jane
       # Experience
       ## Software Engineer Intern | Acme | May 2024 - Aug 2024
       - Built the thing
       # Skills
       Languages: Python, Go

2. outline_to_tex(): the outline becomes a .tex in one plain built-in layout,
   using only packages every TeX distribution and Tectonic have.

READERS maps file types to how their text is read. PDF, Word (.docx) and
text are read in the dashboard (Word's list items as "- " lines), then go
through text_to_outline() alike. The outline the person checked is kept
beside the master (server.outline_path) as their full resume, edited under
Settings and made into the .tex again on each save.
"""

from __future__ import annotations

import re

import resume_tex as rt

READERS = {
    "pdf": "browser",       # pdf.js in the dashboard, then text_to_outline
    "txt": "browser",
    "md": "browser",
    "tex": "as-is",         # already a master; tailored in place
    "docx": "browser",      # word/document.xml's paragraphs, in the dashboard
}

HEADINGS = re.compile(
    r"^(?:(?:work|professional|relevant|technical|research|industry)\s+)?"
    r"(?:experience|employment(?: history)?|work history|projects?|personal projects|academic projects|education|"
    r"skills?|technical skills|core competencies|summary|profile|objective|leadership(?: experience)?|activities|"
    r"extracurricular(?: activities)?|involvement|certifications?|awards?|honors?(?: (?:and|&) awards)?|"
    r"publications?|volunteer(?:ing| experience)?|interests|coursework|relevant coursework|languages|"
    r"achievements|open source)$", re.I)
BULLET = re.compile(r"^\s*(?:[•●▪◦‣⁃∙·■*]|[-–—](?=\s))\s*")
MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE = re.compile(rf"(?:\b{MONTH}\s+)?\b(?:19|20)\d\d\b|\bpresent\b|\bcurrent\b|\b\d{{1,2}}/(?:19|20)?\d\d\b", re.I)
SKILL_ROW = re.compile(r"^([A-Za-z][A-Za-z /&+.-]{1,40}):\s*(.+)$")


def _clean(line: str) -> str:
    return re.sub(r"\s+", " ", line.replace(" ", " ")).strip()


def _kind(title: str) -> str:
    t = title.lower()
    if "skill" in t or "competenc" in t:
        return "skills"
    if "project" in t or "open source" in t:
        return "projects"
    if re.search(r"experience|employment|work|leadership|volunteer|research", t):
        return "experience"
    return "other"


def text_to_outline(text: str) -> str:
    """Best-effort outline of a resume's text. Section headings are the
    usual ones on a line of their own; bullet glyphs start bullets; wrapped
    bullet lines join the bullet above."""
    lines = [_clean(l) for l in (text or "").splitlines()]
    lines = [l for l in lines if l]
    out: list[str] = []
    section = ""
    in_bullet = False
    for i, line in enumerate(lines):
        bare = line.strip(" :").rstrip(":")
        if len(bare) <= 40 and HEADINGS.match(bare):
            section = _kind(bare)
            out.append("# " + bare.title() if bare.isupper() else "# " + bare)
            in_bullet = False
            continue
        if not out or not any(l.startswith("# ") for l in out):
            out.append(line)                            # name and contact lines above the first section
            continue
        m = BULLET.match(line)
        if m:
            out.append("- " + line[m.end():])
            in_bullet = True
            continue
        if section == "skills":
            out.append(line)
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        header_like = bool(DATE.search(line)) or (BULLET.match(nxt) is not None and not in_bullet) \
            or (in_bullet and len(line) < 70 and bool(DATE.search(nxt)))
        if in_bullet and not header_like:
            out[-1] += " " + line                       # a bullet wrapped onto the next line
            continue
        in_bullet = False
        body, date = split_date(line)
        if section in ("experience", "projects"):
            if out[-1].startswith("## "):
                # The entry's second line ("Acme Corp  Remote" under "Intern  May 2024 - Aug 2024"): one header.
                prev, prev_date = split_date(out[-1][3:])
                date = prev_date or date
                body = prev + " | " + body
                out.pop()
            out.append("## " + body + (" | " + date if date else ""))
        else:
            out.append(body + (" | " + date if date else ""))
    return "\n".join(out) + "\n"


_SPAN = rf"(?:{MONTH}\s+)?(?:19|20)\d\d|present|current|now"
DATE_TAIL = re.compile(rf"\s*[|,]?\s*((?:expected\s+)?(?:{_SPAN})(?:\s*(?:-|–|—|to)\s*(?:{_SPAN}))?)\s*$", re.I)


def is_dates(text: str) -> bool:
    """A part that's only dates: "May 2024 - Aug 2024", "2024", "Feb - Apr 2026", "Spring 2025"."""
    return bool(DATE_TAIL.fullmatch(text) or re.fullmatch(
        rf"\s*(?:{MONTH}\s*(?:-{{1,3}}|–|—|to)\s*{MONTH}|(?:spring|summer|fall|autumn|winter))\s+(?:19|20)\d\d\s*",
        text, re.I))


def split_date(line: str) -> tuple[str, str]:
    """("Software Engineer Intern", "May 2024 - Aug 2024") from a line that ends in dates."""
    m = DATE_TAIL.search(line)
    if not m or m.start() == 0:
        return line.strip(), ""
    return line[:m.start()].strip(" |,"), m.group(1)


# --------------------------------------------------------------------------
# Outline -> LaTeX

SPECIALS = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
            "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
TYPOGRAPHY = {"“": "``", "”": "''", "‘": "`", "’": "'", "–": "--", "—": "---",
              "…": "...", " ": " ", "•": "", "●": "", "™": "", "®": ""}
LINK = re.compile(r"(?:https?://)?(?:www\.)?(?:[\w-]+\.)+[a-z]{2,}(?:/[\w./%-]*)?", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def esc(text: str) -> str:
    out = "".join(SPECIALS.get(c, TYPOGRAPHY.get(c, c)) for c in text)
    # Characters a plain pdflatex font can't print are dropped, not left to fail the compile.
    return "".join(c for c in out if ord(c) < 0x250 or c in "‘’")


def _contact(part: str) -> str:
    part = part.strip()
    if EMAIL.fullmatch(part):
        return rf"\href{{mailto:{part}}}{{{esc(part)}}}"
    if LINK.fullmatch(part) and "." in part:
        url = (part if part.startswith("http") else "https://" + part).replace("%", "\\%").replace("#", "\\#")
        return rf"\href{{{url}}}{{{esc(part)}}}"
    return esc(part)


PREAMBLE = r"""\documentclass[11pt,letterpaper]{article}
% Made by Onward from a PDF or text resume. Edit freely: tailoring
% changes only the bullets, the "| \textit{...}" tool lists and the skills rows.
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage[margin=0.6in]{geometry}
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage[hidelinks]{hyperref}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\titleformat{\section}{\large\bfseries}{}{0pt}{}[\vspace{-6pt}\rule{\linewidth}{0.4pt}]
\titlespacing*{\section}{0pt}{8pt}{4pt}
\setlist[itemize]{leftmargin=1.2em, itemsep=1pt, topsep=2pt, parsep=0pt}
"""


def _entry_header(text: str, section: str) -> str:
    """"Title | Company | May 2024 - Aug 2024" -> bold title, the rest, the
    dates flush right. A project's last part, when it lists tools
    ("Python, React"), becomes its "| \\textit{...}" tool list."""
    parts = [p.strip() for p in text.split("|") if p.strip()]
    if not parts:
        return ""
    date = parts.pop() if len(parts) > 1 and DATE.search(parts[-1]) else ""
    tech = parts.pop() if section == "projects" and len(parts) > 1 and "," in parts[-1] else ""
    head = rf"\textbf{{{esc(parts[0])}}}" + "".join(", " + esc(p) for p in parts[1:])
    return head + (rf" | \textit{{{esc(tech)}}}" if tech else "") + (rf" \hfill {esc(date)}" if date else "")


TAG_LINE = re.compile(r"^%\s*(?:skills|inventory)\s*:", re.I)
BAR = re.compile(r"\s*(?:\\textbar\b(?:\{\})?|\$\|\$)\s*")


def header_cells(header: str) -> list[str]:
    """An entry header's parts as plain text: a table's cells, or a macro's
    arguments when the header is one call such as
    \\job{Acme -- Boston}{2024 -- 2025}{Engineer \\textbar{} Go, SQL}."""
    h = rt.strip_comments(header)
    # The last call with two or more arguments is the entry's own header line;
    # spacing macros and a section's intro may come before it.
    call = []
    for m in re.finditer(r"\\[A-Za-z]+\*?\s*(?=\{)", h):
        args, at = [], m.end()
        while at < len(h) and h[at] == "{":
            end = rt._brace_end(h, at)
            args.append(h[at + 1:end - 1])
            at = end
            while at < len(h) and h[at] in " \t\r\n":
                at += 1
        if len(args) >= 2 and not rt.plain(h[at:]).strip():
            call = args
    raw = call or [c for row in rt._rows(header) for c in row]
    cells = [_clean(rt.plain(BAR.sub(" | ", c)).replace("---", "—").replace("--", "–")) for c in raw]
    return [c for c in cells if c]


def tex_to_outline(tex: str) -> str:
    """A .tex master as an outline, so it can be kept and edited as the full
    resume like one made from a PDF: every section, entry, bullet, skills row
    and hidden skill tag comes across. The .tex layout doesn't; outline_to_tex
    makes it again in the built-in one."""
    res = rt.parse(tex)
    start = tex.index("\\begin{document}") + len("\\begin{document}")
    end = tex.find("\\end{document}")
    end = end if end >= 0 else len(tex)
    heads = list(rt.SECTION.finditer(tex, start, end))

    def lines_of(chunk: str) -> list[str]:
        tags = ["% " + m.group(0).lstrip("% ") for m in re.finditer(r"(?<!\\)%\s*(?:skills|inventory)\s*:.*", chunk, re.I)]
        # A line per LaTeX line break, paragraph and table (education often sits in
        # tables); a table's width and column spec ({\textwidth}{@{}l r@{}}) aren't text.
        chunk = re.sub(r"\\begin\{tabular\*?\}(?:\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\})*|\\end\{tabular\*?\}",
                       "\n\n", rt.strip_comments(chunk))
        parts = re.split(r"(?<!\\)\\\\(?:\[[^\]]*\])?|\n\s*\n", chunk)
        return [l for l in (_clean(rt.plain(p)) for p in parts) if l] + tags

    out = lines_of(tex[start:heads[0].start() if heads else end])     # name and contact lines
    for i, h in enumerate(heads):
        stop = heads[i + 1].start() if i + 1 < len(heads) else end
        out.append("# " + _clean(rt.plain(h.group(1))))
        rows = [r for r in res.skill_rows if h.end() <= r.span.start < stop]
        if rows:
            out += [f"{r.label}: {_clean(rt.plain(r.tex))}" for r in rows]
            continue
        at = h.end()
        for e in sorted((e for e in res.entries if h.end() <= e.span.start < stop), key=lambda e: e.span.start):
            out += lines_of(tex[at:e.span.start])
            header = rt._entry_header(res, e)
            cells = header_cells(header)
            # Dates after the rest, where outline_to_tex puts them flush right, then the link.
            dated = [c for c in cells if is_dates(c)]
            link = re.search(r"\\href\{([^}]*)\}", header)
            link = [link.group(1)] if link and not link.group(1).startswith("mailto:") else []
            out.append("## " + " | ".join([c for c in cells if c not in dated] + dated + link))
            block = tex[e.span.start:e.span.end]
            out += [l for l in lines_of(block[:block.find("\\begin{itemize}")]) if l.startswith("% ")]
            out += ["- " + _clean(rt.plain(b.tex)) for b in e.bullets]
            out += [l for l in lines_of(block[block.find("\\begin{itemize}"):]) if l.startswith("% ")]
            at = e.span.end
        out += lines_of(tex[at:stop])
    return "\n".join(out) + "\n"


def outline_to_tex(outline: str) -> str:
    """A .tex resume in the built-in layout. resume_tex.parse() reads it:
    each entry's bullets are one itemize block, skills rows are
    \\textbf{Label:} rows in a section whose title mentions skills."""
    lines = [l.rstrip() for l in (outline or "").splitlines()]
    head: list[str] = []
    body: list[str] = []
    section = ""
    in_list = False

    def close_list():
        nonlocal in_list
        if in_list:
            body.append(r"\end{itemize}")
            in_list = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# "):
            close_list()
            title = line[2:].strip()
            section = _kind(title)
            body.append("")
            body.append(rf"\section*{{{esc(title)}}}")
            continue
        if not section:
            head.append(line)
            continue
        if TAG_LINE.match(line):
            # Hidden skills ("% skills: Docker"): kept as a comment in the entry, so they
            # count for matching without printing. Not a line break of their own.
            body.append("% " + line.lstrip("% ").replace("\\", ""))
            continue
        if line.startswith(("- ", "* ")):
            if not in_list:
                body.append(r"\begin{itemize}")
                in_list = True
            body.append(r"  \item " + esc(line[2:].strip()))
            continue
        close_list()
        if line.startswith("## "):
            body.append(_entry_header(line[3:], section) + r" \\")
            continue
        m = SKILL_ROW.match(line) if section == "skills" else None
        if m:
            body.append(rf"\textbf{{{esc(m.group(1))}:}} {esc(m.group(2))} \\")
        else:
            text_, date = split_date(line)
            body.append(esc(text_.replace(" | ", ", ")) + (rf" \hfill {esc(date)}" if date else "") + r" \\")
    close_list()
    if not any(l.startswith(r"\begin{itemize}") for l in body):
        raise ValueError("No bullet points found. Start each bullet with \"- \" under an entry, so tailoring has "
                         "something to work with.")

    name = esc(head[0]) if head else ""
    contact = [c for l in head[1:] for c in re.split(r"\s*[|•·]\s*|\s{3,}", l) if c.strip()]
    top = [r"\begin{document}", r"\begin{center}", rf"{{\LARGE\bfseries {name}}}\\[2pt]" if name else ""]
    if contact:
        top.append(r" \textbar{} ".join(_contact(c) for c in contact))
    top.append(r"\end{center}")
    # A "\\" right before a list or section has nothing to end; drop those.
    text = "\n".join(body)
    text = re.sub(r" \\\\\n(?=\s*(?:%[^\n]*\n\s*)*(?:\\begin\{itemize\}|\n|\\section))", "\n", text)
    return PREAMBLE + "\n" + "\n".join(t for t in top if t) + "\n" + text.strip() + "\n\\end{document}\n"
