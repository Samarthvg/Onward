"""The skills taxonomy (skills.json), compiled once, and a fast finder.

Finding skills runs ~250 patterns over every requirement line of every
posting, which made a dashboard rebuild take minutes. Each pattern's
alternatives start with literal text ("kubernetes", "k8s"), and a line that
contains none of them can't match, so a plain substring test skips most
patterns before their regex runs. The literals come from Python's own
parse of each pattern; a pattern whose alternatives don't all start with
one is always run.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

try:                                    # Python 3.11+
    import re._parser as _parser
    import re._constants as _const
except ImportError:                     # pragma: no cover - older Pythons
    import sre_parse as _parser
    import sre_constants as _const

ROOT = Path(__file__).resolve().parent
BOUNDARY = "(?<![A-Za-z0-9+#])(?:{})(?![A-Za-z0-9+#])"
TAXONOMY: list[dict] = json.loads((ROOT / "skills.json").read_text(encoding="utf-8"))


def _letter(av) -> str | None:
    """The one letter a class like [Gg] stands for, ignoring case."""
    chars = {chr(v).lower() for op, v in av if op == _const.LITERAL}
    return chars.pop() if len(chars) == 1 and all(op == _const.LITERAL for op, _ in av) else None


def _lead(items) -> list[str] | None:
    """The literal text each way through a parsed pattern starts with, or
    None when some way doesn't start with at least two literal characters.
    The parser factors a shared start out of alternatives ("mysql|mariadb"
    is "m" then a branch), so a branch or group continues the text."""
    text = ""
    for op, av in items:
        if op in (_const.AT, _const.ASSERT_NOT) and not text:
            continue                    # \b or a lookbehind: zero width, at the start
        if op == _const.LITERAL:
            text += chr(av)
            continue
        if op == _const.IN and _letter(av):
            text += _letter(av)
            continue
        if op in (_const.BRANCH, _const.SUBPATTERN):
            alts = av[1] if op == _const.BRANCH else [av[-1]]
            subs = [_lead(a) for a in alts]
            if all(subs):
                return [(text + s).lower() for sub in subs for s in sub]
        break
    return [text.lower()] if len(text) >= 2 else None


def triggers(pattern: str) -> tuple[str, ...] | None:
    try:
        lead = _lead(_parser.parse(pattern))
    except Exception:
        return None
    return tuple(sorted(set(lead))) if lead else None


SKILLS: list[tuple[str, re.Pattern]] = [
    (s["n"], re.compile(BOUNDARY.format(s["re"]), 0 if s.get("cs") else re.I)) for s in TAXONOMY
]
_TRIGGERS = [triggers(s["re"]) for s in TAXONOMY]
_ALWAYS = [i for i, trig in enumerate(_TRIGGERS) if trig is None]
_BY_TRIGGER: dict[str, list[int]] = {}
for _i, _trig in enumerate(_TRIGGERS):
    for _t in _trig or ():
        _BY_TRIGGER.setdefault(_t, []).append(_i)
# A match starts where a word starts (BOUNDARY), so a trigger can only be in a
# line if its first characters (up to 3) start one of the line's words. The
# triggers are indexed by those characters; a trigger that doesn't start
# with a word character is tested on its own.
_WORD = re.compile(r"[a-z0-9+#]+")
_KEYED: dict[str, list[str]] = {}
_LOOSE: list[str] = []
for _t in _BY_TRIGGER:
    _first = _WORD.match(_t)
    if _first:
        _KEYED.setdefault(_first.group(0)[:3], []).append(_t)      # "ai agents" under "ai"
    else:
        _LOOSE.append(_t)


@functools.lru_cache(maxsize=200_000)
def _find(text: str) -> frozenset[str]:
    low = text.lower()
    starts = set()
    for w in _WORD.findall(low):
        starts.update((w[:1], w[:2], w[:3]))
    candidates = set(_ALWAYS)
    for key in starts:
        for t in _KEYED.get(key, ()):
            if t in low:
                candidates.update(_BY_TRIGGER[t])
    for t in _LOOSE:
        if t in low:
            candidates.update(_BY_TRIGGER[t])
    return frozenset(SKILLS[i][0] for i in candidates if SKILLS[i][1].search(text))


def find(text: str) -> set[str]:
    """Taxonomy skills named in `text`. Remembered per text: the same lines
    (boilerplate, a posting seen again) come up on every rebuild."""
    return set(_find(text)) if text else set()
