"""Checks server.py's /api/draft endpoint on a spare port: who may call it,
what it refuses, and that manual mode returns a prompt. No model is called.

Run:  python extension/test/test_draft_endpoint.py
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import server  # noqa: E402

PORT = 8799
server.PORT = PORT
server.ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
httpd = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

failures = 0


def call(method, path, body=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except json.JSONDecodeError:
            return e.code, None


def check(name, cond, detail=""):
    global failures
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    failures += not cond


Q = {"question": "Why do you want to work at Example Test Corp?", "company": "Example Test Corp",
     "title": "Software Engineer", "url": "https://jobs.lever.co/example/123/apply", "jd": "We build tools.", "backend": "manual"}
EXT = {"X-Onward-Client": "autofill"}

s, _ = call("POST", "/api/draft", Q)
check("no origin, no client header: refused", s == 403, s)
s, _ = call("POST", "/api/draft", Q, {"Origin": "https://evil.example", **EXT})
check("website origin with the header: refused", s == 403, s)
s, _ = call("POST", "/api/draft", Q, {"Origin": "moz-extension://abc"})
check("extension origin without the header: refused", s == 403, s)
s, _ = call("POST", "/api/tailor", {"backend": "chatgpt"}, {"Origin": "moz-extension://abc", **EXT})
check("extension can't start tailoring", s == 403, s)
s, _ = call("OPTIONS", "/api/draft", None, {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
                                             "Access-Control-Request-Headers": "x-jobradar-client"})
check("preflight is not answered", s >= 400, s)

if server.MASTER.exists():
    for origin in (None, "moz-extension://0f3c2a9e-1111-2222-3333-444455556666", "chrome-extension://abcdefghijklmnop"):
        h = dict(EXT, **({"Origin": origin} if origin else {}))
        s, body = call("POST", "/api/draft", Q, h)
        check(f"manual prompt for origin {origin}", s == 200 and "Why do you want to work at Example Test Corp?" in body.get("prompt", "")
              and "RESUME" in body["prompt"] and "Reply with only the answer text." in body["prompt"], (s, body and str(body)[:200]))
    s, body = call("POST", "/api/draft", {**Q, "max_chars": 500}, EXT)
    check("character limit reaches the prompt", s == 200 and "at most 500 characters" in body["prompt"], s)
    s, body = call("POST", "/api/draft", {**Q, "url": "https://job-boards.greenhouse.io/chime/jobs/8875663002"}, EXT)
    check("job looked up from the application URL", s == 200 and "Chime" in body["prompt"], s)
    s, _ = call("POST", "/api/draft", {**Q, "backend": "no-such-backend"}, EXT)
    check("unknown backend refused (API backends are allowed now, within their budget)", s == 400, s)
    s, _ = call("POST", "/api/draft", {**Q, "question": "  "}, EXT)
    check("empty question refused", s == 400, s)
else:
    print("skip: no master resume uploaded, so prompts can't be built")

s, _ = call("GET", "/api/draft/doesnotexist")
check("unknown draft id is 404", s == 404, s)

httpd.shutdown()
print("all passed" if not failures else f"{failures} failed")
sys.exit(1 if failures else 0)
