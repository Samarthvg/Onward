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
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import texengine

ROOT = Path(__file__).resolve().parent
SKILL_BOUNDARY = "(?<![A-Za-z0-9+#])(?:{})(?![A-Za-z0-9+#])"
_TAXONOMY = json.loads((ROOT / "skills.json").read_text(encoding="utf-8"))
SKILLS = [
    (s["n"], re.compile(SKILL_BOUNDARY.format(s["re"]), 0 if s.get("cs") else re.I))
    for s in _TAXONOMY
]
SKILL_NAMES = {name.lower(): name for name, _ in SKILLS}
IMPLIES = {s["n"]: set(s.get("implies", [])) for s in _TAXONOMY}


def find_skills(text: str) -> set[str]:
    return {name for name, rx in SKILLS if rx.search(text)} if text else set()


def with_implied(skills: set[str]) -> set[str]:
    """Concrete tools imply the umbrella skill (React -> Frontend)."""
    out = set(skills)
    for k in skills:
        out |= IMPLIES.get(k, set())
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
    res.vocabulary = {w.lower() for w in re.findall(r"[A-Za-z0-9.+#-]+", everything + " " + tags)}
    if not res.entries:
        raise ValueError("No itemize blocks found. Bullets need to be inside \\begin{itemize} ... \\end{itemize}.")
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
    jd_words = {w.lower() for w in re.findall(r"[A-Za-z0-9.+#-]+", jd)}
    for tok in TECHY.findall(new_p):
        t = tok.lower()
        if t not in vocabulary and t in jd_words and t not in old_p.lower():
            return f"adds \"{tok}\" from the job description, which appears nowhere in your master resume"
    return None


UMBRELLA = {"frontend", "backend", "full stack", "machine learning", "deep learning", "system design",
            "security", "oop", "data structures & algorithms", "testing", "agile", "llms", "nlp"}


def _items(text: str) -> list[str]:
    """Top-level comma-separated items of a tool list (commas inside
    parentheses, e.g. "CI/CD (GitHub Actions, Jenkins)", don't split)."""
    out, depth, cur = [], 0, ""
    for ch in plain(text):
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


def _norm_item(s: str) -> str:
    s = re.sub(r"\.?js\b", "", s.lower())          # React.js == React
    return re.sub(r"[^a-z0-9+#&/]", "", s)


def check_list_edit(old: str, new: str) -> str | None:
    """Tool lists may be reordered and extended, never shortened or genericized."""
    old_items, new_items = _items(old), _items(new)
    have = {_norm_item(i) for i in new_items}
    lost = [i for i in old_items if _norm_item(i) not in have]
    if lost:
        return f"drops {', '.join(lost)} from the list"
    before = {_norm_item(i) for i in old_items}
    generic = [i for i in new_items if _norm_item(i) not in before and i.lower() in UMBRELLA]
    if generic:
        return f"adds a category name ({', '.join(generic)}) instead of a tool"
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


def plan_edits(res: Resume, out: dict, jd: str) -> tuple[list[tuple[Span, str]], list[Change], dict]:
    """Turn model output into concrete span replacements plus a change log.
    Anything that fails validation is logged as rejected and left unchanged."""
    changes: list[Change] = []
    rewrites: dict[str, str] = {}

    for item in out.get("bullet_rewrites", []):
        found = res.bullet(item.get("id", ""))
        if not found:
            continue
        entry, b = found
        new = sanitize(item.get("text", ""))
        if not new or plain(new) == plain(b.tex):
            continue
        reason = (check_latex(new)
                  or (len(plain(new)) > len(plain(b.tex)) * 1.3 + 40 and "makes the bullet much longer")
                  or check_honesty(b.tex, new, entry.allowed, res.vocabulary, jd))
        if reason:
            changes.append(Change("bullet", b.id, entry.title, plain(b.tex), plain(new), "rejected", reason))
        else:
            rewrites[b.id] = new
            changes.append(Change("bullet", b.id, entry.title, plain(b.tex), plain(new)))

    tech_new: dict[str, str] = {}
    by_id = {e.id: e for e in res.entries}
    for item in out.get("tech_lines", []):
        e = by_id.get(item.get("id", ""))
        if not e or e.tech is None:
            continue
        new = sanitize(item.get("text", ""))
        if not new or plain(new) == plain(e.tech_tex):
            continue
        reason = (check_latex(new) or check_list_edit(e.tech_tex, new)
                  or check_honesty(e.tech_tex, new, e.allowed, res.vocabulary, jd))
        if reason:
            changes.append(Change("tech", e.id, e.title, plain(e.tech_tex), plain(new), "rejected", reason))
        else:
            tech_new[e.id] = new
            changes.append(Change("tech", e.id, e.title, plain(e.tech_tex), plain(new)))

    rows_new: dict[str, str] = {}
    rows = {r.id: r for r in res.skill_rows}
    for item in out.get("skills_rows", []):
        r = rows.get(item.get("id", ""))
        if not r:
            continue
        new = sanitize(item.get("text", ""))
        if not new or plain(new) == plain(r.tex):
            continue
        reason = (check_latex(new) or check_list_edit(r.tex, new)
                  or check_honesty(r.tex, new, res.inventory, res.vocabulary, jd))
        if reason:
            changes.append(Change("skills", r.id, r.label, plain(r.tex), plain(new), "rejected", reason))
        else:
            rows_new[r.id] = new
            changes.append(Change("skills", r.id, r.label, plain(r.tex), plain(new)))

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
             "suggest_entries": suggest_entries, "suggest_bullets": suggest_bullets}
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

    for rid, new in state["rows"].items():
        row = next(r for r in res.skill_rows if r.id == rid)
        spans.append((row.span, new))
    return spans


def _entry_spans(res: Resume, e: Entry, state: dict, changes: list[Change] | None) -> list[tuple[Span, str]]:
    """Edits inside one kept entry: tech line, bullet rewrites, order and trims."""
    spans: list[tuple[Span, str]] = []
    rank = state["rank"]
    if e.id in state["tech"]:
        spans.append((e.tech, state["tech"][e.id]))
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
# Compilation

class CompileError(Exception):
    pass


def engine_for(tex: str) -> str:
    return "xelatex" if re.search(r"\\usepackage(\[[^\]]*\])?\{fontspec\}", tex) else "pdflatex"


def compile_pdf(tex: str, out_pdf: Path | None = None, timeout: int = 120) -> int:
    """Compile and return the page count. Shell escape is always off. An
    installed TeX is used when there is one, else Tectonic (texengine.py)."""
    engine = engine_for(tex)
    found = texengine.pick(engine)
    if not found:
        raise CompileError(f"{engine} is not installed. Install a TeX distribution, or let It gets easier "
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
        return int(m.group(1))
