"""Match score for one job against the user's resume, the same number the
dashboard shows.

This is a line-for-line port of scoreJob() and its helpers in
dashboard_template.html, so the helper can rank jobs while no dashboard tab is
open. test_match.py runs both versions on the same fixtures; change them
together.

Score = keyword coverage x experience factor. The keywords are the posting's
checklist (job["kw"] from reqparse.keywords), each weighted by where it
appears and by its rarity across stored postings (radar.corpus_stats).
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime

import config
import resume_tex as rt

TAXONOMY = {name for name, _ in rt.SKILLS}

MONTHS = {"jan": 0, "feb": 1, "mar": 2, "apr": 3, "may": 4, "jun": 5, "jul": 6, "aug": 7,
          "sep": 8, "sept": 8, "oct": 9, "nov": 10, "dec": 11}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?"
RANGE = re.compile(MON + r"\s+(\d{4})\s*(?:--?|–|—|to)\s*(?:(present|current|now)|" + MON + r"\s+(\d{4}))", re.I)
DEGREE_LINE = re.compile(r"bachelor|master|b\.?\s?tech|m\.?\s?s\.?\b|ph\.?d|gpa|degree|coursework|diploma", re.I)
INTERN_LINE = re.compile(r"intern|co-?op|research assistant|teaching assistant|trainee", re.I)
DEGREE_RANK = {"associate": 0, "bachelor": 1, "master": 2, "phd": 3}
DEGREE_NAME = {"associate": "Associate's", "bachelor": "Bachelor's", "master": "Master's", "phd": "PhD"}
MS_PER_MONTH = 30.44 * 86400000
# settings.json "scoring"; the dashboard reads the same values.
WEIGHTS = config.SCHEMA.fields["scoring"].default


def js_round(x: float) -> int:
    """Math.round: halves go up, unlike Python's round()."""
    return math.floor(x + 0.5)


def detect_skills(text: str) -> set[str]:
    return rt.with_implied(rt.find_skills(text))


def tex_text(src: str) -> str:
    """texText(): the text the dashboard scores when the resume is a .tex file."""
    s = re.sub(r"(^|[^\\])%.*$", r"\1", src, flags=re.M)
    s = re.sub(r"\\(?:href|url)\{([^}]*)\}(?:\{([^}]*)\})?", lambda m: f" {m.group(1)} {m.group(2) or ''} ", s)
    s = re.sub(r"\\(?:begin|end)\{[^}]*\}", " ", s)
    s = re.sub(r"\\[a-zA-Z@]+\*?(?:\[[^\]]*\])?", " ", s)
    s = re.sub(r"[{}$]", " ", s)
    s = s.replace("\\\\", "\n")
    return re.sub(r"[ \t]+", " ", s)


def _ms(year: int, month: int) -> float:
    # new Date(year, month): local midnight on the 1st.
    return datetime(year + month // 12, month % 12 + 1, 1).timestamp() * 1000


def resume_experience(text: str, now_ms: float | None = None) -> dict:
    """Years of professional experience from dated ranges; internships half,
    education skipped, overlaps merged."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    lines = [l.strip() for l in re.split(r"\r?\n", text) if l.strip()]
    spans = []
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if DEGREE_LINE.search(line) or DEGREE_LINE.search(nxt):
            continue
        for m in RANGE.finditer(line):
            start = _ms(int(m.group(2)), MONTHS[m.group(1).lower()[:3]])
            end = now_ms if m.group(3) else _ms(int(m.group(5)), MONTHS[m.group(4).lower()[:3]])
            if end <= start:
                continue
            role = line + " " + nxt
            spans.append({"start": start, "end": end, "factor": 0.5 if INTERN_LINE.search(role) else 1,
                          "label": re.sub(r"https?://\S+", "", line).strip()[:80]})
    spans.sort(key=lambda s: s["start"])     # stable, like Array.prototype.sort
    months, cursor = 0.0, 0.0
    for s in spans:
        frm = max(s["start"], cursor)
        if s["end"] > frm:
            months += ((s["end"] - frm) / MS_PER_MONTH) * s["factor"]
        cursor = max(cursor, s["end"])
    return {"years": js_round((months / 12) * 10) / 10, "spans": spans}


def resume_degree(text: str) -> str | None:
    if re.search(r"\bph\.?\s?d\b|doctor of", text, re.I):
        return "phd"
    if re.search(r"master|\bm\.?\s?s\.?\s+in\b|\bmsc\b|m\.?\s?tech|\bmba\b", text, re.I):
        return "master"
    if re.search(r"bachelor|b\.?\s?tech|\bb\.?\s?s\.?\s+in\b|\bbsc\b|\bb\.?\s?e\.?\s+in\b|\bb\.?\s?a\.?\s+in\b", text, re.I):
        return "bachelor"
    return None


def degree_fit(req: dict | None, have: str | None) -> dict | None:
    if not req:
        return None
    need = DEGREE_NAME[req["level"]] + (" or equivalent" if req.get("or_equivalent") else "")
    if have and DEGREE_RANK[have] >= DEGREE_RANK[req["level"]]:
        return {"ok": True, "label": f"{need}, you have {DEGREE_NAME[have]}"}
    return {"ok": False, "label": f"asks {need}" + (f", you have {DEGREE_NAME[have]}" if have else "")}


def _num(x: float) -> str:
    """How JS prints a number in a template string (2.0 -> "2")."""
    return str(int(x)) if float(x).is_integer() else str(x)


def exp_fit(job: dict, years: float, w: dict = WEIGHTS) -> dict:
    mn = job.get("min_yoe")
    if mn is None:
        entry = bool(job.get("entry_hint"))
        return {"v": 1 if entry else w["unstated_years_fit"], "label": "entry level" if entry else "years not stated"}
    label = f"asks {mn}+ yrs, you have ~{_num(years)}"
    if years >= mn:
        return {"v": 1, "ok": True, "label": label}
    return {"v": max(w["min_experience_fit"], 1 - (mn - years) * w["years_penalty"]), "ok": False, "label": label}


def term_regex(t: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", re.I)


class Resume:
    """What scoreJob's ctx and `have` hold: the resume text, its skills after
    the user's own corrections, years and degree."""

    def __init__(self, text: str, skills: list[str] | None = None, off: list[str] | None = None,
                 added: list[str] | None = None, years: float | None = None, now_ms: float | None = None):
        self.text = text
        base = set(skills) if skills is not None else detect_skills(text)
        self.have = (base | set(added or [])) - set(off or [])
        self.years = years if years is not None else resume_experience(text, now_ms)["years"]
        self.degree = resume_degree(text)
        self._terms: dict[str, bool] = {}

    @classmethod
    def from_profile(cls, p: dict) -> "Resume":
        """From the profile the dashboard sends to /api/scoring (its state.resume)."""
        return cls(p.get("text", ""), p.get("skills"), p.get("off"), p.get("added"), p.get("years"))

    def has_term(self, t: str) -> bool:
        if t not in self._terms:
            self._terms[t] = bool(term_regex(t).search(self.text))
        return self._terms[t]


def score_job(job: dict, resume: Resume, skill_w: dict, term_w: dict, weights: dict | None = None) -> dict:
    w_ = {**WEIGHTS, **(weights or {})}
    kw = job.get("kw") or []
    if not job.get("has_jd") or not kw:
        return {"pct": None, "items": [], "noJd": not job.get("has_jd")}
    W = M = 0.0
    items = []
    for k, w, kind in kw:
        weight = w * (skill_w[k] if k in skill_w else term_w[k] if k in term_w else w_["unknown_keyword_weight"])
        ok = k in resume.have if k in TAXONOMY else resume.has_term(k)
        W += weight
        if ok:
            M += weight
        items.append({"k": k, "kind": kind, "ok": ok, "weight": weight})
    # Short checklists are thin evidence: mix in 3 average keywords at 50%.
    prior = w_["prior_keywords"] * (W / len(kw))
    cov = (M + w_["prior_credit"] * prior) / (W + prior)
    exp = exp_fit(job, resume.years, w_)
    ew = w_["experience_weight"]
    pct = js_round(100 * cov * (1 - ew + ew * exp["v"]))

    def count(kinds):
        xs = [i for i in items if i["kind"] in kinds]
        return {"have": sum(1 for i in xs if i["ok"]), "of": len(xs)}

    items.sort(key=lambda i: -i["weight"])
    return {
        "pct": pct, "thin": len(kw) < 4, "cov": js_round(cov * 100), "exp": exp, "items": items,
        "req": count(("title", "req")), "pref": count(("pref",)),
        "degree": degree_fit(job.get("degree"), resume.degree),
        "matched": [i["k"] for i in items if i["ok"]],
        "missing": [i["k"] for i in items if not i["ok"]],
    }
