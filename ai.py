"""Paid model APIs: the user's own OpenAI or Anthropic key, what each call
costs, and the monthly budget that stops paid calls.

Keys are entered in the dashboard and kept on this computer only: in the
OS keychain (Windows Credential Manager, macOS Keychain, Secret Service)
through `keyring` when it is installed and works, otherwise in a file only
the user can read, in the data folder (never the code folder, which may be
a git checkout). They are never written to settings.json, logged, or sent
back to the browser; the dashboard only learns whether one is saved.
OPENAI_API_KEY / ANTHROPIC_API_KEY in the environment work too, when no
key is saved.

Every paid call goes through call_json(), which
  1. estimates the cost from the prompt's token count and a typical answer
     length for the task,
  2. reserves that estimate against the provider's monthly budget in the
     ai_usage table (one transaction, so parallel calls can't both slip
     under the cap), refusing with BudgetError when it doesn't fit,
  3. makes the call and replaces the reservation with the real token
     counts and cost the API reported.
The subscription logins (chatgpt, claude-code) and manual paste never come
through here and cost nothing extra.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import config
import pricing

PROVIDERS = tuple(pricing.MODELS)
ENV_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
KEYRING_SERVICE = "Onward"
LEGACY_KEYRING_SERVICE = "jobradar"     # keys saved before the rename move to KEYRING_SERVICE when read
KEY_PATTERN = r"[A-Za-z0-9_\-.:]{20,400}"

# Typical answer length per task, in output tokens (reasoning included),
# for estimates before a call. Measured on real runs; the actual count is
# what gets recorded.
OUTPUT_TOKENS = {"tailor": 7000, "draft": 1800, "rewrite": 1500, "answers": 1500}
OUTPUT_PER_QUESTION = 350
MAX_TOKENS = {"tailor": 32000, "draft": 8000, "rewrite": 8000, "answers": 16000}
# A reservation left by a process that died mid-call stops counting after this.
RESERVATION_TTL = 2 * 3600


class BackendError(Exception):
    """A model call that couldn't be made or returned nothing usable."""


class BudgetError(BackendError):
    pass


# --------------------------------------------------------------------------
# Keys

def _load_keyring():
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    try:
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
    except Exception:
        return None
    return keyring


_KEYRING = _load_keyring()
_KEY_LOCK = threading.Lock()


def use_keyring(backend) -> None:
    """Swap the keychain (tests pass a fake, or None for the file)."""
    global _KEYRING
    _KEYRING = backend


def key_file() -> Path:
    """Fallback key file: in the data folder, unless that is the code folder
    (an install that predates the data folder), where it would sit in a git
    checkout; then in the platform's per-user folder."""
    home = config.store().home.resolve()
    code = config.CODE_DIR.resolve()
    base = config.user_data_dir() if home == code or code in home.parents else home
    return base / "secrets" / "api-keys.json"


def _read_file() -> dict:
    try:
        data = json.loads(key_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_file(data: dict) -> None:
    path = key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _check_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r}")


def clean_key(key: str) -> str:
    import re
    key = "".join(str(key or "").split())
    if not re.fullmatch(KEY_PATTERN, key):
        raise ValueError("That doesn't look like an API key. Paste the whole key.")
    return key


def save_key(provider: str, key: str) -> str:
    """Store a key; returns where it went ("keychain" or "file")."""
    _check_provider(provider)
    key = clean_key(key)
    with _KEY_LOCK:
        if _KEYRING is not None:
            try:
                _KEYRING.set_password(KEYRING_SERVICE, provider, key)
                # Don't leave an older copy behind in the file.
                data = _read_file()
                if data.pop(provider, None) is not None:
                    _write_file(data)
                return "keychain"
            except Exception:
                pass
        data = _read_file()
        data[provider] = key
        _write_file(data)
        return "file"


def delete_key(provider: str) -> None:
    _check_provider(provider)
    with _KEY_LOCK:
        if _KEYRING is not None:
            for service in (KEYRING_SERVICE, LEGACY_KEYRING_SERVICE):
                try:
                    _KEYRING.delete_password(service, provider)
                except Exception:
                    pass
        data = _read_file()
        if data.pop(provider, None) is not None:
            _write_file(data)


def _stored_key(provider: str) -> tuple[str | None, str | None]:
    if _KEYRING is not None:
        try:
            key = _KEYRING.get_password(KEYRING_SERVICE, provider)
            if key:
                return key, "keychain"
            key = _KEYRING.get_password(LEGACY_KEYRING_SERVICE, provider)
            if key:
                with _KEY_LOCK:
                    _KEYRING.set_password(KEYRING_SERVICE, provider, key)
                    _KEYRING.delete_password(LEGACY_KEYRING_SERVICE, provider)
                return key, "keychain"
        except Exception:
            pass
    key = _read_file().get(provider)
    if isinstance(key, str) and key:
        return key, "file"
    env = os.environ.get(ENV_KEYS[provider])
    if env:
        return env, "environment"
    return None, None


def get_key(provider: str) -> str | None:
    _check_provider(provider)
    return _stored_key(provider)[0]


def key_status(provider: str) -> dict:
    """What the dashboard may know about a key: whether there is one and
    where it lives. Never the key, nor any part of it."""
    _, where = _stored_key(provider)
    return {"saved": where is not None, "where": where}


# --------------------------------------------------------------------------
# Clients. Tests replace CLIENTS entries with fakes; nothing else does.

def _openai_client(key: str | None):
    import openai
    if not key:
        raise BackendError("No OpenAI API key. Add one in Settings, under AI models.")
    return openai.OpenAI(api_key=key, max_retries=2)


def _anthropic_client(key: str | None):
    import anthropic
    # Without a saved key the SDK still finds ANTHROPIC_API_KEY or an
    # `ant auth login` profile.
    return anthropic.Anthropic(api_key=key, max_retries=2) if key else anthropic.Anthropic(max_retries=2)


CLIENTS: dict[str, Callable] = {"openai": _openai_client, "anthropic": _anthropic_client}


def client(provider: str):
    return CLIENTS[provider](get_key(provider))


def verify_key(provider: str, key: str) -> str | None:
    """None when the key works, else why not. Listing models is free."""
    try:
        CLIENTS[provider](key).with_options(timeout=15, max_retries=0).models.list()
    except Exception as e:
        name = type(e).__name__
        if "Authentication" in name or "PermissionDenied" in name:
            return f"{pricing.PROVIDER_NAMES[provider]} rejected this key."
        if "Connection" in name or "Timeout" in name:
            return "connection"
        return f"Couldn't check the key ({name})."
    return None


# --------------------------------------------------------------------------
# Settings

def settings(provider: str) -> dict:
    return config.store().section("providers")[provider]


def model_for(provider: str) -> str:
    if provider == "anthropic" and os.environ.get("JOBRADAR_CLAUDE_MODEL"):
        return os.environ["JOBRADAR_CLAUDE_MODEL"]
    return settings(provider)["model"]


def price_for(provider: str, model: str) -> pricing.Model:
    # A model outside the table (set through the environment) is priced as
    # the provider's most expensive one, so the budget errs on the safe side.
    return pricing.find(provider, model) or max(pricing.MODELS[provider], key=lambda m: m.output)


# --------------------------------------------------------------------------
# Token counts and estimates

_COUNT_CACHE: dict[str, int] = {}
_COUNT_LOCK = threading.Lock()
CHARS_PER_TOKEN = {"openai": 4.0, "anthropic": 3.5}


def rough_tokens(provider: str, text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN[provider]) + 1


def count_tokens(provider: str, model: str, system: str, user: str, exact: bool = True) -> tuple[int, bool]:
    """(input tokens, whether the count is exact). Both providers count
    tokens for free; without a key, or offline, it's a character estimate."""
    if not exact or not get_key(provider) and provider == "openai":
        return rough_tokens(provider, system + user), False
    digest = hashlib.sha256(f"{provider}\0{model}\0{system}\0{user}".encode("utf-8")).hexdigest()
    with _COUNT_LOCK:
        if digest in _COUNT_CACHE:
            return _COUNT_CACHE[digest], True
    try:
        c = client(provider).with_options(timeout=15, max_retries=0)
        if provider == "anthropic":
            n = c.messages.count_tokens(model=model, system=system,
                                        messages=[{"role": "user", "content": user}]).input_tokens
        else:
            n = c.responses.input_tokens.count(model=model, instructions=system, input=user).input_tokens
    except Exception:
        return rough_tokens(provider, system + user), False
    with _COUNT_LOCK:
        if len(_COUNT_CACHE) > 500:
            _COUNT_CACHE.clear()
        _COUNT_CACHE[digest] = int(n)
    return int(n), True


def expected_output(purpose: str, questions: int = 0) -> int:
    return OUTPUT_TOKENS[purpose] + (OUTPUT_PER_QUESTION * questions if purpose == "answers" else 0)


def estimate(provider: str, system: str, user: str, purpose: str, questions: int = 0, exact: bool = True) -> dict:
    model = model_for(provider)
    tokens, is_exact = count_tokens(provider, model, system, user, exact)
    out = expected_output(purpose, questions)
    cost = price_for(provider, model).cost(input_tokens=tokens, output_tokens=out)
    return {"provider": provider, "model": model, "input_tokens": tokens, "output_tokens": out,
            "cost": round(cost, 4), "exact_input": is_exact}


# --------------------------------------------------------------------------
# Usage and budget

USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_usage (
    id            TEXT PRIMARY KEY,
    created       REAL NOT NULL,
    month         TEXT NOT NULL,             -- YYYY-MM, local time
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    purpose       TEXT NOT NULL,             -- tailor, draft, answers, rewrite
    state         TEXT NOT NULL,             -- reserved, done, failed
    estimate      REAL NOT NULL DEFAULT 0,
    cost          REAL NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_read    INTEGER NOT NULL DEFAULT 0,
    cache_write   INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ai_usage_month ON ai_usage(provider, month);
CREATE INDEX IF NOT EXISTS ai_usage_created ON ai_usage(created);
"""


def connect() -> sqlite3.Connection:
    path = config.store().path_for("db")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.executescript(USAGE_SCHEMA)
    return conn


def this_month(now: float | None = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time()).strftime("%Y-%m")


def _spent(conn, provider: str, month: str, now: float) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN state = 'reserved' THEN (CASE WHEN created >= ? THEN estimate ELSE 0 END) "
        "ELSE cost END), 0) FROM ai_usage WHERE provider = ? AND month = ?",
        (now - RESERVATION_TTL, provider, month)).fetchone()
    return float(row[0])


def spent(provider: str, month: str | None = None) -> float:
    conn = connect()
    try:
        return _spent(conn, provider, month or this_month(), time.time())
    finally:
        conn.close()


def budget_message(provider: str, budget: float, used: float, cost: float) -> str:
    name = pricing.PROVIDER_NAMES[provider]
    month = datetime.now().strftime("%B")
    if budget <= 0:
        return (f"Your {name} API budget is $0, so Onward makes no paid {name} calls. "
                "Set a monthly budget in Settings, under AI models, or use your ChatGPT or Claude login.")
    return (f"This would go over your {name} API budget for {month}: ${used:.2f} of ${budget:.2f} is used and "
            f"this run would cost about ${cost:.2f}. Raise the budget in Settings, under AI models, "
            "or use your ChatGPT or Claude login, which costs nothing extra.")


def reserve(provider: str, model: str, purpose: str, cost: float) -> str:
    """Hold an estimated cost against this month's budget, or raise
    BudgetError. Returns the reservation id for finish()."""
    budget = float(settings(provider)["monthly_budget"])
    now = time.time()
    month = this_month(now)
    rid = uuid.uuid4().hex
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")      # serializes reservations across processes
        try:
            used = _spent(conn, provider, month, now)
            if budget <= 0 or used + cost > budget:
                conn.execute("ROLLBACK")
                raise BudgetError(budget_message(provider, budget, used, cost))
            conn.execute("INSERT INTO ai_usage (id, created, month, provider, model, purpose, state, estimate) "
                         "VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)", (rid, now, month, provider, model, purpose, cost))
            conn.execute("COMMIT")
        except BudgetError:
            raise
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return rid


def finish(rid: str, provider: str, model: str, usage: dict | None, error: str = "") -> float:
    """Record what a call really used. A call that failed before the API
    billed anything costs 0."""
    u = {k: int((usage or {}).get(k) or 0) for k in ("input_tokens", "cache_read", "cache_write", "output_tokens")}
    cost = price_for(provider, model).cost(u["input_tokens"], u["output_tokens"], u["cache_read"], u["cache_write"])
    conn = connect()
    try:
        conn.execute("UPDATE ai_usage SET state = ?, model = ?, cost = ?, input_tokens = ?, cache_read = ?, "
                     "cache_write = ?, output_tokens = ?, error = ? WHERE id = ?",
                     ("failed" if error else "done", model, cost, u["input_tokens"], u["cache_read"],
                      u["cache_write"], u["output_tokens"], error[:300], rid))
    finally:
        conn.close()
    return cost


def usage_summary(months: int = 6, recent: int = 50) -> dict:
    """Spend per provider for the last few months and the latest calls."""
    now = time.time()
    conn = connect()
    try:
        cutoff = datetime.fromtimestamp(now - months * 31 * 86400).strftime("%Y-%m")
        rows = conn.execute(
            "SELECT month, provider, COUNT(*), SUM(cost), SUM(input_tokens + cache_read + cache_write), SUM(output_tokens) "
            "FROM ai_usage WHERE month >= ? AND state != 'reserved' GROUP BY month, provider "
            "ORDER BY month DESC, provider", (cutoff,)).fetchall()
        calls = conn.execute(
            "SELECT created, provider, model, purpose, state, cost, input_tokens + cache_read + cache_write, "
            "output_tokens, error FROM ai_usage ORDER BY created DESC LIMIT ?", (recent,)).fetchall()
        month = this_month(now)
        current = {p: round(_spent(conn, p, month, now), 4) for p in PROVIDERS}
    finally:
        conn.close()
    return {
        "month": month,
        "providers": {p: {"spent": current[p], "budget": settings(p)["monthly_budget"], "model": model_for(p)}
                      for p in PROVIDERS},
        "months": [{"month": m, "provider": p, "calls": n, "cost": round(c or 0, 4), "input_tokens": i or 0,
                    "output_tokens": o or 0} for m, p, n, c, i, o in rows],
        "calls": [{"created": t, "provider": p, "model": m, "purpose": pu, "state": s, "cost": round(c, 4),
                   "input_tokens": i, "output_tokens": o, "error": e} for t, p, m, pu, s, c, i, o, e in calls],
    }


# --------------------------------------------------------------------------
# Calls

def _anthropic_call(c, model: str, system: str, user: str, schema: dict, max_tokens: int,
                    cache_system: bool, purpose: str) -> tuple[str, str, dict, str]:
    """(text, model that answered, usage, problem). A problem is a reason
    the text can't be used even though the call was billed."""
    import anthropic
    sys_block = {"type": "text", "text": system}
    if cache_system:
        sys_block["cache_control"] = {"type": "ephemeral"}
    kwargs = {}
    if not model.startswith("claude-haiku"):
        # On a policy decline the API answers with another model in the same call.
        kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    try:
        with c.beta.messages.stream(
            model=model, max_tokens=max_tokens, system=[sys_block],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            **kwargs,
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise BackendError("Anthropic rejected the API key. Check it in Settings, under AI models.") from e
    except anthropic.PermissionDeniedError as e:
        raise BackendError("This Anthropic API key can't use that model. Pick another in Settings.") from e
    except anthropic.NotFoundError as e:
        raise BackendError(f"Anthropic doesn't know the model {model}. Pick another in Settings.") from e
    except anthropic.RateLimitError as e:
        raise BackendError("Anthropic rate limit hit. Wait a minute and try again.") from e
    except anthropic.APIStatusError as e:
        raise BackendError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise BackendError("Couldn't reach the Anthropic API.") from e
    u = msg.usage
    usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
             "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
             "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0}
    problem = ""
    if msg.stop_reason == "refusal":
        problem = "Claude declined this request."
    elif msg.stop_reason == "max_tokens":
        problem = "Claude's answer was cut off. Try again."
    text = next((b.text for b in msg.content if b.type == "text"), "")
    return text, msg.model or model, usage, problem


def _openai_call(c, model: str, system: str, user: str, schema: dict, max_tokens: int,
                 cache_system: bool, purpose: str) -> tuple[str, str, dict, str]:
    import openai
    try:
        resp = c.responses.create(
            model=model, instructions=system, input=user, max_output_tokens=max_tokens,
            reasoning={"effort": "medium"}, store=False,
            # Tailoring prompts share their long instructions and resume.
            prompt_cache_key=f"jobradar-{purpose}",
            text={"format": {"type": "json_schema", "name": "result", "schema": schema, "strict": True}},
        )
    except openai.AuthenticationError as e:
        raise BackendError("OpenAI rejected the API key. Check it in Settings, under AI models.") from e
    except openai.PermissionDeniedError as e:
        raise BackendError("This OpenAI API key can't use that model. Pick another in Settings.") from e
    except openai.NotFoundError as e:
        raise BackendError(f"OpenAI doesn't know the model {model}. Pick another in Settings.") from e
    except openai.RateLimitError as e:
        raise BackendError("OpenAI rate limit or quota hit. Wait a minute, or check your OpenAI billing.") from e
    except openai.APIStatusError as e:
        raise BackendError(f"OpenAI API error {e.status_code}: {e.message}") from e
    except openai.APIConnectionError as e:
        raise BackendError("Couldn't reach the OpenAI API.") from e
    u = resp.usage
    cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0 if u else 0
    usage = {"input_tokens": (u.input_tokens - cached) if u else 0, "cache_read": cached,
             "output_tokens": u.output_tokens if u else 0}
    problem = ""
    refusal = next((part.refusal for item in resp.output or [] if item.type == "message"
                    for part in item.content if part.type == "refusal"), None)
    if refusal:
        problem = "The model declined this request."
    elif resp.status == "incomplete":
        reason = getattr(resp.incomplete_details, "reason", "") or ""
        problem = ("The model's answer was cut off. Try again." if reason == "max_output_tokens"
                   else f"The model stopped early ({reason or 'incomplete'}).")
    return resp.output_text or "", resp.model or model, usage, problem


CALLS = {"openai": _openai_call, "anthropic": _anthropic_call}


def call_json(provider: str, system: str, user: str, schema: dict, purpose: str,
              questions: int = 0, cache_system: bool = False) -> tuple[dict, str, dict]:
    """One structured answer from a paid API, inside the monthly budget.
    Returns (parsed JSON, model, {"cost", "estimate", "usage"})."""
    _check_provider(provider)
    est = estimate(provider, system, user, purpose, questions)
    model = est["model"]
    c = client(provider)
    rid = reserve(provider, model, purpose, est["cost"])
    try:
        text, used_model, usage, problem = CALLS[provider](c, model, system, user, schema, MAX_TOKENS[purpose],
                                                           cache_system, purpose)
    except BaseException as e:
        finish(rid, provider, model, None, str(e) or type(e).__name__)
        raise
    try:
        if problem:
            raise BackendError(problem)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise BackendError("The model returned malformed output. Try again.") from e
        if not isinstance(data, dict):
            raise BackendError("The model returned malformed output. Try again.")
    except BackendError as e:
        finish(rid, provider, used_model, usage, str(e))
        raise
    cost = finish(rid, provider, used_model, usage)
    return data, used_model, {"cost": round(cost, 4), "estimate": est["cost"], "usage": usage}
