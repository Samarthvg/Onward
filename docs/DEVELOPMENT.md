# Developing Onward

Everything here is for working on Onward itself. To use it, install it from the
[latest release](https://github.com/Samarthvg/Onward/releases/latest) and see the [README](../README.md).

## Running from source

Python 3.11 or newer. The core needs no packages; `pip install -r packaging/requirements.txt` adds the tray
icon, the dashboard window and the API-key models. Without them the app runs without an icon and opens the
dashboard in your browser.

| Command | Does |
| --- | --- |
| `python app.py` | The tray app, as installed |
| `python app.py --window` | The dashboard in its own window |
| `python app.py --headless` | The app without a tray |
| `python radar.py` | One search (`--render` rebuilds the page from stored jobs; `--update-h1b` fetches H-1B data now) |
| `python server.py` | Only the dashboard and its helper |

A copy run from source keeps its data in the same folder as the installed app (see the README's
"Your data"), unless that folder already has its own `jobs.db`. To keep it apart, set `ONWARD_HOME` to
another folder (`JOBRADAR_HOME` still works), and `JOBRADAR_PORT` to run its helper next to an installed copy;
the extension only talks to port 8765.

## Tests

| Command | Checks |
| --- | --- |
| `python -m unittest` | The app: sources, parsing, scoring, tailoring, the queue, settings, the helper, startup |
| `python -m unittest tests.test_match` | One file |
| `cd extension/test && npm install && npm test` | The extension, in Chromium against mocks of each application site |

Some tests compile real LaTeX and are skipped without `pdflatex` or `xelatex`. The extension's other suites
(Firefox, live sites) are in [extension/README.md](../extension/README.md#tests). The **Tests** workflow runs
the app's tests on Windows and Linux, and the extension's rule tests, on every push and pull request.

## Layout

| | |
| --- | --- |
| `app.py` | The app: tray icon, dashboard window, timed searches, updates |
| `server.py` | The local helper at 127.0.0.1:8765: serves the dashboard and its API, runs tailoring and drafts |
| `radar.py` | One search: reads the boards (or the shared feed), filters, stores and renders the dashboard |
| `feed.py`, `discover.py` | The shared job feed; finding more boards from public new-grad lists |
| `filters.py`, `reqparse.py`, `skillset.py`, `match.py` | Roles, reading requirements, skills, the match score |
| `prep.py`, `questions.py` | The apply queue; a form's custom questions |
| `tailor.py`, `resume_tex.py`, `resume_import.py`, `texengine.py` | Tailoring, the LaTeX resume, PDF engines |
| `ai.py`, `pricing.py`, `voice.py` | API-key models and their costs; the writing voice for drafts |
| `config.py`, `appinfo.py`, `autostart.py`, `updates.py`, `undo.py`, `notify.py`, `oslock.py`, `scheduler.py`, `ats.py` | Settings, names and version, login item, update check, undo, notifications and the rest |
| `dashboard_template.html`, `loading.html` | The dashboard (one page, no build step); the loading screen |
| `skills.json`, `companies.json` | The skill taxonomy; the built-in company boards |
| `extension/` | The Onward Autofill browser extension |
| `packaging/` | Installers, the signed extension, releases; `packaging/feed/` hosts the shared feed |
| `tests/` | The app's tests and their saved fixtures |

## Releasing

Building the installers and publishing a release: [packaging/README.md](../packaging/README.md). Hosting
the shared job feed: [packaging/feed/README.md](../packaging/feed/README.md).
