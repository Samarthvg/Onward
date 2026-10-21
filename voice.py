"""The user's writing-voice settings for drafted answers: tone, their own
writing samples, and the banned phrases tailor.style_problems() checks.

Banned phrases are plain text so anyone can edit the list in Settings:
  *      any word ending ("resonat*" covers resonates, resonating)
  ...    up to 60 characters of anything ("not only ... but also")
  -      a hyphen or a space ("cutting-edge" also catches "cutting edge")
Matching ignores case and whole words only, so "dynamic" doesn't flag
"dynamically" unless written "dynamic*".
"""

from __future__ import annotations

import re
from functools import lru_cache

TONES = ("casual", "neutral", "formal")
MAX_SAMPLES = 3
SAMPLE_MAX = 3000

# Stock phrasing that marks text as machine-written. Same coverage as the
# regular expressions it replaced.
DEFAULT_BANNED = (
    "appeal to me", "appeals to me", "resonat*", "align with", "aligns with", "align perfectly", "aligns perfectly",
    "connect to", "connects to", "connect with", "connects with",
    "make that connection", "makes that connection", "made that connection", "concrete", "concretely",
    "I'd bring", "I would bring", "leverag*", "passionat*", "thrilled", "excited to", "eager to", "deeply",
    "at the intersection of", "drive impact", "drive real impact", "cutting-edge", "seamless", "seamlessly",
    "robust", "fast-paced", "dynamic", "I am confident", "not only ... but also",
    "this experience taught", "this experience gave", "this experience showed", "ultimately", "furthermore",
    "moreover", "in today's", "unique opportunity", "synergy", "end-to-end",
    "offer the chance", "offers the chance", "offer me the chance", "offers me the chance", "the opportunity to",
    "a big reason", "this particular role", "I'd like to apply", "matter to me", "matters to me",
    "take an idea from", "through to", "grounded", "meaningful", "impactful", "contribute to",
)


def _part(text: str) -> str:
    text = " ".join(text.split())
    rx = ""
    for ch in text:
        if ch == "*":
            rx += r"[\w'’]*"
        elif ch == " ":
            rx += r"\s+"
        elif ch == "-":
            rx += r"[- ]"
        elif ch in "'’":
            rx += "['’]"
        else:
            rx += re.escape(ch)
    start = r"\b" if re.match(r"\w", text) else ""
    end = r"\b" if re.search(r"\w$", text) else ""
    return start + rx + end


def phrase_pattern(phrase: str) -> str | None:
    parts = [p for p in (x.strip() for x in phrase.split("...")) if p]
    return ".{0,60}?".join(_part(p) for p in parts) if parts else None


@lru_cache(maxsize=16)
def compile_phrases(phrases: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    out = []
    for p in phrases:
        rx = phrase_pattern(p)
        if rx:
            out.append(re.compile(rx, re.I | re.S))
    return tuple(out)
