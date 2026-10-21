"""The custom questions on a posting's application form, read from the ATS's
public data, so answers can be drafted before the user opens the form.

  Greenhouse  boards-api job endpoint with ?questions=true
  Lever       the card templates embedded in the public apply page
  Ashby       the GraphQL query the public application page itself uses
  Workday     not available: the form sits behind a per-company account

Only questions a model may answer are returned. Standard profile fields,
work authorization, sponsorship, demographic, salary, legal and consent
questions are dropped here, so they are never sent to a model; the
extension fills those from the user's saved answers. The extension's own
classifier (extension/src/shared/rules.js) still decides at fill time which
prepared answers it uses, so this list errs toward dropping.
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from urllib.parse import parse_qs, urlparse

TIMEOUT = 20
USER_AGENT = "jobradar/1.0 (personal job search)"

# Asked of the user, never of a model.
SENSITIVE = re.compile(
    r"sponsor|visa\b|immigration|h-?1b|\bopt\b|\bcpt\b|stem extension|authori[sz]|legally (?:able|eligible|permitted)|"
    r"eligib|citizen|permanent resident|green card|right to work|"
    r"salary|compensation|\bpay\b|wage|hourly rate|"
    r"gender|\bsex\b|\brace\b|ethnic|hispanic|latin[oax]|veteran|military|disabilit|lgbt|orientation|transgender|pronoun|"
    r"criminal|convict|felony|background check|drug|"
    r"consent|\bagree|acknowledge|certify|attest|arbitrat|terms (?:and|&)|privacy|signature|\bsign\b|"
    r"clearance|itar|export control|u\.?s\.? person|date of birth|birth ?date|\bage\b|18 years|over 18|"
    r"social security|\bssn\b|relocat|heard? (?:about|of)|how did you (?:hear|find|learn)|referr|\bconfirm|"
    r"demographic|self[- ]identif",
    re.I)
# Filled from the user's profile by the extension.
PROFILE = re.compile(
    r"^\W*(?:\(optional\)\s*)?(?:(?:legal |preferred |full )?(?:first |last )?name|e-?mail|phone|mobile|linked ?in|git ?hub|website|"
    r"portfolio|personal (?:web)?site|twitter|address|city|state|country|zip|postal|location|"
    r"(?:current|last|most recent)(?:\s*/\s*(?:current|last))? (?:company|employer|title)|"
    r"school|university|college|degree|major|discipline|gpa|graduation(?: date| year)?|resume|cv|cover letter|"
    r"(?:other )?(?:url|link)s?)\b.{0,40}$", re.I)

GREENHOUSE_STD = {"first_name", "last_name", "preferred_name", "email", "phone", "resume", "resume_text",
                  "cover_letter", "cover_letter_text", "location", "linkedin_profile", "website"}


def keep(question: str) -> bool:
    q = " ".join(question.split())
    return len(q) >= 4 and not SENSITIVE.search(q) and not PROFILE.match(q)


def _get(url: str, body: dict | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read(8 * 1024 * 1024)


def _q(qid: str, question: str, kind: str, options=(), max_chars: int = 0) -> dict:
    return {"id": str(qid)[:60], "question": " ".join(question.split())[:1500], "kind": kind,
            "options": [str(o)[:300] for o in options][:200], "max_chars": max_chars}


def greenhouse(board: str, job_id: str) -> list[dict]:
    data = json.loads(_get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true"))
    out = []
    for q in data.get("questions") or []:
        fields = [f for f in q.get("fields") or [] if f.get("type") not in ("input_file", "input_hidden")]
        if not fields or fields[0].get("name") in GREENHOUSE_STD:
            continue
        f = fields[0]
        kind = {"input_text": "text", "textarea": "textarea", "multi_value_single_select": "choice",
                "multi_value_multi_select": "multi"}.get(f.get("type"))
        if kind:
            out.append(_q(f["name"], q.get("label", ""), kind, [v.get("label", "") for v in f.get("values") or []]))
    return out


LEVER_CARD = re.compile(r'<input type="hidden" value="([^"]*)" name="cards\[[0-9a-f-]+\]\[baseTemplate\]"')


def lever(page_html: str) -> list[dict]:
    out = []
    for m in LEVER_CARD.finditer(page_html):
        try:
            card = json.loads(html.unescape(m.group(1)))
        except json.JSONDecodeError:
            continue
        for f in card.get("fields") or []:
            kind = {"text": "text", "textarea": "textarea", "multiple-choice": "choice", "dropdown": "choice",
                    "multiple-select": "multi"}.get(f.get("type"))
            if kind:
                out.append(_q(f.get("id", ""), f.get("text", ""), kind, [o.get("text", "") for o in f.get("options") or []]))
    return out


ASHBY_QUERY = ("query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) { "
               "jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) { "
               "applicationForm { sections { fieldEntries { ... on FormFieldEntry { field } } } } } }")


def ashby(board: str, posting_id: str) -> list[dict]:
    data = json.loads(_get("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting", {
        "operationName": "ApiJobPosting", "query": ASHBY_QUERY,
        "variables": {"organizationHostedJobsPageName": board, "jobPostingId": posting_id}}))
    posting = (data.get("data") or {}).get("jobPosting") or {}
    out = []
    for sec in (posting.get("applicationForm") or {}).get("sections") or []:
        for e in sec.get("fieldEntries") or []:
            f = e.get("field") or {}
            if str(f.get("path", "")).startswith("_systemfield"):
                continue
            t = f.get("type")
            opts = [v.get("label", "") for v in f.get("selectableValues") or []]
            if t == "Boolean":
                kind, opts = "choice", ["Yes", "No"]
            else:
                kind = {"String": "text", "LongText": "textarea", "ValueSelect": "choice", "MultiValueSelect": "multi"}.get(t)
            if kind:
                out.append(_q(f.get("path", ""), f.get("title", ""), kind, opts))
    return out


def fetch(apply_url: str) -> list[dict] | None:
    """Questions a model may answer, or None when this ATS can't be read ahead."""
    u = urlparse(apply_url or "")
    host = u.netloc.lower()
    parts = [p for p in u.path.split("/") if p]
    if host.endswith("greenhouse.io"):
        q = parse_qs(u.query)
        board, jid = (q.get("for") or [None])[0], (q.get("token") or [None])[0]
        if not (board and jid) and len(parts) >= 3 and parts[1] == "jobs":
            board, jid = parts[0], parts[2]
        if not (board and jid and re.fullmatch(r"[\w-]+", board) and jid.isdigit()):
            return None
        qs = greenhouse(board, jid)
    elif host in ("jobs.lever.co", "jobs.eu.lever.co") and len(parts) >= 2:
        if not re.fullmatch(r"[0-9a-f-]{36}", parts[1]):
            return None
        qs = lever(_get(f"https://{host}/{parts[0]}/{parts[1]}/apply").decode("utf-8", "replace"))
    elif host == "jobs.ashbyhq.com" and len(parts) >= 2:
        qs = ashby(parts[0], parts[1])
    else:
        return None
    return [q for q in qs if keep(q["question"])]
