# It gets easier

Finds fresh entry-level job postings straight from company hiring systems, scores them against your resume,
tailors your resume for the ones you pick, and fills in applications through a browser extension. It runs on
your own computer, and your resume and details stay there.

## Install

Download the installer for your computer from the [latest release](../../releases/latest):

- **Windows:** `it-gets-easier-<version>-windows-setup.exe`. Run it. It installs for your user only (no
  administrator rights needed) and adds It gets easier to the Start menu.
- **macOS:** `it-gets-easier-<version>-macos.dmg`. Open it and drag It gets easier to Applications.
- **Linux:** `it-gets-easier-<version>-x86_64.AppImage`. Make it executable (`chmod +x`) and run it.

The app sits in the system tray (the menu bar on macOS) and starts when you log in. The first time, it opens the
dashboard in your browser on a few setup steps: your resume, your details, the jobs you want, how AI should help,
the browser extension, and a first search. Close the page at any point and it picks up there next time. After
setup it searches every 30 minutes while you're logged in. The tray icon shows the last and next search, any
error, and which AI sign-in works. Its menu opens the dashboard, starts a search, turns Start at login on or
off, and quits.

Optional pieces the setup steps explain when they come up:

- **AI:** your ChatGPT or Claude account (through the free `codex` or `claude` command-line tools, signed in
  once), or your own OpenAI or Anthropic API key, or none (you paste prompts into any chat yourself).
- **Tailored PDFs:** an installed TeX (MiKTeX, TeX Live, MacTeX) is used when there is one. Otherwise the app
  offers to download [Tectonic](https://tectonic-typesetting.github.io) into its data folder.
  - A `.tex` resume is tailored in place, keeping its layout.
  - A PDF or text resume is first turned into a `.tex` with a plain layout, which you check once.
  - Without either, scoring and the Apply kit still work. Word files: save as PDF first.

Updates: once a day the app asks GitHub whether a newer release exists (turn this off under Settings, App). On
Windows it can install the update from the tray menu; on macOS and Linux it opens the download page.

## Uninstall

- **Windows:** Settings > Apps > Installed apps > It gets easier > Uninstall. It removes the program, the Start
  menu entry and the login item, then asks whether to delete your data too (No keeps it).
- **macOS:** quit from the menu bar icon, turn off Start at login there first (or delete
  `~/Library/LaunchAgents/io.github.itgetseasier.plist`), then move It gets easier from Applications to the Trash.
- **Linux:** quit from the tray, delete `~/.config/autostart/it-gets-easier.desktop` and the AppImage.

To remove your data as well, delete the data folder below.

## What lives where

Everything is on your computer. The data folder is:

| System  | Data folder                                                                  |
| ------- | ---------------------------------------------------------------------------- |
| Windows | `%LOCALAPPDATA%\JobRadar`                                                    |
| macOS   | `~/Library/Application Support/JobRadar`                                     |
| Linux   | `$XDG_DATA_HOME/jobradar` (usually `~/.local/share/jobradar`)                |

Set `JOBRADAR_HOME` to use another folder. A copy run from source that already keeps `jobs.db` in its own folder
keeps using that folder. Inside it:

- `settings.json`: your roles, companies, tailoring and app choices, and setup progress.
- `jobs.db`: postings, search history, tailoring history and the apply queue.
- `resume/`: your master resume(s), tailored PDFs, your "about me" notes.
- `data/`: public H-1B employer data from USCIS, if a role needs visa information.
- `dashboard.html`, `radar.log`, `server.log`, `app-state.json`, `radar-status.json`: the page, logs and app state.
- `tools/`: Tectonic and the LaTeX packages it fetched, if you chose to download it.
- `extension/`: the installed app's copy of the browser extension, for Load unpacked.
- API keys: in the system keychain when one is available, otherwise in `secrets/api-keys.json`, readable only by you.

Your browser keeps the dashboard's view settings, the resume text used for match scores and your Apply kit
details for `127.0.0.1:8765`. The extension keeps its own copy of your details and resume in the browser.

The installed program is in `%LOCALAPPDATA%\Programs\It gets easier` (Windows) or `/Applications` (macOS); the
login item is the `It gets easier` value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, the
LaunchAgent above, or the autostart file above.

What leaves your computer:

- Requests to the companies' job boards and the public SimplifyJobs list.
- Once a day, downloads of a few public new-grad and internship lists on GitHub (SimplifyJobs, vanshb03,
  speedyapply). Only their links are used, to find more company job boards to read (`discover.py`).
- The USCIS data files.
- The daily update check, if on.
- Tectonic's download, if you chose it.
- Whatever you send to the AI provider you chose: your resume, your details and the posting.
- Phone notifications, if you set up an ntfy topic.

Nothing is sent to It gets easier itself.

## Running from source

Python 3.11 or newer; the core needs no packages. `python app.py` runs the tray app (`pip install -r
packaging/requirements.txt` for the tray icon and the API-key models; without them it runs without an icon).
`python app.py --headless` runs without a tray, `python radar.py` does one search, `python server.py` serves only
the dashboard. Tests: `python -m unittest`. Building the installers: see [packaging/README.md](packaging/README.md).
