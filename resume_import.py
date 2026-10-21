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

READERS maps file types to how their text is read. PDF and text are read in
the dashboard; Word files (.docx) are the next to add: read the document's
paragraphs (and list items, as "- " lines) into text, then call
text_to_outline() like the others.
"""

from __future__ import annotations

import re

READERS = {
    "pdf": "browser",       # pdf.js in the dashboard, then text_to_outline
    "txt": "browser",
    "md": "browser",
    "tex": "as-is",         # already a master; tailored in place
    "docx": None,           # not yet: see the module docstring
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
    text = re.sub(r" \\\\\n(?=\s*(?:\\begin\{itemize\}|\n|\\section))", "\n", text)
    return PREAMBLE + "\n" + "\n".join(t for t in top if t) + "\n" + text.strip() + "\n\\end{document}\n"
