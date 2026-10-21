"""Runs the extension in real Firefox (temporary add-on via Selenium) against
the mocks, served from a local web server. Firefox can't reroute the ATS host
names the way Playwright does, so a temporary copy of the extension also
matches the local server; the extension recognizes each form by its markup.

Run:  python extension/test/firefox_mocks.py          mocks (after node build-mocks.mjs)
      python extension/test/firefox_mocks.py --live   read-only on live forms
"""
import base64
import functools
import http.server
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import WebDriverWait

HERE = Path(__file__).resolve().parent
EXT = HERE.parent
DIST = HERE / "mocks" / "dist"
PORT = 8781
ADDON_ID = "jobradar-autofill@jobradar.local"
UUID = "4f1d3a52-6c1e-4d7b-9a0e-1b2c3d4e5f60"
FIREFOX = r"C:\Program Files\Mozilla Firefox\firefox.exe"

PROFILE = json.loads((HERE / "fixtures" / "fake-profile.json").read_text(encoding="utf-8"))
RESUME = (HERE / "fixtures" / "fake-resume.pdf").read_bytes()
RESUME_STATE = f"fake-resume.pdf:{len(RESUME)}"

LIVE = [
    ("greenhouse", "https://job-boards.greenhouse.io/chime/jobs/8875663002",
     {"#first_name": "Testy", "#email": "testy.mctestface@example.com", "#phone": "555-0100"}),
    ("lever", "https://jobs.lever.co/veeva/8e4b8273-783e-420a-8f93-af521a341d1e/apply",
     {"input[name=name]": "Testy McTestface", "input[name=location]": "Boston, MA, USA"}),
    ("ashby", "https://jobs.ashbyhq.com/netic/e645059a-a2e8-4cfd-84e8-085d92da6e62/application",
     {"#_systemfield_name": "Testy McTestface", "#_systemfield_email": "testy.mctestface@example.com"}),
]

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  {detail}"))
    failures += not cond


def expect_state(name, state, want):
    for k, v in want.items():
        check(f"{name}: {k}", state.get(k) == v, f"got {state.get(k)!r}, want {v!r}")


def serve():
    (DIST / "embed-local.html").write_text(
        f'<!doctype html><html><head><meta charset="utf-8"><title>Careers at Mock Co</title></head><body><h1>Careers</h1>'
        f'<iframe id="grnhse_iframe" src="http://localhost:{PORT}/greenhouse.html" width="100%" height="2600" style="border:0"></iframe></body></html>',
        encoding="utf-8")
    (DIST / "seed.html").write_text("<!doctype html><title>seed</title>", encoding="utf-8")
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = functools.partial(Quiet, directory=str(DIST))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_copy() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="jr-ff-"))
    dst = tmp / "ext"
    shutil.copytree(EXT, dst, ignore=shutil.ignore_patterns("test", "web-ext-artifacts"))
    m = json.loads((dst / "manifest.json").read_text(encoding="utf-8"))
    local = ["http://127.0.0.1/*", "http://localhost/*"]   # Firefox match patterns take no port
    m["host_permissions"] += local
    m["content_scripts"][0]["matches"] += local
    # Test copy only: lets the test seed extension storage from a local page,
    # since Marionette can't script extension pages.
    m["content_scripts"].append({"matches": local, "js": ["test-seed.js"], "run_at": "document_start"})
    (dst / "test-seed.js").write_text("""
      window.addEventListener("message", (e) => {
        if (e.source !== window || !e.data || !e.data.__jrSeed) return;
        browser.storage.local.clear().then(() => browser.storage.local.set(e.data.__jrSeed))
          .then(() => document.documentElement.setAttribute("data-jr-seeded", "ok"),
                (err) => document.documentElement.setAttribute("data-jr-seeded", String(err)));
      });
    """, encoding="utf-8")
    (dst / "manifest.json").write_text(json.dumps(m, indent=1), encoding="utf-8")
    return dst


def main():
    httpd = serve()
    ext = test_copy()
    opts = Options()
    opts.binary_location = FIREFOX
    if "--headed" not in sys.argv:
        opts.add_argument("-headless")
    opts.set_preference("extensions.webextensions.uuids", json.dumps({ADDON_ID: UUID}))
    driver = webdriver.Firefox(options=opts)
    driver.set_window_size(1280, 1000)
    try:
        driver.install_addon(str(ext), temporary=True)
        print("Firefox", driver.capabilities.get("browserVersion"), "- extension installed temporarily")
        time.sleep(1)
        # The extension opens its options page on install, reusing the blank tab.
        handles = driver.window_handles
        driver.switch_to.new_window("tab")
        for h in handles:
            driver.switch_to.window(h)
            driver.close()
        driver.switch_to.window(driver.window_handles[0])

        def seed(extra=None):
            # Selenium can't intercept the helper, so it stays off: otherwise the
            # forms would get work history from the user's real master resume.
            data = {**PROFILE, "settings": {**PROFILE["settings"], "helper": False},
                    "resume": {"name": "fake-resume.pdf", "type": "application/pdf", "size": len(RESUME), "updated": 1,
                               "data": base64.b64encode(RESUME).decode()}, **(extra or {})}
            driver.get(f"http://127.0.0.1:{PORT}/seed.html")
            WebDriverWait(driver, 10).until(lambda d: d.execute_script("return document.readyState") == "complete")
            driver.execute_script("window.postMessage({__jrSeed: arguments[0]}, '*')", data)
            ok = WebDriverWait(driver, 10).until(lambda d: d.execute_script("return document.documentElement.getAttribute('data-jr-seeded')"))
            check("storage seeded", ok == "ok", ok)

        def fill(frame=None):
            host = WebDriverWait(driver, 15).until(lambda d: d.find_element(By.CSS_SELECTOR, "#jobradar-autofill"))
            root = host.shadow_root
            WebDriverWait(driver, 10).until(lambda d: root.find_elements(By.CSS_SELECTOR, "button.launch, header button.primary"))
            root.find_elements(By.CSS_SELECTOR, "button.launch, header button.primary")[0].click()
            # The panel re-renders while filling, so read it fresh each time.
            read = "const h = document.querySelector('#jobradar-autofill'); const n = h && h.shadowRoot.querySelector(arguments[0]); return n ? n.innerText : '';"
            WebDriverWait(driver, 40).until(lambda d: d.execute_script(read, ".status").startswith("Filled "))
            return driver.execute_script(read, ".panel")

        state = lambda: driver.execute_script("return window.__mockState()")

        if "--live" in sys.argv:
            # Read-only on real forms: fake details, no resume, never submitted.
            for name, url, want in LIVE:
                print("- live", name)
                seed({"resume": None})
                driver.get(url)
                text = fill()
                for sel, v in want.items():
                    got = driver.execute_script("const e = document.querySelector(arguments[0]); return e ? e.value : null", sel)
                    check(f"{name} {sel}", got == v, f"got {got!r}")
                print("    " + next((l for l in text.splitlines() if l.startswith("Filled ")), ""))
            return finish()

        print("- greenhouse")
        seed()
        driver.get(f"http://127.0.0.1:{PORT}/greenhouse.html")
        text = fill()
        expect_state("greenhouse", state(), {
            "first_name": "Testy", "last_name": "McTestface", "email": "testy.mctestface@example.com", "country": "United States",
            "phone": "555-0100", "location": "Boston, Massachusetts, United States", "resume": RESUME_STATE, "cover_letter": "",
            "question_1": "https://www.linkedin.com/in/testy-mctestface-example", "question_3": "LinkedIn", "question_4": "Yes",
            "question_5": "Yes", "question_6": None, "school--0": "Northeastern University", "degree--0": "Bachelor's Degree",
            "4024620002": "Decline To Self Identify", "4024625002": ["Decline To Self Identify"], "4024621002": None, "consent": False})
        check("greenhouse: nothing submitted", driver.execute_script("return window.__submitted") == 0)
        check("greenhouse: hybrid question left for the user", "Do you acknowledge" in text)

        print("- greenhouse memory")
        driver.find_element(By.ID, "question_8").send_keys("Two weeks after an offer")
        time.sleep(1.6)
        driver.refresh()
        fill()
        expect_state("memory", state(), {"question_8": "Two weeks after an offer"})

        print("- greenhouse in a cross-origin iframe")
        seed()
        driver.get(f"http://127.0.0.1:{PORT}/embed-local.html")
        driver.switch_to.frame(driver.find_element(By.ID, "grnhse_iframe"))
        fill()
        expect_state("embed", state(), {"first_name": "Testy", "resume": RESUME_STATE, "question_5": "Yes"})
        driver.switch_to.default_content()

        print("- lever")
        driver.get(f"http://127.0.0.1:{PORT}/lever.html")
        fill()
        expect_state("lever", state(), {
            "resume": RESUME_STATE, "name": "Testy McTestface", "email": "testy.mctestface@example.com", "org": "Example Test Corp",
            "urls[LinkedIn]": "https://www.linkedin.com/in/testy-mctestface-example", "cards[a][field0]": "No", "cards[a][field1]": "Yes",
            "eeo[gender]": "Decline to self-identify", "eeo[veteran]": "Decline to self-identify"})
        check("lever: nothing submitted", driver.execute_script("return window.__submitted") == 0)

        print("- ashby")
        driver.get(f"http://127.0.0.1:{PORT}/ashby.html")
        fill()
        expect_state("ashby", state(), {
            "_systemfield_name": "Testy McTestface", "_systemfield_resume": RESUME_STATE, "cadd1ed8": "555-0100",
            "_systemfield_location": "Boston, Massachusetts, United States", "086af02e": "yes", "56f07ce0": None,
            "eeo_gender": "Decline to self-identify"})
        check("ashby: autofill pane untouched", state().get("autofillPane") is None)

        print("- workday")
        driver.get(f"http://127.0.0.1:{PORT}/workday.html")
        fill()
        expect_state("workday 1", state(), {
            "source--source": ["LinkedIn"], "country--country": "United States of America", "name--legalName--firstName": "Testy",
            "name--legalName--lastName": "McTestface", "address--city": "Boston", "phoneNumber--phoneNumber": "555-0100",
            "address--addressLine1": "", "phoneNumber--phoneType": ""})
        check("workday: no navigation by the extension", driver.execute_script("return window.__navClicks") == 0)
        driver.find_element(By.CSS_SELECTOR, "[data-automation-id='bottom-navigation-next-button']").click()
        time.sleep(1.2)
        fill()
        expect_state("workday 2", state(), {"resume": RESUME_STATE, "webAddress-1--url": "https://testy.example.com"})
        check("workday: only the test's own Next click", driver.execute_script("return window.__navClicks") == 1)
        return finish()
    finally:
        driver.quit()
        httpd.shutdown()
        shutil.rmtree(ext.parent, ignore_errors=True)
        (DIST / "embed-local.html").unlink(missing_ok=True)
        (DIST / "seed.html").unlink(missing_ok=True)


def finish():
    print("all passed" if not failures else f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
