"""Tailor a master LaTeX resume to one job description.

The model only proposes edits by ID (rewrite bullet B4, reorder, drop project
E6, ...). resume_tex validates every edit against the master and applies the
survivors to the original source, then this module compiles and trims until
the result fits on one page.

Backends, all returning the same JSON:
  ollama       local model (default qwen3:8b), free, private
  chatgpt      OpenAI's `codex` CLI on your ChatGPT subscription login, no API key
  claude-code  the `claude` CLI on your Claude subscription login, no API key
  claude-api   Anthropic API with ANTHROPIC_API_KEY (or an `ant auth` profile)
  manual       you paste the prompt into any chat and paste the JSON answer back

draft_answer() writes a first draft of a free-text application answer with
the same backends, except claude-api, which bills per token.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path

import resume_tex as rt

ROOT = Path(__file__).resolve().parent
RUNNER_DIR = ROOT / "resume" / ".claude-runner"   # empty cwd so no project files load
OLLAMA_URL = os.environ.get("JOBRADAR_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("JOBRADAR_OLLAMA_MODEL", "qwen3:8b")
CLAUDE_MODEL = os.environ.get("JOBRADAR_CLAUDE_MODEL", "claude-opus-5-5")
TARGET_PAGES = 1
MAX_TRIM_STEPS = 15

INSTRUCTIONS = """You tailor a software engineer's resume to one job description by proposing edits to numbered pieces of the resume. Your output is applied by a program and every edit is checked, so follow the rules exactly.

Rules
1. Truthfulness comes first. A skill may appear in an entry's bullets or tech line only if it is in that entry's VERIFIED SKILLS list. Skills-section rows may use only the VERIFIED INVENTORY. If the job wants something the resume doesn't support, report it as "missing" in jd_keywords and do not add it anywhere.
2. Never add or change numbers, metrics, company names, job titles, dates or URLs.
3. Keep what each bullet says the person did. You may rephrase a bullet so it uses the job description's terminology for things the bullet already describes (for example "REST endpoints" -> "RESTful APIs" when the job says that), or so that a verified skill for that entry is named explicitly. Lead with a strong past-tense verb. Avoid filler such as "leveraged", "spearheaded", "utilized", "synergy".
4. Keep LaTeX exactly as it appears in the original bullet: \\textbf{...}, \\textit{...} and \\href{url}{text} stay as they are. Use no other LaTeX commands. Write & % $ # _ as \\& \\% \\$ \\# \\_.
5. A rewritten bullet must stay about the same length as the original (one printed line is about 110 characters). Only rewrite bullets where it clearly helps; leave the rest out of bullet_rewrites.
6. tech_lines and skills_rows are comma-separated lists of the person's own tool names. Keep every existing item, spelled exactly as written (e.g. keep "React.js", "NumPy", "PouchDB"). You may only reorder items so the job's tools come first, and append a few items the job asks for that are in that entry's VERIFIED SKILLS (tech lines) or the VERIFIED INVENTORY (skills rows). Never replace a tool with a category name such as "Machine Learning", "Frontend" or "Full Stack". Only include a row or tech line in your output if you changed it.
7. bullet_priority lists every bullet ID, most relevant to this job first. Projects are printed in this order too (by their most relevant bullet); work experience stays in date order.
8. The result must fit on one page. Put the least relevant bullets in drop_bullets and, if needed, the least relevant projects in drop_entries. Never drop work experience entries. Keep at least two bullets per entry.
9. jd_keywords lists the 8-15 most important skills or terms in the job description, each with status "added" (you worked it in), "already_present", or "missing" (the resume has no verified evidence), and a short note.
10. summary: two sentences on what you emphasized and why."""

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


class BackendError(Exception):
    pass


def _resume_block(res: rt.Resume) -> str:
    return "RESUME (edit by ID)\n\n" + rt.describe(res)


MAX_INSTRUCTION = 500


def clean_instruction(text) -> str:
    return " ".join(str(text or "").split())[:MAX_INSTRUCTION]


def _job_block(job: dict, jd: str) -> str:
    block = f"JOB: {job.get('title', '')} at {job.get('company', '')}\n\nJOB DESCRIPTION\n{jd.strip()}"
    note = clean_instruction(job.get("instruction"))
    if note:
        # Every backend's prompt (manual paste included) ends with this block.
        block += ("\n\nTHE APPLICANT'S REQUEST FOR THIS VERSION\n" + note +
                  "\nFollow it as far as the rules above allow. The rules win when they conflict; "
                  "if that happens, say so in summary.")
    return block


# --------------------------------------------------------------------------
# Backends

def run_ollama(res: rt.Resume, job: dict, jd: str) -> tuple[dict, str]:
    return _ollama_json(INSTRUCTIONS, _resume_block(res) + "\n\n" + _job_block(job, jd), SCHEMA)


def _claude_json(prompt: str, schema: dict) -> tuple[dict, str]:
    """One structured answer from the `claude` CLI on the user's subscription login."""
    exe = shutil.which("claude")
    if not exe:
        raise BackendError("The claude command isn't installed.")
    RUNNER_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-p", "--output-format", "json", "--no-session-persistence",
           "--tools", "", "--strict-mcp-config",
           "--system-prompt", "You return only the requested JSON.",
           "--json-schema", json.dumps(schema)]
    try:
        proc = subprocess.run(cmd, input=prompt, cwd=RUNNER_DIR, capture_output=True,
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
        raise BackendError(msg[:400])
    parsed = out.get("structured_output")
    if parsed is None:
        try:
            parsed = json.loads(out.get("result", ""))
        except json.JSONDecodeError as e:
            raise BackendError("Claude returned no structured output.") from e
    model = next(iter(out.get("modelUsage") or {}), "claude")
    return parsed, model


def _codex_json(prompt: str, schema: dict) -> tuple[dict, str]:
    """One structured answer from OpenAI's `codex` CLI on the user's ChatGPT login."""
    exe = shutil.which("codex")
    if not exe:
        raise BackendError("The codex command isn't installed. Run: npm install -g @openai/codex")
    RUNNER_DIR.mkdir(parents=True, exist_ok=True)
    # Per-run file names: tailoring and drafting can run at the same time.
    stem = f"codex-{os.getpid()}-{time.time_ns()}"
    schema_path = RUNNER_DIR / f"{stem}.schema.json"
    out_path = RUNNER_DIR / f"{stem}.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    prompt = "Do not run any commands or read any files. Everything you need is below.\n\n" + prompt
    cmd = [exe, "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
           "-s", "read-only", "-C", str(RUNNER_DIR), "--color", "never",
           "--output-schema", str(schema_path), "-o", str(out_path), "-"]
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
            return json.loads(out_path.read_text(encoding="utf-8")), "chatgpt (codex)"
        except json.JSONDecodeError as e:
            raise BackendError("ChatGPT returned malformed output. Try again.") from e
    finally:
        out_path.unlink(missing_ok=True)
        schema_path.unlink(missing_ok=True)


def _ollama_json(system: str, user: str, schema: dict, temperature: float = 0.3) -> tuple[dict, str]:
    body = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "format": schema,
        "stream": False,
        "think": False,
        "options": {"num_ctx": 16384, "temperature": temperature},
    }
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise BackendError(f"Ollama isn't reachable at {OLLAMA_URL}. Start the Ollama app. ({e.reason})") from e
    try:
        return json.loads(data["message"]["content"]), OLLAMA_MODEL
    except (KeyError, json.JSONDecodeError) as e:
        raise BackendError("The local model returned malformed output. Try again.") from e


def run_claude_code(res: rt.Resume, job: dict, jd: str) -> tuple[dict, str]:
    return _claude_json(INSTRUCTIONS + "\n\n" + _resume_block(res) + "\n\n" + _job_block(job, jd), SCHEMA)


def run_chatgpt(res: rt.Resume, job: dict, jd: str) -> tuple[dict, str]:
    return _codex_json("\n\n".join([INSTRUCTIONS, _resume_block(res), _job_block(job, jd)]), SCHEMA)


def build_manual_prompt(master_tex: str, job: dict, jd: str) -> str:
    """Everything a chat model needs, for pasting into ChatGPT or Claude by hand."""
    res = rt.parse(master_tex)
    return "\n\n".join([
        INSTRUCTIONS,
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


def run_claude_api(res: rt.Resume, job: dict, jd: str) -> tuple[dict, str]:
    import anthropic
    client = anthropic.Anthropic()
    try:
        # The resume and instructions are identical across jobs, so they sit in a
        # cached system prefix; only the job description varies per request.
        with client.beta.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=32000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": INSTRUCTIONS + "\n\n" + _resume_block(res),
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": _job_block(job, jd)}],
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": SCHEMA}},
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise BackendError("No valid Anthropic API key. Set ANTHROPIC_API_KEY or run `ant auth login`.") from e
    except anthropic.RateLimitError as e:
        raise BackendError("Anthropic rate limit hit. Wait a minute and try again.") from e
    except anthropic.APIStatusError as e:
        raise BackendError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise BackendError("Couldn't reach the Anthropic API.") from e
    if msg.stop_reason == "refusal":
        raise BackendError("Claude declined this request.")
    if msg.stop_reason == "max_tokens":
        raise BackendError("Claude's answer was cut off. Try again.")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        return json.loads(text), msg.model
    except json.JSONDecodeError as e:
        raise BackendError("Claude returned malformed output.") from e


BACKENDS = {"ollama": run_ollama, "chatgpt": run_chatgpt, "claude-code": run_claude_code,
            "claude-api": run_claude_api}


# --------------------------------------------------------------------------
# Writing voice for drafted answers
#
# Drafts should read like the applicant wrote them: a new grad talking to a
# person, not a model summarizing a resume. The prompt sets the voice, and
# style_problems() checks the result in code, because models drift back to
# the same stock phrases whatever the prompt says.

ABOUT_PATH = ROOT / "resume" / "about_me.txt"
ABOUT_MAX = 4000

VOICE = """How to write it
- Sound like a real new-grad student talking to a person: plain everyday words, contractions (I'm, I've, it's), a mix of short and longer sentences, and a bit of real personality. It should read like the applicant typed it, not like a cover letter template.
- Lead with an honest, personal reason when ABOUT THE APPLICANT gives one that fits (an interest, something they care about, how they use the product). Never invent interests, hobbies, feelings or stories that aren't written there.
- Mention at most one or two things from the resume, described the way you'd tell a friend what you built ("I built a chatbot that remembers past conversations"), not as a resume line full of tool names. Name a tool only if the question or the job really cares about it.
- Don't restate the company's mission or the posting back to them, and don't explain what their product does.
- Never use these or anything like them: "appeals to me", "resonates", "aligns with", "connects to", "makes that connection", "concrete", "I would bring", "I'd bring", "leverage", "passionate", "thrilled", "excited to", "eager to", "deeply", "at the intersection of", "drive impact", "cutting-edge", "seamless", "robust", "fast-paced", "dynamic", "I am confident", "not only ... but also", "In [project], I built", "This experience taught me", "Ultimately".
- Also avoid cover-letter stock lines: "offers the chance", "the opportunity to", "a big reason", "this particular role", "I'd like to apply", "matters to me", "take an idea from", "through to", "grounded", "meaningful", "impactful", "contribute to".
- No em dashes, no semicolons, no lists of three adjectives or three items in a row.
- Don't repeat a word (other than small common words) and use the company or product name at most twice.

Example of the difference (different company, same idea):
Too machine-written: "Notion's mission resonates with me because it empowers teams to collaborate seamlessly. In my capstone project, I leveraged React and Node.js to build a robust task-management platform, and I would bring this full-stack experience to Notion's dynamic team."
Sounds like a person: "I've used Notion for every class since my first semester, so I already have opinions about what it gets right. For my capstone I built a small task tracker in React and Node, and the hardest part was making edits feel instant when two people were working at once. I'd love to work on that kind of problem with people who care about it as much as I do.\""""

# Stock phrasing that marks text as machine-written. Checked case-insensitively.
AI_TELLS = [
    r"appeals? to me", r"resonat\w+", r"aligns? (?:with|perfectly)", r"connects? (?:to|with)",
    r"make[sd]? that connection", r"\bconcrete(?:ly)?\b", r"\bI(?:'d| would) bring\b", r"\bleverag\w+",
    r"\bpassionat\w+", r"\bthrilled\b", r"\bexcited to\b", r"\beager to\b", r"\bdeeply\b",
    r"at the intersection of", r"drive (?:real )?impact", r"cutting[- ]edge", r"\bseamless(?:ly)?\b",
    r"\brobust\b", r"fast[- ]paced", r"\bdynamic\b", r"I am confident", r"not only\b.{0,60}\bbut also",
    r"\bthis experience (?:taught|gave|showed)", r"\bultimately\b", r"\bfurthermore\b", r"\bmoreover\b",
    r"\bin today's\b", r"\bunique opportunity\b", r"\bsynergy\b", r"\bend-to-end\b",
    r"offers? (?:me )?the chance", r"\bthe opportunity to\b", r"\ba big reason\b", r"\bthis particular role\b",
    r"\bI'd like to apply\b", r"\bmatters? to me\b", r"take an idea from", r"\bthrough to\b",
    r"\bgrounded\b", r"\bmeaningful\b", r"\bimpactful\b", r"\bcontribute to\b",
]
_TELL_RX = [re.compile(p, re.I) for p in AI_TELLS]
_STOP = set("""a an the and or but if of to in on at for with from by as is are was were be been being it its
this that these those i i'm i've i'd me my we our you your they their them he she his her there here what
which who whom how why when where so than then too very just also not no can could would should will
about into over under after before while because more most some any all each other such only own same
both few many much do does did done have has had having get got make made one two like""".split())


def about_me() -> str:
    """The applicant's own notes on interests and motivations, if they wrote any."""
    try:
        return ABOUT_PATH.read_text(encoding="utf-8").strip()[:ABOUT_MAX]
    except OSError:
        return ""


def save_about_me(text: str) -> None:
    ABOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ABOUT_PATH.write_text(text.strip()[:ABOUT_MAX] + "\n", encoding="utf-8")


def style_problems(text: str, company: str = "") -> list[str]:
    """Concrete reasons a draft reads as machine-written, for a rewrite pass."""
    problems = []
    for rx in _TELL_RX:
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


REWRITE_INSTRUCTIONS = """Rewrite each answer below so it sounds like the applicant wrote it themselves. Keep every fact exactly as it is and add no new ones. Fix the listed problems and follow the voice rules. Keep each answer about the same length and within its character limit.

""" + VOICE + """

Return one entry per id with the rewritten text in "answer"."""

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"answers": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "answer": {"type": "string"}},
        "required": ["id", "answer"], "additionalProperties": False}}},
    "required": ["answers"],
    "additionalProperties": False,
}


def humanize(items: list[dict], backend: str, company: str = "") -> list[dict]:
    """One rewrite pass over answers that still read as machine-written.

    items: [{"id", "answer", "max_chars"}]. Returns the same list with
    rewritten answers where the rewrite came back cleaner, plus "style_notes"
    listing whatever problems remain so the dashboard can point them out."""
    flagged = [dict(i, problems=style_problems(i["answer"], company)) for i in items]
    todo = [i for i in flagged if i["problems"]]
    if todo:
        lines = []
        for i in todo:
            limit = f" (at most {i['max_chars']} characters)" if i.get("max_chars") else ""
            lines.append(f"[id {i['id']}]{limit}\nproblems: {'; '.join(i['problems'])}\nanswer: {i['answer']}")
        prompt = REWRITE_INSTRUCTIONS + "\n\nANSWERS\n" + "\n\n".join(lines)
        try:
            if backend == "ollama":
                raw, _ = _ollama_json("You return only the requested JSON.", prompt, REWRITE_SCHEMA, temperature=0.6)
            elif backend == "chatgpt":
                raw, _ = _codex_json(prompt, REWRITE_SCHEMA)
            else:
                raw, _ = _claude_json(prompt, REWRITE_SCHEMA)
        except BackendError:
            raw = {}
        better = {str(a.get("id")): str(a.get("answer", "")).strip() for a in (raw or {}).get("answers", [])}
        for i in todo:
            new = _fit(better.get(str(i["id"]), ""), int(i.get("max_chars") or 0))
            # Keep the rewrite only if it really is cleaner.
            if new and len(style_problems(new, company)) < len(i["problems"]):
                i["answer"] = new
                i["problems"] = style_problems(new, company)
    return [dict({k: v for k, v in i.items() if k != "problems"}, style_notes=i["problems"]) for i in flagged]


# --------------------------------------------------------------------------
# Drafting answers to application questions ("Why this company?")

DRAFT_INSTRUCTIONS = """You draft one answer to a question on a job application, written as the applicant, using their resume and the job posting. The applicant reviews and edits it before sending.

Rules
1. Use only facts from the resume and from ABOUT THE APPLICANT. Never invent employers, projects, numbers, dates, skills, interests or personal details. If neither supports something the question asks about, answer honestly with what they do show instead of making something up.
2. Answer the question that was asked, for this company and role.
3. First person. No greeting, sign-off or headings, and no bullet points unless the question asks for a list.
4. Length: {length}.
5. Put only the answer text in the "answer" field.

""" + VOICE

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

# Subscription logins and the local model only. claude-api bills per token,
# so drafting never uses it.
DRAFT_BACKENDS = ("chatgpt", "claude-code", "ollama")


def draft_prompt(master_tex: str, job: dict, question: str, context: str, jd: str, max_chars: int = 0) -> tuple[str, str]:
    """(instructions, material) for drafting one answer."""
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
    about = about_me()
    if about:
        parts.append(f"ABOUT THE APPLICANT (their own words: interests and motivations you may use)\n{about}")
    # .replace, not .format: the voice rules contain literal braces-free text but
    # may gain some, and only {length} is a placeholder.
    return DRAFT_INSTRUCTIONS.replace("{length}", length), "\n\n".join(parts)


def build_draft_prompt(master_tex: str, job: dict, question: str, context: str, jd: str, max_chars: int = 0) -> str:
    """The whole drafting prompt, for pasting into a chat by hand."""
    instructions, material = draft_prompt(master_tex, job, question, context, jd, max_chars)
    return instructions.replace('Put only the answer text in the "answer" field.', "Reply with only the answer text.") + "\n\n" + material


def _fit(text: str, max_chars: int) -> str:
    text = text.strip()
    if not max_chars or len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(".\n"))
    return (cut[:end + 1] if end > max_chars // 2 else cut.rsplit(" ", 1)[0]).strip()


ANSWERS_INSTRUCTIONS = """You fill in the remaining questions on one job application, as the applicant, using their resume, their profile and the job posting. The applicant reviews every answer before submitting.

Rules
1. Never invent experience, employers, projects, numbers, skills, awards, dates, interests or personal details. Use only what the resume, the profile and ABOUT THE APPLICANT say, plus plain inferences from them (someone living in Boston is not a California resident; a May 2026 graduate can start around June 2026; someone whose resume doesn't list this company has not worked there).
2. If the answer depends on something only the applicant knows and the resume and profile don't settle it (offer deadlines, family ties to employees, consent to recordings or data sharing, legal declarations), return an empty answer so the applicant answers it.
3. Choice questions: "answer" must be copied exactly from that question's options, or empty. For "multi" questions put every option that applies in "choices" (exact option text) and leave "answer" empty. Otherwise leave "choices" empty.
4. Short text questions get short answers (a date, a year, a name, a few words). Open questions get a specific answer for this company and role: about 80 to 150 words unless the question asks for less, and never more than its character limit. First person; no greeting, no headings. Write open answers by the voice rules below.
5. Return one entry per question id.

""" + VOICE

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

PROFILE_FOR_ANSWERS = ("firstName", "lastName", "preferredName", "location", "country", "company", "currentTitle",
                       "school", "degree", "major", "gradDate", "languages", "linkedin", "github", "portfolio")


def answers_prompt(master_tex: str, job: dict, profile: dict, questions: list[dict], jd: str) -> str:
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
    parts = [ANSWERS_INSTRUCTIONS, f"JOB: {job.get('title', '')} at {job.get('company', '')}"]
    if jd.strip():
        parts.append(f"JOB POSTING\n{jd.strip()[:15000]}")
    parts += [f"PROFILE\n{prof}", f"RESUME\n{rt.plain(master_tex)}"]
    about = about_me()
    if about:
        parts.append(f"ABOUT THE APPLICANT (their own words: interests and motivations you may use)\n{about}")
    parts.append("QUESTIONS\n" + "\n".join(qs))
    return "\n\n".join(parts)


def answer_questions(master_tex: str, job: dict, profile: dict, questions: list[dict], jd: str, backend: str) -> dict:
    """Answers for several application questions in one model call."""
    if backend not in DRAFT_BACKENDS:
        raise BackendError(f"Answering questions can't use {backend}. Choose ChatGPT, Claude or the local model.")
    if not questions:
        return {"answers": [], "backend": backend, "model": "", "seconds": 0}
    started = time.time()
    prompt = answers_prompt(master_tex, job, profile, questions, jd)
    if backend == "ollama":
        raw, model = _ollama_json("You return only the requested JSON.", prompt, ANSWERS_SCHEMA, temperature=0.4)
    elif backend == "chatgpt":
        raw, model = _codex_json(prompt, ANSWERS_SCHEMA)
    else:
        raw, model = _claude_json(prompt, ANSWERS_SCHEMA)
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
        fixed = {i["id"]: i for i in humanize(long, backend, job.get("company", ""))}
        for a in out:
            if a["id"] in fixed:
                a["answer"] = fixed[a["id"]]["answer"]
                a["style_notes"] = fixed[a["id"]]["style_notes"]
    return {"answers": out, "backend": backend, "model": model, "seconds": round(time.time() - started, 1)}


def draft_answer(master_tex: str, job: dict, question: str, context: str, jd: str,
                 backend: str, max_chars: int = 0) -> dict:
    if backend not in DRAFT_BACKENDS:
        raise BackendError(f"Drafting can't use {backend}. Choose ChatGPT, Claude or the local model.")
    if not question.strip():
        raise BackendError("No question to answer.")
    started = time.time()
    instructions, material = draft_prompt(master_tex, job, question, context, jd, max_chars)
    if backend == "ollama":
        raw, model = _ollama_json(instructions, material, DRAFT_SCHEMA, temperature=0.5)
    elif backend == "chatgpt":
        raw, model = _codex_json(instructions + "\n\n" + material, DRAFT_SCHEMA)
    else:
        raw, model = _claude_json(instructions + "\n\n" + material, DRAFT_SCHEMA)
    answer = _fit(str((raw or {}).get("answer", "")), max_chars)
    if not answer:
        raise BackendError("The model returned an empty answer. Try again.")
    checked = humanize([{"id": "1", "answer": answer, "max_chars": max_chars}], backend, job.get("company", ""))[0]
    return {"answer": checked["answer"], "style_notes": checked["style_notes"], "backend": backend,
            "model": model, "seconds": round(time.time() - started, 1)}


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
    status = {"ollama": False, "ollama_model": OLLAMA_MODEL,
              "claude_code": _signed_in("claude", ["claude", "auth", "status"], _claude_logged_in),
              "chatgpt": _signed_in("codex", ["codex", "login", "status"],
                                    lambda p: p.returncode == 0 and "logged in" in (p.stdout + p.stderr).lower()),
              "claude_api": bool(os.environ.get("ANTHROPIC_API_KEY"))}
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as resp:
            names = [m["name"] for m in json.loads(resp.read()).get("models", [])]
        status["ollama"] = OLLAMA_MODEL in names or f"{OLLAMA_MODEL}:latest" in names
        status["ollama_running"] = True
    except Exception:
        status["ollama_running"] = False
    return status


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


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:40] or "job"


def tailor(master_tex: str, job: dict, jd: str, backend: str, out_dir: Path,
           rewrite_bullets: bool = True, manual_answer: str | None = None) -> dict:
    if backend != "manual" and backend not in BACKENDS:
        raise BackendError(f"Unknown backend {backend}")
    if len(jd.strip()) < 200:
        raise BackendError("The job description is too short to tailor against. Paste the full posting.")
    started = time.time()
    res = rt.parse(master_tex)
    if backend == "manual":
        raw, model = parse_manual_answer(manual_answer or ""), "pasted answer"
    else:
        raw, model = BACKENDS[backend](res, job, jd)
    if not rewrite_bullets:
        raw = {**raw, "bullet_rewrites": []}

    spans, changes, state = rt.plan_edits(res, raw, jd)
    tex = rt.apply(master_tex, spans)

    # Compile; if a model edit broke LaTeX, fall back to selection-only edits.
    try:
        pages = rt.compile_pdf(tex)
    except rt.CompileError as e:
        for c in changes:
            if c.kind in ("bullet", "tech", "skills") and c.status == "applied":
                c.status, c.reason = "rejected", f"reverted: the edited resume didn't compile ({e})"
        state.update(rewrites={}, tech={}, rows={})
        tex = rt.apply(master_tex, rt.build_spans(res, state))
        pages = rt.compile_pdf(tex)

    # Trim only while the PDF runs long. Order: the model's suggested bullets,
    # then the least relevant bullet (entries keep 2), then suggested projects,
    # then the least relevant project. Work experience is never removed.
    rank = state["rank"]
    for _ in range(MAX_TRIM_STEPS):
        if pages <= TARGET_PAGES:
            break
        droppable = []
        for e in res.entries:
            if e.id in state["drop_entries"]:
                continue
            kept = [b for b in e.bullets if b.id not in state["drop_bullets"]]
            if len(kept) > 2:
                droppable += kept
        pick = next((bid for bid in state["suggest_bullets"]
                     if bid in {b.id for b in droppable}), None)
        if pick is None and droppable:
            pick = max(droppable, key=lambda b: rank.get(b.id, 999)).id
        if pick is not None:
            state["drop_bullets"].add(pick)
        else:
            projects = [e for e in res.entries if e.kind == "project" and e.id not in state["drop_entries"]]
            if not projects:
                break
            victim = next((e for eid in state["suggest_entries"] for e in projects if e.id == eid), None)                 or max(projects, key=lambda e: min((rank.get(b.id, 999) for b in e.bullets), default=999))
            state["drop_entries"].add(victim.id)
        tex = rt.apply(master_tex, rt.build_spans(res, state))
        pages = rt.compile_pdf(tex)

    name = f"{_slug(job.get('company', ''))}_{_slug(job.get('title', ''))}_{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{name}.tex"
    pdf_path = out_dir / f"{name}.pdf"
    tex_path.write_text(tex, encoding="utf-8")
    rt.compile_pdf(tex, pdf_path)

    # Drop and reorder entries are derived from the final state so the log
    # matches the PDF exactly.
    final_changes = [c for c in changes if c.kind in ("bullet", "tech", "skills")]
    log: list[rt.Change] = []
    rt.build_spans(res, state, log)
    for c in log:
        if c.kind == "drop_bullet":
            c.reason = "trimmed to fit one page"
    final_changes += [rt.Change("drop_entry", eid, next(e.title for e in res.entries if e.id == eid),
                                reason="trimmed to fit one page")
                      for eid in sorted(state["drop_entries"])] + log

    return {
        "backend": backend,
        "model": model,
        "seconds": round(time.time() - started, 1),
        "pages": pages,
        "tex": tex_path.name,
        "pdf": pdf_path.name,
        "summary": raw.get("summary", ""),
        "keywords": verify_keywords(raw.get("jd_keywords", []), master_tex, tex),
        "changes": [asdict(c) for c in final_changes],
        "master_digest": res.digest,
        "instruction": clean_instruction(job.get("instruction")),
    }
