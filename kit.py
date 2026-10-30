"""What the user keeps for application forms, held by the helper so the
app's window, any browser and the extension see the same copy:

  profile  name, contact details, links, school (the Apply kit)
  answers  standard questions (work authorization, sponsorship, ...) and
           the voluntary self-identification answers
  marks    the dashboard's Applied and Dismiss marks
  memory   answers the extension saved from forms, kept here as its backup

Each value carries when it last changed (ms); the later one wins. Stored in
the resume folder as dashboard.json.
"""

import json
import os
import threading
from pathlib import Path

import config

LOCK = threading.Lock()

MAX_MEMORY = 5000            # saved answers and their use logs
MAX_MEMORY_BYTES = 4_000_000


def _flat(v, n: int, size: int) -> dict | None:
    if not isinstance(v, dict) or len(v) > n:
        return None
    return {str(k)[:60]: str(x)[:size] for k, x in v.items()
            if isinstance(x, (str, int, float)) and not isinstance(x, bool)}


def _clean_marks(v) -> dict | None:
    if not isinstance(v, dict) or len(v) > 50000:
        return None
    return {str(k)[:2000]: x for k, x in v.items() if x in ("applied", "dismissed")}


def _clean_memory(v) -> dict | None:
    """{"mem:<question>": {...}, "use:<question>": [...]}, as the extension keeps them."""
    if not isinstance(v, dict) or len(v) > MAX_MEMORY:
        return None
    out = {str(k)[:600]: x for k, x in v.items()
           if str(k).startswith(("mem:", "use:")) and isinstance(x, (dict, list))}
    return out if len(json.dumps(out)) <= MAX_MEMORY_BYTES else None


CLEAN = {
    "profile": lambda v: _flat(v, 100, 2000),
    "answers": lambda v: _flat(v, 100, 2000),
    "marks": _clean_marks,
    "memory": _clean_memory,
}

# The Apply kit once kept two answers in words itself; they are answers now.
_MOVED = {"workAuth": "workAuthText", "sponsorship": "sponsorshipText"}


def path() -> Path:
    return config.store().path_for("resume_dir") / "dashboard.json"


def _read() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if k in CLEAN and isinstance(v, dict) and "value" in v} \
        if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, p)


def _upgrade(data: dict) -> bool:
    """Brings an older Apply kit up to date: its answers-in-words move out of
    the profile, and its single "City, State" splits into City and State."""
    prof = (data.get("profile") or {}).get("value")
    if not isinstance(prof, dict):
        return False
    changed = False
    if any(k in prof for k in _MOVED):
        ans = dict((data.get("answers") or {}).get("value") or {})
        for old, new in _MOVED.items():
            v = prof.pop(old, "")
            if v and not ans.get(new):
                ans[new] = v
        at = max(float(data["profile"].get("at") or 0), float((data.get("answers") or {}).get("at") or 0))
        data["answers"] = {"value": ans, "at": at}
        changed = True
    if "location" in prof:
        city, _, st = str(prof.pop("location")).partition(",")
        if not prof.get("city") and not prof.get("state"):
            prof["city"], prof["state"] = city.strip(), st.strip()
        # Newer than the copies the dashboard and the extension hold, so they take this one.
        data["profile"]["at"] = float(data["profile"].get("at") or 0) + 1
        changed = True
    return changed


def location(profile: dict) -> str:
    """ "City, State", for extensions before 0.3.1, which read one field."""
    return ", ".join(x for x in (str(profile.get("city") or "").strip(), str(profile.get("state") or "").strip()) if x)


def state() -> dict:
    """{key: {"value": ..., "at": ms}} for the keys that are saved."""
    with LOCK:
        data = _read()
        if _upgrade(data):
            _write(data)
        return data


def value(key: str) -> dict:
    return (state().get(key) or {}).get("value") or {}


def save(key: str, val, at: float) -> dict:
    """Saves one value unless what's saved is newer. Returns what's saved now."""
    clean = CLEAN[key](val)
    if clean is None:
        raise ValueError(f"That isn't a valid {key} value")
    with LOCK:
        data = _read()
        _upgrade(data)
        if key not in data or float(data[key].get("at") or 0) <= at:
            data[key] = {"value": clean, "at": at}
            if key == "profile":
                _upgrade(data)
            _write(data)
        return data[key]
