"""Tailor a master LaTeX resume to one job description.

The model only proposes edits by ID (rewrite bullet B4, reorder, drop project
E6, ...). resume_tex validates every edit against the master and applies the
survivors to the original source, then this module compiles and trims until
the result fits on one page.

Backends, all returning the same JSON:
  chatgpt      OpenAI's `codex` CLI on your ChatGPT subscription login, no API key
  claude-code  the `claude` CLI on your Claude subscription login, no API key
  openai-api   the OpenAI API on the user's own key, billed per token (ai.py)
  claude-api   the Anthropic API on the user's own key, billed per token (ai.py)
  manual       you paste the prompt into any chat and paste the JSON answer back

The API backends go through ai.call_json(), which estimates each call's
cost, keeps to the monthly budget and records what it really cost.

draft_answer() and answer_questions() write answers to application
questions with the same backends (manual paste is handled by the caller),
in the voice the user set up in Settings.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import ai
import config
import pricing
import resume_tex as rt
import voice

def _tailoring(key: str, env: str | None = None):
    """settings.json "tailoring"; the JOBRADAR_* environment variables that
    predate it still win when set."""
    return (os.environ.get(env) if env else None) or config.store().section("tailoring")[key]


def runner_dir() -> Path:
    """Empty working folder for the CLI backends, so no project files load."""
    return config.store().path_for("resume_dir") / ".claude-runner"


def target_pages() -> int:
    return _tailoring("target_pages")


def min_bullets() -> int:
    return _tailoring("min_bullets")


def bullet_limits(res: rt.Resume) -> tuple[int, dict[str, int]]:
    """The fewest bullets a tailored entry shows (Settings) and the most, by
    kind: what the resume's own entries of that kind show at most, so a
    tailored version is as full as the resume it comes from, within
    rt.MOST_BULLETS and never under the fewest."""
    low = min_bullets()
    own: dict[str, int] = {}
    for e in res.entries:
        if not e.fact and e.kind in ("experience", "project"):
            # Its own bullets only: the fact sheet's drawn into it don't make it fuller.
            own[e.kind] = max(own.get(e.kind, 0), sum(not b.fact for b in e.bullets))
    other = max(own.values(), default=4)
    return low, {k: max(low, min(rt.MOST_BULLETS, own.get(k, other))) for k in ("experience", "project")}


def strictness() -> str:
    return _tailoring("strictness")


INSTRUCTIONS = """You tailor a software engineer's resume to one job description by proposing edits to numbered pieces of the resume. Your output is applied by a program and every edit is checked, so follow the rules exactly.

Rules
1. Truthfulness comes first. A skill may appear in an entry's bullets or tech line only if it is in that entry's VERIFIED SKILLS list. Skills-section rows may use only the VERIFIED INVENTORY. If the job wants something the resume doesn't support, report it as "missing" in jd_keywords and do not add it anywhere.
2. Never add or change numbers, metrics, company names, job titles, dates or URLs.
3. Keep what each bullet says the person did: the same work, scope, results and tools. Within that, you may rephrase a bullet freely: restructure the sentence, lead with the part that matters most for this job, pick different verbs, use the job description's terminology for things the bullet already describes (for example "REST endpoints" -> "RESTful APIs"), and name a verified skill for that entry explicitly. Shape each bullet you rewrite like a one-line STAR answer: the action, how it was done, then the result, with a result only where the original states one.
4. Keep LaTeX exactly as it appears in the original bullet: \\textbf{...}, \\textit{...} and \\href{url}{text} stay as they are. Use no other LaTeX commands. Write & % $ # _ as \\& \\% \\$ \\# \\_.
5. A rewritten bullet may grow a little, at most about a third longer than the original (one printed line is about 110 characters); shorter is better. Rewrite any bullet a rephrase makes clearly better for this job; leave the rest out of bullet_rewrites.
6. tech_lines and skills_rows are comma-separated lists of the person's own tool names. Keep every existing item, spelled exactly as written (e.g. keep "React.js", "NumPy", "PouchDB"). You may only reorder items so the job's tools come first, and append a few items the job asks for that are in that entry's VERIFIED SKILLS (tech lines) or the VERIFIED INVENTORY (skills rows). Never replace a tool with a category name such as "Machine Learning", "Frontend" or "Full Stack". Only include a row or tech line in your output if you changed it.
7. bullet_priority lists every bullet ID, most relevant to this job first. Projects are printed in this order too (by their most relevant bullet); work experience stays in date order.
8. The result must fit on one page, with at most four bullets per job and three per project; rank every bullet in bullet_priority, most relevant to this job first, since that decides which stay. Entries and bullets marked (fact sheet) are the applicant's own further work, as true as the rest: rank them above the resume's own where they fit this job better, below where they don't. Put the least relevant bullets in drop_bullets and, if needed, the least relevant projects in drop_entries. Never drop work experience entries. Keep at least three bullets per entry (all of them when it has fewer): drop a whole project rather than leave one with two.
9. jd_keywords lists the 8-15 most important skills or terms in the job description, each with status "added" (you worked it in), "already_present", or "missing" (the resume has no verified evidence), and a short note.
10. summary: two sentences on what you emphasized and why.
11. Write like the person, not like a model. Match how their own bullets read: their kind of verbs, sentence length and punctuation, plain and specific. Prefer their own words to new ones. No em dashes. No clause tacked on to sound like impact (", ensuring ...", ", enabling ...", ", driving ..."). No stock resume words such as "spearheaded", "leveraged", "utilized", "orchestrated", "robust", "seamless", "cutting-edge", "innovative", "comprehensive", "pivotal", "fostered", "facilitated". Don't start bullets next to each other with the same verb. Edits that read as machine-written are sent back."""

SCHEMA = {
    "type": "object",
    "properties": {
        "bullet_rewrites": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["id", "text"], "additionalProperties": False}},
        "tech_lines": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["id", "text"], "additionalProperties": False}},
        "skills_rows": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["id", "text"], "additionalProperties": False}},
        "bullet_priority": {"type": "array", "items": {"type": "string"}},
        "drop_bullets": {"type": "array", "items": {"type": "string"}},
        "drop_entries": {"type": "array", "items": {"type": "string"}},
        "jd_keywords": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "status": {"type": "string", "enum": ["added", "already_present", "missing"]},
                "note": {"type": "string"}},
            "required": ["keyword", "status", "note"], "additionalProperties": False}},
        "summary": {"type": "string"},
    },
    "required": ["bullet_rewrites", "tech_lines", "skills_rows", "bullet_priority",
                 "drop_bullets", "drop_entries", "jd_keywords", "summary"],
    "additionalProperties": False,
}

# The rules that change with the strictness. INSTRUCTIONS has the "reword"
# ones. The truthfulness rules (1, 2, 6) are the same at every level, and
# resume_tex checks them in code.
REWORD_RULES = (
    """3. Keep what each bullet says the person did: the same work, scope, results and tools. Within that, you may rephrase a bullet freely: restructure the sentence, lead with the part that matters most for this job, pick different verbs, use the job description's terminology for things the bullet already describes (for example "REST endpoints" -> "RESTful APIs"), and name a verified skill for that entry explicitly. Shape each bullet you rewrite like a one-line STAR answer: the action, how it was done, then the result, with a result only where the original states one.""",
    """5. A rewritten bullet may grow a little, at most about a third longer than the original (one printed line is about 110 characters); shorter is better. Rewrite any bullet a rephrase makes clearly better for this job; leave the rest out of bullet_rewrites.""",
)
assert all(r in INSTRUCTIONS for r in REWORD_RULES)

# "none": bullets keep their words; the rest of the tailoring still runs.
NONE_RULES = (
    """3. Leave every bullet's wording as it is: bullet_rewrites must be empty. Tailor through the order, the tech lines and the skills rows.""",
    """5. Choose drop_bullets and drop_entries by relevance to this job; the wording itself doesn't change.""",
)

# "rewrite": the most freedom. A bullet may be rewritten from scratch and use
# facts from the rest of its entry (resume_tex checks it against the whole
# entry); the truthfulness rules don't change.
REWRITE_RULES = (
    """3. Each bullet must stay true to its entry: everything it says has to come from that entry's own bullets, tech line or verified skills. Within that, you may rewrite a bullet from scratch: say what matters most for this job, combine details from the entry's other bullets (and put the bullet you took them from in drop_bullets if it would repeat them), pick a different angle on the same work, and use the job description's terminology for things the entry already describes. Shape each bullet like a one-line STAR answer: the action, how it was done, then the result, with a result only where the entry states one. Never move work, tools or results from one entry to another.""",
    """5. A rewritten bullet may be up to about two thirds longer than the original (one printed line is about 110 characters), but a bullet that ends a few words into a new line is sent back, so aim for one or two full lines. Rewrite every bullet you can make clearly better for this job; leave the rest out of bullet_rewrites.""",
)
LEVEL_RULES = {"none": NONE_RULES, "rewrite": REWRITE_RULES}


def instructions(level: str = "reword", check: bool = True) -> str:
    """The tailoring rules for a strictness level (config.STRICTNESS), with
    or without the claim checks."""
    text = INSTRUCTIONS
    swaps = list(zip(REWORD_RULES, LEVEL_RULES.get(level, REWORD_RULES)))
    if not check:
        swaps += zip(CHECKED_RULES, OPEN_RULES)
    for old, new in swaps:
        text = text.replace(old, new)
    return text


# Without the claim checks the model may name what the work plainly
# involved; it still may not make anything up.
CHECKED_RULES = (
    """1. Truthfulness comes first. A skill may appear in an entry's bullets or tech line only if it is in that entry's VERIFIED SKILLS list. Skills-section rows may use only the VERIFIED INVENTORY. If the job wants something the resume doesn't support, report it as "missing" in jd_keywords and do not add it anywhere.""",
    """2. Never add or change numbers, metrics, company names, job titles, dates or URLs.""",
    """append a few items the job asks for that are in that entry's VERIFIED SKILLS (tech lines) or the VERIFIED INVENTORY (skills rows).""",
)
OPEN_RULES = (
    """1. Stay true to the person's work. The VERIFIED lists are what the resume shows for sure; beyond them you may name tools, skills and practices an entry's work plainly involved (a REST API means HTTP and JSON, a deployed web app means some hosting), in bullets, tech lines and skills rows. Never claim experience the resume gives no grounds for: report it as "missing" in jd_keywords instead.""",
    """2. Never invent metrics, company names, job titles, dates or URLs, and keep the person's numbers as they are.""",
    """append a few items the job asks for that the entry's work plainly involved (tech lines) or that the resume as a whole backs (skills rows).""",
)
assert all(r in INSTRUCTIONS for r in CHECKED_RULES)


@dataclass(frozen=True)
class Rules:
    """How one tailoring may change the resume: the strictness level
    (config.STRICTNESS), the user's banned phrases, and whether edits are held
    to the master resume's claims."""
    level: str = "reword"
    banned: tuple = ()
    check: bool = True


def check_claims() -> bool:
    return bool(_tailoring("check_claims"))


BackendError = ai.BackendError


def _resume_block(res: rt.Resume) -> str:
    return "RESUME (edit by ID)\n\n" + rt.describe(res)


MAX_INSTRUCTION = 4000


def clean_instruction(text) -> str:
    """The applicant's request, its paragraphs kept, within MAX_INSTRUCTION."""
    lines = "\n".join(" ".join(line.split()) for line in str(text or "").splitlines())
    return re.sub(r"\n{3,}", "\n\n", lines).strip()[:MAX_INSTRUCTION]


def read_resume(master_tex: str, job: dict, confirmed=()) -> rt.Resume:
    """The master resume as tailoring checks against it: plus the skills the
    person confirmed under Review skills, and what they say about their own
    work in their request for this version (their word, like a confirmed
    skill: the skills it names, where an entry shows that kind of work, and
    its words)."""
    said = clean_instruction(job.get("instruction"))
    res = rt.confirm(rt.parse(master_tex), [*confirmed, *sorted(rt.find_skills(said))])
    res.vocabulary |= rt.words(said)
    return res


def _job_block(job: dict, jd: str) -> str:
    block = f"JOB: {job.get('title', '')} at {job.get('company', '')}\n\nJOB DESCRIPTION\n{jd.strip()}"
    note = clean_instruction(job.get("instruction"))
    if note:
        # Every backend's prompt (manual paste included) ends with this block.
        block += ("\n\nTHE APPLICANT'S REQUEST FOR THIS VERSION\n" + note +
                  "\nFollow it as far as the rules above allow. What the applicant says here about their own work "
                  "counts as backed, like the resume: use it in the entries that show that work. It can't add "
                  "entries the resume doesn't have. The rules win when they conflict; if that happens, say so in "
                  "summary, naming what to add to the master resume.")
    return block


# --------------------------------------------------------------------------
# Backends

def _claude_json(prompt: str, schema: dict) -> tuple[dict, str]:
    """One structured answer from the `claude` CLI on the user's subscription login."""
    exe = shutil.which("claude")
    if not exe:
        raise BackendError("The claude command isn't installed.")
    runner_dir().mkdir(parents=True, exist_ok=True)
    sec = config.store().section("claude")
    cmd = [exe, "-p", "--output-format", "json", "--no-session-persistence",
           "--tools", "", "--strict-mcp-config",
           "--system-prompt", "You return only the requested JSON.",
           "--json-schema", json.dumps(schema)]
    if sec["model"]:
        cmd += ["--model", sec["model"]]
    if sec["effort"]:
        cmd += ["--effort", sec["effort"]]
    try:
        proc = subprocess.run(cmd, input=prompt, cwd=runner_dir(), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired as e:
        raise BackendError("Claude took too long. Try again.") from e
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise BackendError((proc.stderr or proc.stdout or "claude failed").strip()[:300]) from e
    if out.get("is_error"):
        msg = str(out.get("result", "claude failed"))
        if re.search(r"auth|login|OAuth", msg, re.I):
            msg += " Run `claude auth login` in a terminal once, then try again."
        elif sec["model"] and re.search(r"selected model|unrecognized_model", msg + (proc.stderr or ""), re.I):
            msg = (f"Your Claude login can't use the {CLAUDE_NAMES.get(sec['model'], sec['model'])} model. "
                   "Pick another in Settings, under AI models.")
        raise BackendError(msg[:400])
    parsed = out.get("structured_output")
    if parsed is None:
        try:
            parsed = json.loads(out.get("result", ""))
        except json.JSONDecodeError as e:
            raise BackendError("Claude returned no structured output.") from e
    model = next(iter(out.get("modelUsage") or {}), "claude")
    return parsed, model


# Thinking efforts offered for the ChatGPT login. Codex also lists "ultra",
# which hands parts of a task to helper agents; one structured answer has
# nothing to hand off, so it's left out.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
CATALOG_TTL = 600
_catalog: tuple[float, list[dict]] = (0.0, [])
_catalog_lock = threading.Lock()


def codex_models() -> list[dict]:
    """The models Codex offers on the user's login, in Codex's own order:
    [{id, label, efforts, default_effort}]. Read from `codex debug models`,
    so new models show up without an Onward update; [] when Codex isn't
    installed or can't say."""
    global _catalog
    with _catalog_lock:
        at, models = _catalog
        if time.time() - at < CATALOG_TTL:
            return models
        models = []
        exe = shutil.which("codex")
        if exe:
            try:
                proc = subprocess.run([exe, "debug", "models"], capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=20)
                models = _parse_catalog(proc.stdout) if proc.returncode == 0 else []
            except (subprocess.TimeoutExpired, OSError):
                models = []
        # A failed read is retried sooner than a good one is refreshed.
        _catalog = (time.time() - (CATALOG_TTL - 60 if not models else 0), models)
        return models


def _parse_catalog(text: str) -> list[dict]:
    try:
        raw = json.loads(text).get("models") or []
    except (json.JSONDecodeError, AttributeError):
        return []
    out = []
    for m in sorted((m for m in raw if isinstance(m, dict)), key=lambda m: m.get("priority") or 0):
        slug = str(m.get("slug") or "")
        if m.get("visibility") != "list" or not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", slug):
            continue
        efforts = [e for e in EFFORTS if e in {str(x.get("effort")) for x in m.get("supported_reasoning_levels") or []
                                               if isinstance(x, dict)}]
        default = str(m.get("default_reasoning_level") or "")
        out.append({"id": slug, "label": str(m.get("display_name") or slug)[:100], "efforts": efforts,
                    "default_effort": default if default in efforts else ""})
    return out


def codex_choice(model: str | None = None, effort: str | None = None) -> tuple[str, str]:
    """The saved (or given) ChatGPT model and effort, checked against what
    Codex offers now. Codex runs an effort a model doesn't support without
    complaint, so it's checked here."""
    sec = config.store().section("chatgpt")
    model = sec["model"] if model is None else model
    effort = sec["effort"] if effort is None else effort
    if effort and effort not in EFFORTS:
        raise BackendError(f"Unknown thinking effort {effort}. Pick another in Settings, under AI models.")
    if not model and not effort:
        return "", ""
    catalog = codex_models()
    if not catalog:
        return model, effort         # Codex can't list its models; it will say if one is wrong
    if model:
        found = next((m for m in catalog if m["id"] == model), None)
        if not found:
            raise BackendError(f"ChatGPT doesn't offer {model} any more. Pick another model in Settings, under AI models.")
        if effort and effort not in found["efforts"]:
            raise BackendError(f"{found['label']} doesn't take {effort} thinking effort. "
                               "Pick another in Settings, under AI models.")
    return model, effort


# The Claude login's models: the claude CLI's aliases, which follow its
# latest models (it has no command that lists them). A plan without a model
# may get another, so the run records the model that actually answered.
CLAUDE_NAMES = {"fable": "Fable", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku"}
CLAUDE_MODELS = [{"id": k, "label": f"{v} (latest)", "efforts": list(EFFORTS), "default_effort": ""}
                 for k, v in CLAUDE_NAMES.items()]


def _codex_ran(stderr: str) -> str:
    """What Codex says it ran, from the header it prints: "chatgpt (model, effort)"."""
    model = re.search(r"^model:\s*(\S+)", stderr, re.M)
    effort = re.search(r"^reasoning effort:\s*(\S+)", stderr, re.M)
    if not model:
        return "chatgpt (codex)"
    return f"chatgpt ({model.group(1)}{', ' + effort.group(1) if effort else ''})"


def _codex_json(prompt: str, schema: dict) -> tuple[dict, str]:
    """One structured answer from OpenAI's `codex` CLI on the user's ChatGPT login."""
    exe = shutil.which("codex")
    if not exe:
        raise BackendError("The codex command isn't installed. Run: npm install -g @openai/codex")
    model, effort = codex_choice()
    runner_dir().mkdir(parents=True, exist_ok=True)
    # Per-run file names: tailoring and drafting can run at the same time.
    stem = f"codex-{os.getpid()}-{time.time_ns()}"
    schema_path = runner_dir() / f"{stem}.schema.json"
    out_path = runner_dir() / f"{stem}.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    prompt = "Do not run any commands or read any files. Everything you need is below.\n\n" + prompt
    cmd = [exe, "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
           "-s", "read-only", "-C", str(runner_dir()), "--color", "never",
           "--output-schema", str(schema_path), "-o", str(out_path)]
    if model:
        cmd += ["-m", model]
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    cmd.append("-")
    try:
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=900)
        except subprocess.TimeoutExpired as e:
            raise BackendError("ChatGPT took too long. Try again.") from e
        if proc.returncode != 0 or not out_path.exists():
            msg = (proc.stderr or proc.stdout or "codex failed").strip()
            if re.search(r"log ?in|auth|401|unauthori", msg, re.I):
                msg = "Not signed in to ChatGPT. Run `codex login` in a terminal once, then try again."
            raise BackendError(msg[-400:])
        try:
            return json.loads(out_path.read_text(encoding="utf-8")), _codex_ran(proc.stderr or "")
        except json.JSONDecodeError as e:
            raise BackendError("ChatGPT returned malformed output. Try again.") from e
    finally:
        out_path.unlink(missing_ok=True)
        schema_path.unlink(missing_ok=True)



def ask(backend: str, system: str, user: str, schema: dict, purpose: str,
        questions: int = 0, cache_system: bool = False) -> tuple[dict, str, float | None]:
    """One structured answer from any backend but manual: (JSON, model,
    cost in dollars, None for the subscription logins). `system` is what
    stays the same across jobs (rules, resume), `user` the rest; the CLIs
    get both as one prompt."""
    if backend == "chatgpt":
        raw, model = _codex_json(system + "\n\n" + user, schema)
        return raw, model, None
    if backend == "claude-code":
        raw, model = _claude_json(system + "\n\n" + user, schema)
        return raw, model, None
    provider = pricing.API_BACKENDS.get(backend)
    if not provider:
        raise BackendError(f"Unknown backend {backend}")
    raw, model, info = ai.call_json(provider, system, user, schema, purpose, questions, cache_system)
    return raw, model, info["cost"]


def estimate(backend: str, system: str, user: str, purpose: str, questions: int = 0, exact: bool = True) -> dict | None:
    """What a call would cost on a paid API; None for the subscription
    logins and manual paste, which cost nothing extra."""
    provider = pricing.API_BACKENDS.get(backend)
    return ai.estimate(provider, system, user, purpose, questions, exact) if provider else None


def tailor_prompt(res: rt.Resume, job: dict, jd: str, level: str = "reword",
                  check: bool = True) -> tuple[str, str]:
    """(system, user). The rules and resume are the same for every job, so
    the API backends cache them; only the job block varies."""
    return instructions(level, check) + "\n\n" + _resume_block(res), _job_block(job, jd)


def estimate_tailor(master_tex: str, job: dict, jd: str, backend: str, level: str | None = None,
                    exact: bool = True, confirmed=()) -> dict | None:
    system, user = tailor_prompt(read_resume(master_tex, job, confirmed), job, jd, level or strictness(),
                                 check_claims())
    return estimate(backend, system, user, "tailor", exact=exact)


def build_manual_prompt(master_tex: str, job: dict, jd: str, level: str | None = None, confirmed=()) -> str:
    """Everything a chat model needs, for pasting into ChatGPT or Claude by hand."""
    res = read_resume(master_tex, job, confirmed)
    return "\n\n".join([
        instructions(level or strictness(), check_claims()),
        "Reply with a single JSON object and nothing else, matching this JSON Schema:\n"
        + json.dumps(SCHEMA, indent=1),
        _resume_block(res),
        _job_block(job, jd),
    ])


def parse_manual_answer(text: str) -> dict:
    """Accept the JSON with or without code fences or chatter around it."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise BackendError("Couldn't find a JSON object in the pasted answer.")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise BackendError(f"The pasted answer isn't valid JSON ({e.msg} at character {e.pos}).") from e
    if not isinstance(data, dict):
        raise BackendError("The pasted answer isn't a JSON object.")
    for key, val in SCHEMA["properties"].items():
        data.setdefault(key, [] if val["type"] == "array" else "")
    return data


BACKENDS = tuple(b for b in config.TAILOR_BACKENDS if b != "manual")


# --------------------------------------------------------------------------
# Writing voice for drafted answers
#
# Drafts should read like the applicant wrote them: a person talking to a
# person, not a model summarizing a resume. The prompt sets the voice (the
# tone, the applicant's own writing samples, the banned phrases from
# Settings), and style_problems() checks the result in code, because models
# drift back to the same stock phrases whatever the prompt says.

ABOUT_MAX = 4000


def about_path() -> Path:
    return config.store().path_for("resume_dir") / "about_me.txt"


TONE_RULES = {
    "casual": "- Sound like a real new-grad student talking to a person: plain everyday words, contractions (I'm, I've, it's), a mix of short and longer sentences, and a bit of real personality. It should read like the applicant typed it, not like a cover letter template.",
    "neutral": "- Sound like a thoughtful applicant writing to a person: plain everyday words, some contractions, a mix of short and longer sentences. Friendly and direct, neither chatty nor stiff. It should read like the applicant typed it, not like a cover letter template.",
    "formal": "- Write in a polished, professional register: complete sentences, few contractions, courteous and precise. Still use plain words instead of corporate buzzwords, and keep it specific and personal. It should read like the applicant wrote it, not like a cover letter template.",
}

VOICE_BODY = """- Lead with something honest and personal from ABOUT THE APPLICANT when it fits the question (an interest, something they care about, how they use the product). Never invent interests, hobbies, feelings or stories that aren't written there.
- Mention at most one or two things from the resume, and only where they answer the question, described the way you'd tell a friend what you built ("I built a chatbot that remembers past conversations"), not as a resume line full of tool names. Name a tool only if the question or the job really cares about it.
- Don't restate the company's mission or the posting back to them, and don't explain what their product does."""

# The wording the default banned-phrase list is written into the prompt
# with; an edited list is listed phrase by phrase instead.
DEFAULT_BANNED_RULES = """- Never use these or anything like them: "appeals to me", "resonates", "aligns with", "connects to", "makes that connection", "concrete", "I would bring", "I'd bring", "leverage", "passionate", "thrilled", "excited to", "eager to", "deeply", "at the intersection of", "drive impact", "cutting-edge", "seamless", "robust", "fast-paced", "dynamic", "I am confident", "not only ... but also", "In [project], I built", "This experience taught me", "Ultimately".
- Also avoid cover-letter stock lines: "offers the chance", "the opportunity to", "a big reason", "this particular role", "I'd like to apply", "matters to me", "take an idea from", "through to", "grounded", "meaningful", "impactful", "contribute to"."""

VOICE_END = """- No em dashes, no semicolons, no lists of three adjectives or three items in a row.
- Don't repeat a word (other than small common words) and use the company or product name at most twice.

Example of the difference (different company, same idea):
Too machine-written: "Notion's mission resonates with me because it empowers teams to collaborate seamlessly. In my capstone project, I leveraged React and Node.js to build a robust task-management platform, and I would bring this full-stack experience to Notion's dynamic team."
Sounds like a person: "I've used Notion for every class since my first semester, so I already have opinions about what it gets right. For my capstone I built a small task tracker in React and Node, and the hardest part was making edits feel instant when two people were working at once. I'd love to work on that kind of problem with people who care about it as much as I do.\""""

MAX_BANNED_IN_PROMPT = 150


def voice_settings() -> dict:
    return config.store().section("voice")


def voice_rules(v: dict | None = None) -> str:
    """The "How to write it" block for the user's tone and banned phrases."""
    v = v or voice_settings()
    banned = list(v["banned_phrases"])
    if tuple(banned) == voice.DEFAULT_BANNED:
        banned_rules = DEFAULT_BANNED_RULES
    elif banned:
        listed = ", ".join(f'"{p}"' for p in banned[:MAX_BANNED_IN_PROMPT])
        banned_rules = (f"- Never use these phrases or anything like them: {listed}. "
                        '("*" stands for any word ending and "..." for any words in between.)')
    else:
        banned_rules = ""
    tone = v["tone"] if v["tone"] in TONE_RULES else "casual"
    end = VOICE_END
    if tone != "casual":
        end += "\n(The example shows the idea, not the tone. Keep to the tone described at the top.)"
    return "How to write it\n" + "\n".join(x for x in (TONE_RULES[tone], VOICE_BODY, banned_rules, end) if x)


# The default voice, as the prompts used it before voice settings existed.
VOICE = voice_rules({"tone": "casual", "banned_phrases": list(voice.DEFAULT_BANNED), "samples": []})


def samples_block(v: dict | None = None) -> str:
    """The applicant's own writing, for drafts to imitate."""
    samples = [x.strip() for x in (v or voice_settings())["samples"] if x.strip()]
    if not samples:
        return ""
    body = "\n\n".join(f"--- sample {i} ---\n{x[:voice.SAMPLE_MAX]}" for i, x in enumerate(samples, 1))
    return ("WRITING SAMPLES (the applicant wrote these. Match how they write: sentence length, word choice, "
            "how formal they are, how they open and close. Don't copy their sentences, and use a fact from them "
            "only if the resume or ABOUT THE APPLICANT says it too)\n" + body)


_STOP = set("""a an the and or but if of to in on at for with from by as is are was were be been being it its
this that these those i i'm i've i'd me my we our you your they their them he she his her there here what
which who whom how why when where so than then too very just also not no can could would should will
about into over under after before while because more most some any all each other such only own same
both few many much do does did done have has had having get got make made one two like""".split())


def about_me() -> str:
    """The applicant's own notes on interests and motivations, if they wrote any."""
    try:
        return about_path().read_text(encoding="utf-8").strip()[:ABOUT_MAX]
    except OSError:
        return ""


def save_about_me(text: str) -> None:
    about_path().parent.mkdir(parents=True, exist_ok=True)
    about_path().write_text(text.strip()[:ABOUT_MAX] + "\n", encoding="utf-8")


def style_problems(text: str, company: str = "", banned: list[str] | tuple[str, ...] | None = None) -> list[str]:
    """Concrete reasons a draft reads as machine-written, for a rewrite pass.
    `banned` defaults to the user's banned-phrase list in Settings."""
    if banned is None:
        banned = voice_settings()["banned_phrases"]
    problems = []
    for rx in voice.compile_phrases(tuple(banned)):
        m = rx.search(text)
        if m:
            problems.append(f'uses the stock phrase "{m.group(0)}"')
    if "—" in text or " -- " in text:
        problems.append("uses an em dash")
    if ";" in text:
        problems.append("uses a semicolon")
    words = re.findall(r"[a-z][a-z'-]+", text.lower())
    counts: dict[str, int] = {}
    for w in words:
        if w not in _STOP and len(w) > 3:
            counts[w] = counts.get(w, 0) + 1
    # Repeated stems too: "connection" / "connects", "build" / "building".
    stems: dict[str, set[str]] = {}
    for w in counts:
        stems.setdefault(w[:6], set()).add(w)
    company_words = {w.lower() for w in re.findall(r"[A-Za-z]+", company)}
    # People repeat short words ("want", "love") naturally. What reads as
    # machine-written is long formal words echoing each other ("capabilities",
    # "experience", "connection" / "connects"), so those may appear once and
    # everything else twice. Names (ElevenMusic, React) may appear twice too.
    def limit(w: str) -> int:
        return 1 if len(w) >= 7 else 2
    repeated = sorted({w for w, c in counts.items() if c > limit(w) and w not in company_words}
                      | {"/".join(sorted(v)) for k, v in stems.items()
                         if len(v) > 1 and max(map(len, v)) >= 7 and not (v & company_words)})
    if repeated:
        problems.append("repeats words: " + ", ".join(repeated[:6]))
    if company:
        n = len(re.findall(re.escape(company), text, re.I))
        if n > 2:
            problems.append(f"names {company} {n} times")
    return problems


def rewrite_instructions(v: dict | None = None) -> str:
    return ("Rewrite each answer below so it sounds like the applicant wrote it themselves. Keep every fact exactly "
            "as it is and add no new ones. Fix the listed problems and follow the voice rules. Keep each answer "
            "about the same length and within its character limit.\n\n" + voice_rules(v)
            + '\n\nReturn one entry per id with the rewritten text in "answer".')


REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"answers": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "answer": {"type": "string"}},
        "required": ["id", "answer"], "additionalProperties": False}}},
    "required": ["answers"],
    "additionalProperties": False,
}


def humanize(items: list[dict], backend: str, company: str = "", costs: list[float] | None = None) -> list[dict]:
    """One rewrite pass over answers that still read as machine-written.

    items: [{"id", "answer", "max_chars"}]. Returns the same list with
    rewritten answers where the rewrite came back cleaner, plus "style_notes"
    listing whatever problems remain so the dashboard can point them out.
    A paid rewrite's cost is appended to `costs`."""
    v = voice_settings()
    banned = v["banned_phrases"]
    flagged = [dict(i, problems=style_problems(i["answer"], company, banned)) for i in items]
    todo = [i for i in flagged if i["problems"]]
    if todo:
        lines = []
        for i in todo:
            limit = f" (at most {i['max_chars']} characters)" if i.get("max_chars") else ""
            lines.append(f"[id {i['id']}]{limit}\nproblems: {'; '.join(i['problems'])}\nanswer: {i['answer']}")
        material = "\n\n".join(x for x in (samples_block(v), "ANSWERS\n" + "\n\n".join(lines)) if x)
        try:
            raw, _, cost = ask(backend, rewrite_instructions(v), material, REWRITE_SCHEMA, "rewrite")
            if cost is not None and costs is not None:
                costs.append(cost)
        except BackendError:
            # The budget ran out or the model failed: keep the first draft and its notes.
            raw = {}
        better = {str(a.get("id")): str(a.get("answer", "")).strip() for a in (raw or {}).get("answers", [])}
        for i in todo:
            new = _fit(better.get(str(i["id"]), ""), int(i.get("max_chars") or 0))
            # Keep the rewrite only if it really is cleaner.
            if new and len(style_problems(new, company, banned)) < len(i["problems"]):
                i["answer"] = new
                i["problems"] = style_problems(new, company, banned)
    return [dict({k: v for k, v in i.items() if k != "problems"}, style_notes=i["problems"]) for i in flagged]


# --------------------------------------------------------------------------
# Drafting answers to application questions ("Why this company?")

# What both drafting prompts say about reading the question: a question about
# habits ("day to day") wants a routine, not the nearest project write-up.
QUESTION_KINDS = """Before writing, work out what kind of answer the question wants, and give that kind:
   - A habit or way of working ("day to day", "how do you usually", "your approach to", "how do you stay current"): what the applicant does regularly and how, in the present tense, with the tools and steps of that routine. A project may appear as a one-line example of the habit, never as the answer itself.
   - A past example ("tell us about a time", "describe a project"): one specific story: the situation, what they did, how it went.
   - Why this company or this role: their honest reasons.
   - An opinion or a preference: their view and why they hold it.
   A question that asks several of these gets a short answer to each part. When the resume and ABOUT THE APPLICANT don't say enough for that kind of answer, give what they do support in a shorter answer instead of filling it with unrelated projects, and never say what the applicant hasn't done, doesn't have or can't claim.
   Bring in the company or the role only when the question asks about them, and never end with a line about what the applicant would do, learn or build at the company unless the question asks."""

DRAFT_RULES = """You draft one answer to a question on a job application, written as the applicant, using their resume and the job posting. The applicant reviews and edits it before sending.

Rules
1. Use only facts from the resume and from ABOUT THE APPLICANT. Never invent employers, projects, numbers, dates, skills, interests or personal details. If neither supports something the question asks about, answer honestly with what they do show instead of making something up.
2. Answer the question that was asked. {kinds}
3. First person. No greeting, sign-off or headings, and no bullet points unless the question asks for a list.
4. Length: {length}.
5. Put only the answer text in the "answer" field.

"""

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

# Every backend but manual paste, which the caller handles. Same list as
# config.DRAFT_BACKENDS; the API ones are held to their monthly budget.
DRAFT_BACKENDS = config.DRAFT_BACKENDS


def _entries_block(parts: list[str], entries: str) -> None:
    """The applicant's own entries (jobs, projects) for this job, as edited in
    the extension: only the ones that share terms with the posting are sent."""
    entries = (entries or "").strip()[:6000]
    if entries:
        parts.append("THE APPLICANT'S OWN ENTRIES FOR THIS JOB (their wording; where these differ from the resume, "
                     "use these)\n" + entries)


def _about_and_samples(parts: list[str], v: dict) -> None:
    about = about_me()
    if about:
        parts.append(f"ABOUT THE APPLICANT (their own words: interests and motivations you may use)\n{about}")
    samples = samples_block(v)
    if samples:
        parts.append(samples)


def draft_prompt(master_tex: str, job: dict, question: str, context: str, jd: str, max_chars: int = 0,
                 note: str = "", previous: str = "", entries: str = "") -> tuple[str, str]:
    """(instructions, material) for drafting one answer. With `note`, the
    applicant's request for a new version of `previous` ("use my Y project,
    not X")."""
    v = voice_settings()
    length = (f"at most {max_chars} characters, including spaces; aim for about {int(max_chars * 0.8)}"
              if max_chars else "about 90 to 130 words in one or two short paragraphs, or shorter if the question asks for something brief")
    parts = [f"QUESTION\n{question.strip()}"]
    extra = context.strip()
    if extra and extra != question.strip():
        parts.append(f"TEXT AROUND THE QUESTION ON THE FORM\n{extra}")
    parts.append(f"JOB: {job.get('title', '')} at {job.get('company', '')}")
    if jd.strip():
        parts.append(f"JOB POSTING\n{jd.strip()[:20000]}")
    parts.append(f"RESUME\n{rt.plain(master_tex)}")
    _entries_block(parts, entries)
    _about_and_samples(parts, v)
    note = clean_instruction(note)
    if note:
        if previous.strip():
            parts.append(f"THE EARLIER DRAFT\n{previous.strip()[:20000]}")
        parts.append("THE APPLICANT'S REQUEST FOR THIS ANSWER\n" + note + "\nWrite the answer again so it does this. "
                     "The rules above still win: if it asks for something the resume and ABOUT THE APPLICANT don't "
                     "support, use what they do support and invent nothing.")
    # .replace, not .format: only {length} is a placeholder, and the voice
    # rules (the user's own phrases among them) may contain braces.
    return DRAFT_RULES.replace("{length}", length).replace("{kinds}", QUESTION_KINDS) + voice_rules(v), "\n\n".join(parts)


def build_draft_prompt(master_tex: str, job: dict, question: str, context: str, jd: str, max_chars: int = 0,
                       note: str = "", previous: str = "", entries: str = "") -> str:
    """The whole drafting prompt, for pasting into a chat by hand."""
    instructions, material = draft_prompt(master_tex, job, question, context, jd, max_chars, note, previous, entries)
    return instructions.replace('Put only the answer text in the "answer" field.', "Reply with only the answer text.") + "\n\n" + material


def estimate_draft(master_tex: str, job: dict, question: str, context: str, jd: str, backend: str,
                   max_chars: int = 0, exact: bool = True, note: str = "", previous: str = "",
                   entries: str = "") -> dict | None:
    instructions, material = draft_prompt(master_tex, job, question, context, jd, max_chars, note, previous, entries)
    return estimate(backend, instructions, material, "draft", exact=exact)


def _fit(text: str, max_chars: int) -> str:
    text = text.strip()
    if not max_chars or len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(".\n"))
    return (cut[:end + 1] if end > max_chars // 2 else cut.rsplit(" ", 1)[0]).strip()


ANSWERS_RULES = """You fill in the remaining questions on one job application, as the applicant, using their resume, their profile and the job posting. The applicant reviews every answer before submitting.

Rules
1. Never invent experience, employers, projects, numbers, skills, awards, dates, interests or personal details. Use only what the resume, the profile and ABOUT THE APPLICANT say, plus plain inferences from them (someone living in Boston is not a California resident; a May 2026 graduate can start around June 2026; someone whose resume doesn't list this company has not worked there).
2. If the answer depends on something only the applicant knows and the resume and profile don't settle it (offer deadlines, family ties to employees, consent to recordings or data sharing, legal declarations), return an empty answer so the applicant answers it.
3. Choice questions: "answer" must be copied exactly from that question's options, or empty. For "multi" questions put every option that applies in "choices" (exact option text) and leave "answer" empty. Otherwise leave "choices" empty.
4. Short text questions get short answers (a date, a year, a name, a few words). Open questions get a specific answer: about 80 to 150 words unless the question asks for less, and never more than its character limit. First person; no greeting, no headings. Write open answers by the voice rules below. {kinds}
5. Return one entry per question id.

"""

ANSWERS_SCHEMA = {
    "type": "object",
    "properties": {"answers": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "answer": {"type": "string"},
                       "choices": {"type": "array", "items": {"type": "string"}}},
        "required": ["id", "answer", "choices"], "additionalProperties": False}}},
    "required": ["answers"],
    "additionalProperties": False,
}

PROFILE_FOR_ANSWERS = ("firstName", "lastName", "preferredName", "city", "state", "country", "company", "currentTitle",
                       "school", "degree", "major", "gradDate", "languages", "linkedin", "github", "portfolio")


def answers_prompt(master_tex: str, job: dict, profile: dict, questions: list[dict], jd: str,
                   entries: str = "") -> tuple[str, str]:
    """(instructions, material) for answering several questions at once."""
    v = voice_settings()
    prof = "\n".join(f"{k}: {str(profile.get(k, '')).strip()}" for k in PROFILE_FOR_ANSWERS if str(profile.get(k, "")).strip())
    qs = []
    for q in questions:
        line = f"[id {q['id']}] ({q['kind']}"
        if q.get("max_chars"):
            line += f", at most {q['max_chars']} characters"
        line += f") {q['question'].strip()}"
        if q.get("context") and q["context"].strip() != q["question"].strip():
            line += f"\n    text around it: {q['context'].strip()[:600]}"
        if q.get("options"):
            line += "\n    options: " + " | ".join(q["options"])
        qs.append(line)
    parts = [f"JOB: {job.get('title', '')} at {job.get('company', '')}"]
    if jd.strip():
        parts.append(f"JOB POSTING\n{jd.strip()[:15000]}")
    parts += [f"PROFILE\n{prof}", f"RESUME\n{rt.plain(master_tex)}"]
    _entries_block(parts, entries)
    _about_and_samples(parts, v)
    parts.append("QUESTIONS\n" + "\n".join(qs))
    return ANSWERS_RULES.replace("{kinds}", QUESTION_KINDS) + voice_rules(v), "\n\n".join(parts)


def _total(costs: list[float]) -> float | None:
    return round(sum(costs), 4) if costs else None


def answer_questions(master_tex: str, job: dict, profile: dict, questions: list[dict], jd: str, backend: str,
                     entries: str = "") -> dict:
    """Answers for several application questions in one model call."""
    if backend not in DRAFT_BACKENDS:
        raise BackendError(f"Answering questions can't use {backend}.")
    if not questions:
        return {"answers": [], "backend": backend, "model": "", "seconds": 0, "cost": None}
    started = time.time()
    instructions, material = answers_prompt(master_tex, job, profile, questions, jd, entries)
    raw, model, cost = ask(backend, instructions, material, ANSWERS_SCHEMA, "answers", questions=len(questions))
    costs = [cost] if cost is not None else []
    byid = {str(q["id"]): q for q in questions}
    out = []
    for a in (raw or {}).get("answers", []):
        q = byid.get(str(a.get("id")))
        if not q:
            continue
        opts = q.get("options") or []
        answer = str(a.get("answer", "")).strip()
        choices = [c for c in (a.get("choices") or []) if c in opts]
        # A choice the model made up is no answer at all.
        if opts and answer not in opts:
            answer = ""
        if not opts:
            answer = _fit(answer, int(q.get("max_chars") or 0))
        if answer or choices:
            out.append({"id": str(q["id"]), "answer": answer, "choices": choices})
    # Open text answers get the voice check; short and choice answers don't need it.
    open_ids = {str(q["id"]) for q in questions if not q.get("options")}
    long = [{"id": a["id"], "answer": a["answer"], "max_chars": byid[a["id"]].get("max_chars")}
            for a in out if a["id"] in open_ids and len(a["answer"]) > 120]
    if long:
        fixed = {i["id"]: i for i in humanize(long, backend, job.get("company", ""), costs)}
        for a in out:
            if a["id"] in fixed:
                a["answer"] = fixed[a["id"]]["answer"]
                a["style_notes"] = fixed[a["id"]]["style_notes"]
    return {"answers": out, "backend": backend, "model": model, "seconds": round(time.time() - started, 1),
            "cost": _total(costs)}


def draft_answer(master_tex: str, job: dict, question: str, context: str, jd: str,
                 backend: str, max_chars: int = 0, note: str = "", previous: str = "", entries: str = "") -> dict:
    if backend not in DRAFT_BACKENDS:
        raise BackendError(f"Drafting can't use {backend}.")
    if not question.strip():
        raise BackendError("No question to answer.")
    started = time.time()
    instructions, material = draft_prompt(master_tex, job, question, context, jd, max_chars, note, previous, entries)
    raw, model, cost = ask(backend, instructions, material, DRAFT_SCHEMA, "draft")
    costs = [cost] if cost is not None else []
    answer = _fit(str((raw or {}).get("answer", "")), max_chars)
    if not answer:
        raise BackendError("The model returned an empty answer. Try again.")
    checked = humanize([{"id": "1", "answer": answer, "max_chars": max_chars}], backend, job.get("company", ""), costs)[0]
    return {"answer": checked["answer"], "style_notes": checked["style_notes"], "backend": backend,
            "model": model, "seconds": round(time.time() - started, 1), "cost": _total(costs)}


_LOGIN_CACHE: dict[str, tuple[float, bool]] = {}


def _signed_in(name: str, cmd: list[str], check) -> bool:
    """Whether a subscription CLI is installed and signed in; cached 60s
    because each check spawns a process."""
    hit = _LOGIN_CACHE.get(name)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    ok = False
    exe = shutil.which(cmd[0])
    if exe:
        try:
            proc = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=20)
            ok = check(proc)
        except (subprocess.TimeoutExpired, OSError):
            ok = False
    _LOGIN_CACHE[name] = (time.time(), ok)
    return ok


def _claude_logged_in(proc) -> bool:
    try:
        return bool(json.loads(proc.stdout).get("loggedIn"))
    except json.JSONDecodeError:
        return False


def backend_status() -> dict:
    return {"claude_code": _signed_in("claude", ["claude", "auth", "status"], _claude_logged_in),
            "chatgpt": _signed_in("codex", ["codex", "login", "status"],
                                  lambda p: p.returncode == 0 and "logged in" in (p.stdout + p.stderr).lower()),
            # A key saved in Settings or set in the environment.
            "openai_api": ai.key_status("openai")["saved"],
            "claude_api": ai.key_status("anthropic")["saved"]}


# --------------------------------------------------------------------------
# Pipeline

def verify_keywords(claimed: list[dict], master_tex: str, final_tex: str) -> list[dict]:
    """The model's keyword list, with each status recomputed from the actual
    text: a model saying it "added" something doesn't make it so."""
    before = rt.plain(master_tex).lower()
    after = rt.plain(final_tex).lower()
    before_sk = rt.with_implied(rt.find_skills(rt.plain(master_tex)))
    after_sk = rt.with_implied(rt.find_skills(rt.plain(final_tex)))
    out, seen = [], set()
    for k in claimed:
        kw = str(k.get("keyword", "")).strip()
        if not kw or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        skills = rt.find_skills(kw)
        if skills:
            in_before, in_after = skills <= before_sk, skills <= after_sk
        else:
            pat = re.compile(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])")
            in_before, in_after = bool(pat.search(before)), bool(pat.search(after))
        if in_after:
            status = "already_present" if in_before else "added"
        else:
            status = "cut" if in_before else "missing"
        out.append({"keyword": kw, "status": status,
                    "note": "" if status == k.get("status") else f"model said {k.get('status')}"})
    return out


# --------------------------------------------------------------------------
# Line fit: an edit that runs onto an extra line holding only a few words
# ("...Net-" / "working, Operating Systems") wastes the line. Lists drop the
# items they added, last first; bullets are rewritten once to a length that
# ends near a line's end; whatever still spills keeps the master's wording.

MIN_FILL = 0.7          # the least share of the width a wrapped piece's last line may hold
SPILLED = "would leave a few words on a line of their own"

REFIT_RULES = """You adjust resume bullets so each one ends close to the end of a printed line, instead of spilling a few words onto a line of their own.

Each bullet comes with the applicant's ORIGINAL wording, the CURRENT tailored version, and a TARGET length: how many characters it should print (letters, spaces and punctuation, not LaTeX commands).

Rules
1. Write a version of CURRENT whose printed length is inside TARGET.
2. Keep its meaning and the job keywords it uses. To shorten, cut filler and repeated words. To lengthen, use only wording ORIGINAL or CURRENT already supports.
3. Never add a skill, tool, number, date, employer or claim that isn't in ORIGINAL or CURRENT.
4. Keep the LaTeX commands CURRENT uses (\\textbf{...}, \\%, and so on) and add none.
5. Return every bullet you were given, by its id.
"""

REFIT_SCHEMA = {
    "type": "object",
    "properties": {"bullets": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
        "required": ["id", "text"], "additionalProperties": False}}},
    "required": ["bullets"],
    "additionalProperties": False,
}


def spills(fit: rt.Fit | None) -> bool:
    """A wrapped piece whose last line holds too little (unmeasured: no)."""
    return bool(fit and fit.lines > 1 and fit.fill < MIN_FILL)


def spilled(state: dict, layout: dict) -> dict[str, rt.Fit]:
    """The edited pieces whose last line holds too little."""
    out = {}
    for mid in [*state["rewrites"], *state["tech"], *state["rows"]]:
        fit = rt.line_fit(layout, mid)
        if spills(fit):
            out[mid] = fit
    return out


def _per_line(text: str, fit: rt.Fit) -> float:
    """Printed characters a full line of this piece holds: its text covers
    every line but the label's share of the first and the last line's gap."""
    return len(rt.plain(text)) / max(0.5, fit.lines - 1 - fit.lead + fit.fill)


def refit_targets(text: str, fit: rt.Fit) -> tuple[int, int]:
    """The printed length to aim for: one line fewer when only a few words
    spilled, else enough to fill the last line."""
    per = _per_line(text, fit)
    n = (fit.lines - 1 if fit.fill <= 0.45 else fit.lines) - fit.lead
    return int((n - 0.12) * per), int(n * per) - 2


def accepted(res: rt.Resume, bid: str, text: str, jd: str, rules: Rules = Rules()) -> str | None:
    """A model's second try at a bullet (a refit or a fix-up), held to the
    first rewrite's checks and to reading like the person: the text to use,
    or None."""
    found = res.bullet(bid)
    new = rt.sanitize(str(text or ""))
    if not found or not new:
        return None
    entry, b = found
    new = rt.match_ending(b.tex, new)
    basis = rt.entry_evidence(entry) if rules.level == "rewrite" else b.tex
    if rt.check_latex(new) or (rules.check and rt.check_honesty(basis, new, entry.allowed, res.vocabulary, jd)) \
            or rt.style_problems(b.tex, new, rules.banned):
        return None
    return new


def refit_bullets(res: rt.Resume, state: dict, bad: dict, backend: str, jd: str,
                  rules: Rules = Rules()) -> tuple[dict[str, str], float | None]:
    """New wording for spilled bullets, each held to the same honesty checks
    as the first rewrite: {id: text} for the ones that passed, and the cost."""
    blocks = []
    for bid, fit in bad.items():
        _, b = res.bullet(bid)
        lo, hi = refit_targets(state["rewrites"][bid], fit)
        blocks.append(f"ID {bid}\nORIGINAL\n{b.tex.strip()}\nCURRENT\n{state['rewrites'][bid].strip()}\nTARGET {lo} to {hi} characters")
    raw, _, cost = ask(backend, REFIT_RULES, "BULLETS\n\n" + "\n\n".join(blocks), REFIT_SCHEMA, "refit")
    out = {}
    for item in raw.get("bullets") or []:
        bid = str(item.get("id", ""))
        new = accepted(res, bid, item.get("text"), jd, rules) if bid in bad else None
        if new:
            out[bid] = new
    return out, cost


# One fix-up pass for rewrites that read as machine-written (resume_tex.
# style_problems): the problems are named, the result is checked again, and
# what still reads that way keeps the master's wording.
MACHINE = "reads as machine-written"

POLISH_RULES = """You fix resume bullets that read as machine-written. Each comes with the applicant's ORIGINAL wording, the CURRENT tailored version, and the PROBLEMS found in it.

Rules
1. Rewrite CURRENT without the problems, keeping its meaning, the job keywords it uses and about its length.
2. Write like the applicant's ORIGINAL: their kind of verbs and punctuation, plain and specific. No em dashes, no clause tacked on to sound like impact (", ensuring ...", ", enabling ..."), no stock resume words ("spearheaded", "leveraged", "utilized", "robust", "seamless").
3. Never add a skill, tool, number, date, employer or claim that isn't in ORIGINAL or CURRENT.
4. Keep the LaTeX commands CURRENT uses and add none.
5. Return every bullet you were given, by its id.
"""


def polish(res: rt.Resume, state: dict, changes: list[rt.Change], backend: str, jd: str,
           rules: Rules = Rules()) -> float | None:
    """Fix the rewrites plan_edits listed in state["style"]. Returns the cost."""
    bad = {bid: p for bid, p in (state.pop("style", None) or {}).items() if bid in state["rewrites"]}
    if not bad:
        return None
    log = {c.target: c for c in changes if c.kind == "bullet" and c.status == "applied"}
    fixed, cost = {}, None
    if backend != "manual":
        blocks = []
        for bid, problems in bad.items():
            _, b = res.bullet(bid)
            blocks.append(f"ID {bid}\nORIGINAL\n{b.tex.strip()}\nCURRENT\n{state['rewrites'][bid].strip()}"
                          f"\nPROBLEMS: {', '.join(problems)}")
        try:
            raw, _, cost = ask(backend, POLISH_RULES, "BULLETS\n\n" + "\n\n".join(blocks), REFIT_SCHEMA, "refit")
        except BackendError:
            raw = {}
        for item in raw.get("bullets") or []:
            bid = str(item.get("id", ""))
            new = accepted(res, bid, item.get("text"), jd, rules) if bid in bad else None
            if new and not rt.repeated_opening(res, {**state["rewrites"], **fixed}, bid, new):
                fixed[bid] = new
    for bid, problems in bad.items():
        if bid in fixed:
            state["rewrites"][bid] = fixed[bid]
            if bid in log:
                log[bid].after = rt.plain(fixed[bid])
        else:
            state["rewrites"].pop(bid)
            if bid in log:
                log[bid].status, log[bid].reason = "rejected", f"{MACHINE} ({', '.join(problems)})"
    return cost


def fit_lines(res: rt.Resume, state: dict, changes: list[rt.Change], layout: dict, measure,
              backend: str, jd: str, rules: Rules = Rules()) -> float | None:
    """Fix the edits that spill (see above). `measure()` compiles the current
    state into `layout`. Returns what the extra model call cost."""
    bad = spilled(state, layout)
    if not bad:
        return None
    log = {c.target: c for c in changes if c.status == "applied" and c.kind in ("bullet", "tech", "skills")}

    def revert(mid: str) -> None:
        for k in ("rewrites", "tech", "rows"):
            state[k].pop(mid, None)
        if mid in log:
            log[mid].status, log[mid].reason = "rejected", SPILLED

    for mid, fit in bad.items():
        kind = "tech" if mid in state["tech"] else "rows" if mid in state["rows"] else None
        if not kind:
            continue
        old = next(e.tech_tex for e in res.entries if e.id == mid) if kind == "tech" \
            else next(r.tex for r in res.skill_rows if r.id == mid)
        new = state[kind][mid]
        got = rt.trim_appended(old, new, fit.fill * _per_line(new, fit) + 2)
        if not got or rt.plain(got[0]) == rt.plain(old):
            revert(mid)
            continue
        state[kind][mid] = got[0]
        if mid in log:
            note = f"left out {', '.join(got[1])} so it doesn't spill onto another line"
            log[mid].after = rt.plain(got[0])
            log[mid].reason = "; ".join(x for x in (log[mid].reason, note) if x)

    bullets = {bid: fit for bid, fit in bad.items() if bid in state["rewrites"]}
    cost = None
    if bullets:
        new = {}
        if backend != "manual":
            try:
                new, cost = refit_bullets(res, state, bullets, backend, jd, rules)
            except BackendError:
                new = {}
        for bid in bullets:
            if bid in new and rt.plain(new[bid]) == rt.plain(res.bullet(bid)[1].tex):
                # Fitted back to the master's own wording: no change at all.
                state["rewrites"].pop(bid)
                if bid in log:
                    changes.remove(log.pop(bid))
            elif bid in new:
                state["rewrites"][bid] = new[bid]
                if bid in log:
                    log[bid].after = rt.plain(new[bid])
            else:
                revert(bid)

    measure()
    still = spilled(state, layout)
    for mid in still:
        revert(mid)
    if still:
        measure()
    return cost


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:40] or "job"


SPILLED_OUT = "would leave a word or two on a line of its own, and its entry has others"
FILLED = "reworded to end near the end of its line"


def cap_bullets(res: rt.Resume, state: dict) -> set[str]:
    """Drops the bullets beyond the most an entry of its kind shows
    (state["max_bullets"]), the least relevant first (the model's suggestions,
    then by rank; at equal rank the fact sheet's before the resume's own, then
    the later ones). Returns their ids."""
    rank, out = state["rank"], set()
    for e in res.entries:
        cap = state["max_bullets"].get(e.kind)
        kept = [b for b in e.bullets if b.id not in state["drop_bullets"]]
        if not cap or e.id in state["drop_entries"] or len(kept) <= cap:
            continue
        best_first = sorted(kept, key=lambda b: (b.id in state["suggest_bullets"], rank.get(b.id, 999),
                                                 b.fact, e.bullets.index(b)))
        out |= {b.id for b in best_first[cap:]}
    state["drop_bullets"] |= out
    return out


def fit_page_lines(res: rt.Resume, state: dict, changes: list[rt.Change], layout: dict, measure,
                   backend: str, jd: str, rules: Rules = Rules()) -> tuple[bool, float | None]:
    """Every bullet on the page held to the rule edits are: its last line at
    least MIN_FILL full, not a word or two on a line of its own. Each that
    falls short is refitted like an edit (same checks), unless the strictness
    keeps bullets' words; one that still does keeps the resume's wording, or,
    from the fact sheet, is left out when its entry keeps the fewest others.
    Returns whether any was left out, and what the model call cost.
    `measure()` compiles into `layout`."""
    def spilling() -> dict[str, rt.Fit]:
        out = {}
        for e in res.entries:
            if e.id in state["drop_entries"]:
                continue
            for b in e.bullets:
                fit = rt.line_fit(layout, b.id)
                if b.id not in state["drop_bullets"] and spills(fit):
                    out[b.id] = fit
        return out

    bad = {bid: fit for bid, fit in spilling().items() if bid not in state["rewrites"]}
    if not bad:
        return False, None
    new, cost = {}, None
    if backend != "manual" and rules.level != "none":
        for bid in bad:
            state["rewrites"][bid] = res.bullet(bid)[1].tex
        try:
            new, cost = refit_bullets(res, state, bad, backend, jd, rules)
        except BackendError:
            new = {}
        for bid in bad:
            state["rewrites"].pop(bid)
    for bid, text in new.items():
        e, b = res.bullet(bid)
        if rt.plain(text) != rt.plain(b.tex):
            state["rewrites"][bid] = text
            changes.append(rt.Change("bullet", bid, e.title, rt.plain(b.tex), rt.plain(text), fact=b.fact, reason=FILLED))
    measure()
    left_out = False
    for bid in spilling():
        if bid not in bad:
            continue
        e, b = res.bullet(bid)
        state["rewrites"].pop(bid, None)
        changes[:] = [c for c in changes if not (c.target == bid and c.reason == FILLED)]
        if not b.fact:
            continue                    # the resume's own wording stays
        if sum(x.id not in state["drop_bullets"] for x in e.bullets) > state["min_bullets"]:
            state["drop_bullets"].add(bid)
            state.setdefault("spilled", set()).add(bid)
            left_out = True
            continue
        # At the fewest it may show: one the cap set aside takes its place, if that one fills its line.
        rank = state["rank"]
        aside = sorted((x for x in e.bullets if x.id in state.get("capped", set()) and x.id in state["drop_bullets"]),
                       key=lambda x: (rank.get(x.id, 999), x.fact, e.bullets.index(x)))
        for alt in aside[:2]:
            state["drop_bullets"].discard(alt.id)
            state["drop_bullets"].add(bid)
            measure()
            if not spills(rt.line_fit(layout, alt.id)):
                state["capped"].discard(alt.id)
                state.setdefault("spilled", set()).add(bid)
                left_out = True
                break
            state["drop_bullets"].add(alt.id)
            state["drop_bullets"].discard(bid)
    measure()
    return left_out, cost


def trim_order(res: rt.Resume, state: dict) -> list[tuple[str, str]]:
    """Everything that may go to fit the page, in the order it goes, as
    ("bullet", id) and ("entry", id). The model's suggestions go first, then
    the least relevant: a bullet while its entry keeps more than the fewest, or a
    whole project (course projects included) once even its best bullet ranks
    no higher than the weakest bullet left, so a project unrelated to the job
    goes before a related entry loses its detail. Unranked pieces count as the
    least relevant, the later in the resume the sooner; at equal rank the fact
    sheet's pieces go before the resume's own. Work experience keeps every
    entry; every entry keeps the fewest it may show (all it has, when fewer); other
    sections stay as they are."""
    rank, low = state["rank"], state.get("min_bullets", rt.MIN_BULLETS)
    pos = {b.id: i for i, b in enumerate(b for e in res.entries for b in e.bullets)}
    fact = {b.id: b.fact for e in res.entries for b in e.bullets} | {e.id: e.fact for e in res.entries}
    suggested_b, suggested_e = set(state["suggest_bullets"]), set(state["suggest_entries"])
    kept = {e.id: [b.id for b in e.bullets if b.id not in state["drop_bullets"]]
            for e in res.entries if e.id not in state["drop_entries"] and e.kind in ("experience", "project")}
    kinds = {e.id: e.kind for e in res.entries}
    owner = {b.id: e.id for e in res.entries for b in e.bullets}
    # Larger goes first: suggested, then by rank (999 unranked), the fact sheet's, then later in the resume.
    weight = lambda bid: (bid in suggested_b, rank.get(bid, 999), fact[bid], pos[bid])
    order = []
    while True:
        bullets = [bid for eid, bids in kept.items() if len(bids) > low for bid in bids]
        projects = [eid for eid in kept if kinds[eid] == "project"]
        bullet = max(bullets, key=weight, default=None)
        # A project weighs as its best bullet, or as suggested when the model said so.
        entry_weight = lambda eid: (eid in suggested_e, *min(
            ((rank.get(b, 999), fact[eid], pos[b]) for b in kept[eid]), default=(999, fact[eid], 0)))
        entry = max(projects, key=entry_weight, default=None)
        if entry is not None and (bullet is None or entry_weight(entry) >= weight(bullet)):
            order.append(("entry", entry))
            del kept[entry]
        elif bullet is not None:
            order.append(("bullet", bullet))
            kept[owner[bullet]].remove(bullet)
        else:
            return order


def tailor(master_tex: str, job: dict, jd: str, backend: str, out_dir: Path,
           manual_answer: str | None = None, level: str | None = None, confirmed=()) -> dict:
    """`level` is the strictness (config.STRICTNESS), the user's setting by default.
    `confirmed`: skills the person confirmed under Review skills, which the
    skills section may list though the master resume doesn't."""
    if backend != "manual" and backend not in BACKENDS:
        raise BackendError(f"Unknown backend {backend}")
    if len(jd.strip()) < 200:
        raise BackendError("The job description is too short to tailor against. Paste the full posting.")
    level = level or strictness()
    check = check_claims()
    started = time.time()
    res = read_resume(master_tex, job, confirmed)
    cost = None
    if backend == "manual":
        raw, model = parse_manual_answer(manual_answer or ""), "pasted answer"
    else:
        system, user = tailor_prompt(res, job, jd, level, check)
        raw, model, cost = ask(backend, system, user, SCHEMA, "tailor", cache_system=True)
    if level == "none":
        raw = {**raw, "bullet_rewrites": []}

    # The user's own banned phrases, from Settings, writing voice.
    rules = Rules(level, tuple(voice_settings()["banned_phrases"]), check)
    _, changes, state = rt.plan_edits(res, raw, jd, level, rules.banned, check)
    fixed = polish(res, state, changes, backend, jd, rules)
    if fixed is not None:
        cost = (cost or 0) + fixed

    # Every compile marks where each piece's lines end (resume_tex "Line
    # layout"); the saved .tex has no marks. Should the marks themselves not
    # compile with some resume, it's compiled without them and not measured.
    layout: dict = {}
    marks, last = [True], [0]

    def build(measure: bool) -> str:
        tex = rt.tidy(rt.apply(master_tex, rt.build_spans(res, {**state, "measure": measure})))
        return rt.with_marks(tex) if measure else tex

    def compile_now(out_pdf: Path | None = None) -> int:
        """Compiles the current state; returns (and remembers) its page count."""
        if marks[0]:
            try:
                last[0] = rt.compile_pdf(build(True), out_pdf, layout=layout)
                return last[0]
            except rt.CompileError:
                marks[0] = False
        layout.clear()
        last[0] = rt.compile_pdf(build(False), out_pdf)
        return last[0]

    # Compile; if a model edit broke LaTeX, fall back to selection-only edits.
    try:
        pages = compile_now()
    except rt.CompileError as e:
        for c in changes:
            if c.kind in ("bullet", "tech", "skills") and c.status == "applied":
                c.status, c.reason = "rejected", f"reverted: the edited resume didn't compile ({e})"
        state.update(rewrites={}, tech={}, rows={})
        marks[0] = True
        pages = compile_now()

    refit = fit_lines(res, state, changes, layout, compile_now, backend, jd, rules)
    if refit is not None:
        cost = (cost or 0) + refit
    pages = last[0]

    # Bullets per entry, whatever the page: no more than the resume's own entries
    # of its kind show, and a fact sheet entry with fewer than the fewest stays out.
    state["min_bullets"], state["max_bullets"] = bullet_limits(res)
    short = {e.id for e in res.entries if e.fact and len(e.bullets) < state["min_bullets"]}
    state["drop_entries"] |= short
    capped = state["capped"] = cap_bullets(res, state)
    if capped or short:
        compile_now()

    def fit_page() -> None:
        """Trims while the PDF runs long: the shortest start of trim_order that
        fits, found by halving (removing more never adds a page), so a long
        master costs a handful of compiles rather than one per bullet. When
        even everything that may go leaves it long, it aims for the fewest
        pages it can reach, keeping as much as fits in them."""
        pages = last[0]
        if pages <= target_pages():
            return
        order = trim_order(res, state)
        if not order:
            return
        base = (set(state["drop_bullets"]), set(state["drop_entries"]))
        at = [0]

        def upto(n: int) -> int:
            if at[0] == n:
                return last[0]
            at[0] = n
            state["drop_bullets"], state["drop_entries"] = set(base[0]), set(base[1])
            for kind, xid in order[:n]:
                state["drop_bullets" if kind == "bullet" else "drop_entries"].add(xid)
            return compile_now()

        lo, hi = 0, len(order)
        goal = max(target_pages(), upto(hi))
        if goal < pages:
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if upto(mid) <= goal:
                    hi = mid
                else:
                    lo = mid
        else:
            hi = 0
        upto(hi)
        trimmed.extend(order[:hi])

    def refill(misses: int = 4) -> None:
        """Puts back what was trimmed while the page still holds it, the most
        relevant first, until `misses` in a row don't fit: the halving stops at
        the first cut that fits, and refitted lines free room too, so a smaller
        piece further on often still has a place. Bullets come back only up to
        what their entry may show, and only ending near the end of a line, as
        every bullet on the page does (fit_page_lines ran before this)."""
        goal = max(target_pages(), last[0])
        dirty = False
        for kind, xid in reversed(trimmed):
            if misses == 0:
                break
            key = "drop_bullets" if kind == "bullet" else "drop_entries"
            if xid not in state[key]:
                continue
            if kind == "bullet":
                e, _ = res.bullet(xid)
                shown = sum(b.id not in state["drop_bullets"] for b in e.bullets)
                if e.id in state["drop_entries"] or shown >= state["max_bullets"].get(e.kind, shown + 1):
                    continue
                back = [xid]
            else:
                back = [b.id for b in next(x for x in res.entries if x.id == xid).bullets if b.id not in state["drop_bullets"]]
            state[key].discard(xid)
            if compile_now() <= goal and not any(spills(rt.line_fit(layout, bid)) for bid in back):
                dirty = False
                continue
            state[key].add(xid)
            dirty, misses = True, misses - 1
        if dirty:
            compile_now()

    trimmed: list[tuple[str, str]] = []
    fit_page()
    # Every bullet on the page ends near the end of a line, as edits are made to;
    # leaving a fact sheet one out can free room, so the page is fitted again after.
    spill, refit = fit_page_lines(res, state, changes, layout, compile_now, backend, jd, rules)
    if refit is not None:
        cost = (cost or 0) + refit
    if spill:
        fit_page()
    refill()

    name = f"{_slug(job.get('company', ''))}_{_slug(job.get('title', ''))}_{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{name}.tex"
    pdf_path = out_dir / f"{name}.pdf"
    tex = build(False)
    tex_path.write_text(tex, encoding="utf-8")
    pages = compile_now(pdf_path)

    # Drop and reorder entries are derived from the final state so the log
    # matches the PDF exactly.
    final_changes = [c for c in changes if c.kind in ("bullet", "tech", "skills")]
    log: list[rt.Change] = []
    rt.build_spans(res, state, log)
    facts_of = {b.id for e in res.entries for b in e.bullets if b.fact} | {e.id for e in res.entries if e.fact}
    spilled_ids = state.get("spilled", set())
    for c in log:
        if c.kind == "drop_bullet":
            c.fact = c.target in facts_of
            c.reason = (SPILLED_OUT if c.target in spilled_ids
                        else f"kept the {state['max_bullets'][res.bullet(c.target)[0].kind]} most relevant for this job"
                        if c.target in capped else "trimmed to fit one page")
    final_changes += [rt.Change("drop_entry", eid, next(e.title for e in res.entries if e.id == eid),
                                reason="trimmed to fit one page", fact=eid in facts_of)
                      for eid in sorted(state["drop_entries"])] + log
    # What the fact sheet put on the page: its entries, and its bullets in the resume's own entries.
    for e in res.entries:
        if e.id in state["drop_entries"]:
            continue
        if e.fact:
            final_changes.append(rt.Change("fact_entry", e.id, e.title, fact=True))
        else:
            final_changes += [rt.Change("fact_bullet", b.id, e.title, before=rt.plain(b.tex), fact=True)
                              for b in e.bullets if b.fact and b.id not in state["drop_bullets"]]

    return {
        "backend": backend,
        "model": model,
        "seconds": round(time.time() - started, 1),
        "pages": pages,
        "target_pages": target_pages(),
        "tex": tex_path.name,
        "pdf": pdf_path.name,
        "summary": raw.get("summary", ""),
        "keywords": verify_keywords(raw.get("jd_keywords", []), master_tex, tex),
        "changes": [asdict(c) for c in final_changes],
        "master_digest": res.digest,
        "instruction": clean_instruction(job.get("instruction")),
        "strictness": level,
        "check_claims": check,
        "cost": cost,
    }
