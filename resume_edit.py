"""Edits made by hand to a tailored resume.

A tailored version's pieces (its bullets, the tool line after an entry's
name, and the skills rows) are offered as text to edit: **bold**, *italic*
and the characters LaTeX escapes (R&D, 40%) typed as they read. A piece
whose LaTeX doesn't come back the same from that text (a link, a macro) is
offered as its LaTeX instead, so saving never changes what wasn't touched.

Saving applies the edits to that version's .tex, which stays as it was, and
makes a new version from the result: everything outside the edited pieces,
layout included, is the version's own text.

Edits, by piece id from parse():
    {"bullets": {"B3": text}, "remove": ["B4"], "add": {"E1": [text, ...]},
     "tech": {"E2": text}, "skills": {"S1": text}}
Each text is {"text": str, "latex": bool}: as edited, or LaTeX as is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import resume_tex as rt

MAX_TEXT = 1200          # characters in one piece
MAX_ADDED = 10           # new bullets in one entry

ESCAPED = "&%$#_"
BOLD = re.compile(r"\\textbf\{([^{}\\]*)\}")
ITALIC = re.compile(r"\\textit\{([^{}\\]*)\}")


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def to_text(tex: str) -> str:
    """LaTeX to the text offered for editing."""
    s = _squash(tex)
    s = BOLD.sub(lambda m: f"**{m.group(1)}**", s)
    s = ITALIC.sub(lambda m: f"*{m.group(1)}*", s)
    s = re.sub(r"\\([&%$#_])", r"\1", s)
    return s.replace("---", "\u2014").replace("--", "\u2013")


def from_text(text: str) -> str:
    """The edited text back to LaTeX."""
    s = _squash(text).replace("\u2014", "---").replace("\u2013", "--")
    s = re.sub(r"(?<!\\)([&%$#_])", r"\\\1", s)
    s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", lambda m: "\\textbf{" + m.group(1) + "}", s)
    return re.sub(r"(?<![*\w])\*(?=\S)([^*]+?)(?<=\S)\*(?![*\w])", lambda m: "\\textit{" + m.group(1) + "}", s)


def editable(tex: str) -> dict:
    """{"text", "latex"}: the piece as text, or as its LaTeX when the text
    wouldn't turn back into the same LaTeX."""
    text = to_text(tex)
    if "\\" in text or "{" in text or "}" in text or "~" in text or from_text(text) != _squash(tex):
        return {"text": _squash(tex), "latex": True}
    return {"text": text, "latex": False}


def entry_name(res: rt.Resume, e: rt.Entry) -> str:
    """"Example Labs · Software Engineer Intern": the entry by its name and
    role, read from its header as resume history does."""
    header = rt._entry_header(res, e)
    if not rt.plain(header).strip():
        return e.section.title() if e.section.isupper() else e.section   # a list with no header of its own
    parts, _ = rt.header_parts(header)
    f = rt.header_fields(parts, e.kind)
    return " · ".join(x for x in (f["name"], f["title"]) if x) or e.title.split(" | ")[0]


def pieces(tex: str) -> dict:
    """What the editor shows of one version: its entries in order, each with
    its tool line and bullets, and its skills rows."""
    res = rt.parse(tex)
    return {
        "entries": [{"id": e.id, "title": entry_name(res, e), "section": e.section, "kind": e.kind,
                     "tech": editable(e.tech_tex) if e.tech else None,
                     "bullets": [{"id": b.id, **editable(b.tex)} for b in e.bullets]}
                    for e in res.entries],
        "skills": [{"id": r.id, "label": r.label, **editable(r.tex)} for r in res.skill_rows],
    }


@dataclass
class Edit:
    kind: str           # "bullet", "add", "remove", "tech" or "skills"
    target: str         # the piece's id (an entry's for "add")
    label: str          # the entry's title, or the skills row's label
    before: str
    after: str


class EditError(ValueError):
    pass


def _latex(piece, where: str) -> str:
    if not isinstance(piece, dict) or not isinstance(piece.get("text"), str):
        raise EditError(f"{where}: expected text")
    text = piece["text"]
    if len(text) > MAX_TEXT:
        raise EditError(f"{where} is longer than {MAX_TEXT} characters.")
    out = _squash(text) if piece.get("latex") else from_text(text)
    if rt.check_latex(out) == "unbalanced braces":
        raise EditError(f"{where} has a {{ without its }} (or the other way round).")
    return out


def _line_span(tex: str, start: int, end: int) -> rt.Span:
    """A removed bullet's whole line, when nothing else is on it."""
    a = start
    while a > 0 and tex[a - 1] in " \t":
        a -= 1
    b = end
    nl = tex.find("\n", b)
    rest = tex[b:nl if nl >= 0 else len(tex)]
    if (a == 0 or tex[a - 1] == "\n") and (not rest.strip() or rest.strip().startswith(rt.FACT_MARK)):
        return rt.Span(a, nl + 1 if nl >= 0 else len(tex))
    return rt.Span(start, end)


def apply(tex: str, edits: dict) -> tuple[str, list[Edit]]:
    """The version's .tex with the edits made, and what each changed. Text
    the same as before isn't an edit."""
    if not isinstance(edits, dict):
        raise EditError("Expected the edits as an object.")
    res = rt.parse(tex)
    entries = {e.id: e for e in res.entries}
    rows = {r.id: r for r in res.skill_rows}
    names = {e.id: entry_name(res, e) for e in res.entries}

    def section(name: str) -> dict:
        got = edits.get(name) or {}
        if not isinstance(got, dict):
            raise EditError(f"{name}: expected an object")
        return got

    spans: list[tuple[rt.Span, str]] = []
    log: list[Edit] = []
    remove = edits.get("remove") or []
    if not isinstance(remove, list):
        raise EditError("remove: expected a list")
    removed = set()
    for bid in remove:
        found = res.bullet(str(bid))
        if not found:
            raise EditError(f"There's no bullet {bid} in this version.")
        e, b = found
        removed.add(b.id)
        spans.append((_line_span(tex, b.whole.start, b.whole.end), ""))
        log.append(Edit("remove", b.id, names[e.id], to_text(b.tex), ""))

    for bid, piece in section("bullets").items():
        found = res.bullet(str(bid))
        if not found:
            raise EditError(f"There's no bullet {bid} in this version.")
        e, b = found
        if b.id in removed:
            continue
        new = _latex(piece, f"A bullet in {names[e.id]}")
        if not new:
            raise EditError(f"A bullet in {names[e.id]} is empty. Remove it instead.")
        if new != _squash(b.tex):
            spans.append((b.span, new))
            log.append(Edit("bullet", b.id, names[e.id], to_text(b.tex), to_text(new)))

    for eid, texts in section("add").items():
        e = entries.get(str(eid))
        if not e:
            raise EditError(f"There's no entry {eid} in this version.")
        if not isinstance(texts, list) or len(texts) > MAX_ADDED:
            raise EditError(f"Add up to {MAX_ADDED} bullets to {names[e.id]} at a time.")
        # A new bullet is written as the entry's last one is ("\item ..." or
        # "\resumeItem{...}"), on its own line after it.
        last = next((b for b in reversed(e.bullets) if b.whole), None)
        if not last:
            raise EditError(f"{names[e.id]} has no bullets to add one after.")
        whole = tex[last.whole.start:last.whole.end]
        cut = (last.span.start - last.whole.start, last.span.end - last.whole.start)
        line_start = tex.rfind("\n", 0, last.whole.start) + 1
        indent = re.match(r"[ \t]*", tex[line_start:last.whole.start]).group(0)
        new_lines = []
        for piece in texts:
            new = _latex(piece, f"A new bullet in {names[e.id]}")
            if new:
                new_lines.append(indent + whole[:cut[0]] + new + whole[cut[1]:])
                log.append(Edit("add", e.id, names[e.id], "", to_text(new)))
        if not new_lines:
            continue
        kept = next((b for b in reversed(e.bullets) if b.whole and b.id not in removed), None)
        if kept:
            # After the last kept bullet's line (and its fact sheet mark).
            nl = tex.find("\n", kept.whole.end)
            at = nl if nl >= 0 else len(tex)
            spans.append((rt.Span(at, at), "".join("\n" + x for x in new_lines)))
        else:
            # Every bullet removed: the new ones go where the first one was.
            first = e.bullets[0]
            at = _line_span(tex, first.whole.start, first.whole.end).start
            spans.append((rt.Span(at, at), "".join(x + "\n" for x in new_lines)))

    for eid, piece in section("tech").items():
        e = entries.get(str(eid))
        if not e or not e.tech:
            raise EditError(f"There's no tool list {eid} in this version.")
        new = _latex(piece, f"The tool list of {names[e.id]}")
        if new != _squash(e.tech_tex):
            spans.append((e.tech, new))
            log.append(Edit("tech", e.id, names[e.id], to_text(e.tech_tex), to_text(new)))

    for rid, piece in section("skills").items():
        r = rows.get(str(rid))
        if not r:
            raise EditError(f"There's no skills row {rid} in this version.")
        new = _latex(piece, f"The {r.label} row")
        if not new:
            raise EditError(f"The {r.label} row is empty.")
        if new != _squash(r.tex):
            spans.append((r.span, new))
            log.append(Edit("skills", r.id, r.label, to_text(r.tex), to_text(new)))

    # Several edits never overlap: a removed bullet's other edits are skipped,
    # and new bullets go in after a line, not inside one.
    out = tex
    for sp, txt in sorted(spans, key=lambda s: (s[0].start, s[0].end), reverse=True):
        out = out[:sp.start] + txt + out[sp.end:]
    return out, log


def measured(tex: str, edits: list[Edit]) -> tuple[str, dict[str, str]]:
    """The edited .tex with line marks around each edited or added piece,
    for compile_pdf(layout=...): {mark id: what to call it} alongside."""
    res = rt.parse(tex)
    want_bullets = {x.after for x in edits if x.kind in ("bullet", "add")}
    want_rows = {x.target for x in edits if x.kind == "skills"}
    want_tech = {x.target for x in edits if x.kind == "tech"}
    spans: list[tuple[rt.Span, str]] = []
    names: dict[str, str] = {}
    for e in res.entries:
        for b in e.bullets:
            if to_text(b.tex) in want_bullets:
                spans.append((b.span, rt.marked(b.id, b.tex)))
                text = re.sub(r"\*+", "", to_text(b.tex)).split()
                start = " ".join(text[:6]) + ("…" if len(text) > 6 else "")
                names[b.id] = f"The bullet “{start}” in {entry_name(res, e)}"
        if e.tech and e.id in want_tech:
            spans.append((e.tech, rt.marked(e.id, e.tech_tex)))
            names[e.id] = f"The tool list of {entry_name(res, e)}"
    for r in res.skill_rows:
        if r.id in want_rows:
            spans.append((r.span, rt.marked(r.id, r.tex)))
            names[r.id] = f"The {r.label} row"
    return rt.with_marks(rt.apply(tex, spans)), names
