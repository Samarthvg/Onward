# Onward

Finds fresh entry-level job postings straight from company hiring systems, scores them against your resume,
tailors your resume for the ones worth applying to, and fills in applications through a browser extension. You
always review the form and press Submit yourself. It runs on your own computer, and your resume and details
stay there.

- **Fresh postings, with proof.** Every 30 minutes it reads thousands of companies' own job boards (Greenhouse,
  Lever, Ashby, Workday, SmartRecruiters, iCIMS, Oracle, Eightfold, and Amazon's, Google's and Microsoft's own
  sites), adding more each run as it finds them in public lists of new-grad and internship postings. Each
  posting shows when the company posted it, with a link to check, how long it has been up, and whether it was
  reposted. Postings older than 30 days are left out (60 for new-grad programs).
- **Scores you can check.** Each posting's required and nice-to-have skills are matched word for word against
  your resume, with experience counted. Every score shows what you have and what's missing.
- **Visa signals.** "No sponsorship" and "no OPT" are read from the posting itself, beside the company's H-1B
  approvals from USCIS.
- **Today's picks and the Apply queue.** The Apply queue opens on the few best new fits of the day. Behind
  them, it prepares your best matches from the last 7 days (15 at a time to start): a tailored resume where
  that helps, and drafted answers for the form's own questions.
- **Honest tailoring.** Only skills your resume backs are added (a skill you confirm under Review skills may
  also go in the skills section), no numbers are invented, and it stays one page.
  Tailored resumes are named `Firstname_Lastname_Company_Resume.pdf`.
- **Autofill.** The Onward Autofill extension for Firefox fills Greenhouse, Lever, Ashby and Workday forms,
  attaches the prepared resume, and marks the job applied when the confirmation page appears. Its own README is
  [extension/README.md](extension/README.md).

## Install

Download the installer for your computer from the [latest release](../../releases/latest):

- **Windows:** `onward-<version>-windows-setup.exe`. Run it. It installs for your user only (no
  administrator rights needed) and adds Onward to the Start menu. The installers aren't code-signed yet, so
  Windows may say it protected your PC: choose **More info**, then **Run anyway**.
- **macOS:** `onward-<version>-macos.dmg`. Open it and drag Onward to Applications.
- **Linux:** `onward-<version>-x86_64.AppImage`. Make it executable (`chmod +x`) and run it.

The app sits in the system tray (the menu bar on macOS) and starts when you log in. The first time, it opens the
dashboard on a few setup steps: your resume, your details, the jobs you want, how AI should help, the browser
extension, and a first search. Close it at any point and it picks up there next time. After setup it searches
every 30 minutes while you're logged in.

Clicking the tray icon opens the dashboard in a window of its own (on Linux, in your browser); job links open in
your browser, where the extension is. The tray shows the last and next search, any error, and which AI sign-in
works. Its menu opens the dashboard or opens it in your browser instead, starts a search, turns Start at login
on or off, opens the data folder, installs updates, and quits.

The dashboard has four pages: **Apply queue** (today's picks, then the rest of the queue), **All jobs** (every
posting, with filters and a count for each time range), **How it works** (what scores, dates and badges mean)
and **Settings**.

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

- **Windows:** Settings > Apps > Installed apps > Onward > Uninstall. It removes the program, the Start
  menu entry and the login item, then asks whether to delete your data too (No keeps it).
- **macOS:** quit from the menu bar icon, turn off Start at login there first (or delete
  `~/Library/LaunchAgents/io.github.onward.plist`), then move Onward from Applications to the Trash.
- **Linux:** quit from the tray, delete `~/.config/autostart/onward.desktop` and the AppImage.

To remove your data as well, delete the data folder below.

## What lives where

Everything is on your computer. The data folder is:

| System  | Data folder                                                                  |
| ------- | ---------------------------------------------------------------------------- |
| Windows | `%LOCALAPPDATA%\Onward`                                                      |
| macOS   | `~/Library/Application Support/Onward`                                       |
| Linux   | `$XDG_DATA_HOME/onward` (usually `~/.local/share/onward`)                    |

A `JobRadar` folder from before the rename is moved there the first time the app starts. Set `ONWARD_HOME` to use another folder (`JOBRADAR_HOME` still works). A copy run from source that already keeps `jobs.db` in its own folder
keeps using that folder. Inside it:

- `settings.json`: your roles, companies, tailoring and app choices, and setup progress.
- `jobs.db`: postings, search history, tailoring history and the apply queue.
- `resume/`: your master resume(s) and tailored PDFs; your "about me" notes; the resume match scores use
  (`scoring.json`); your Apply kit details and Applied/Dismiss marks (`dashboard.json`).
- `data/`: H-1B employer data from USCIS's H-1B Employer Data Hub, if a role needs visa information: the two
  newest fiscal years, one small file each, refreshed every 90 days.
- `dashboard.html`, `radar.log`, `server.log`, `app-state.json`, `radar-status.json`: the page, logs and app state.
- `window/`: the dashboard window's own browser storage.
- `tools/`: Tectonic and the LaTeX packages it fetched, if you chose to download it.
- `extension/`: the installed app's copy of the browser extension, for Load unpacked.
- API keys: in the system keychain when one is available, otherwise in `secrets/api-keys.json`, readable only by you.

The dashboard window and any browser you open it in keep their own view settings (time range, theme, how many
picks), and a copy of your resume, Apply kit and marks. Those three also live in the data folder, so every copy
sees the same, and the most recent change wins. The extension keeps its own copy of your details and resume in
the browser.

The installed program is in `%LOCALAPPDATA%\Programs\Onward` (Windows) or `/Applications` (macOS); the
login item is the `Onward` value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, the
LaunchAgent above, or the autostart file above.

What leaves your computer:

- Requests to the companies' job boards and the public SimplifyJobs list.
- Once a day, downloads of a few public new-grad and internship lists on GitHub (SimplifyJobs, vanshb03,
  speedyapply). Only their links are used, to find more company job boards to read (`discover.py`).
- Every 90 days, USCIS's H-1B Employer Data Hub export for two fiscal years (about 90 MB each, kept as about
  1.5 MB of totals), if a role needs visa information.
- The daily update check, if on. Firefox also checks the same GitHub release for updates to the signed add-on.
- Tectonic's download, if you chose it.
- Whatever you send to the AI provider you chose: your resume, your details and the posting.
- Phone notifications, if you set up an ntfy topic.
- Downloads of a shared job feed, if you set one in Settings. Only downloads: the feed learns nothing about
  your roles, resume or applications.

Nothing is sent to Onward itself.

## Shared job feed

Searching about 4,000 company boards every 30 minutes is the same work on every computer. A shared feed
does it once: one search publishes its postings as static files, and every copy of Onward downloads them
instead of searching. Scoring, roles, tailoring and the apply queue stay on each computer. `feed.py` has
the details.

**Reading a feed.** Put its address in Settings, under Radar (advanced settings), or in `settings.json` as
`radar.feed_url`. A new copy downloads the whole snapshot once (a few MB per thousand postings); after
that each run downloads only what changed. Companies you added yourself are still searched from your
computer. If the feed can't be read, a file doesn't match its checksum, or the feed hasn't updated for
3 hours, Onward searches every board itself that run, as it does without a feed.

**Publishing one.** Give the search its own data folder with a broad role, since each reader filters by
its own roles: copy [packaging/feed/feed-settings.json](packaging/feed/feed-settings.json) there as
`settings.json`. It covers software roles from internship to mid level in the US. Then every 30 minutes:

```
ONWARD_HOME=/srv/onward-feed python radar.py --feed-out /srv/onward-feed/public
```

and copy `public/` to any static host or CDN, `index.json` last, so it only ever names files that are
already there. Old change files and snapshots are deleted as new ones are written: two days of changes
are kept, and a copy further behind reads the snapshot again.

[packaging/feed/README.md](packaging/feed/README.md) sets this up for free: GitHub Actions runs the search,
a Cloudflare Worker starts it every 30 minutes, and Cloudflare R2 holds the feed and the search's database.

## Running from source

Python 3.11 or newer; the core needs no packages. `pip install -r packaging/requirements.txt` adds the tray
icon, the dashboard window and the API-key models; without them the app runs without an icon and opens the
dashboard in your browser.

| Command | Does |
| --- | --- |
| `python app.py` | The tray app, as installed |
| `python app.py --window` | The dashboard in its own window |
| `python app.py --headless` | The app without a tray |
| `python radar.py` | One search (`--render` rebuilds the page from stored jobs; `--update-h1b` fetches H-1B data now) |
| `python server.py` | Only the dashboard and its helper |
| `python -m unittest` | The tests; the extension's are in [extension/README.md](extension/README.md) |
| `python preview.py` | This folder's code at http://127.0.0.1:8766/ with a copy of the installed app's data (`--fresh` copies it again), next to the installed app, to check changes before releasing them |

Run only one preview at a time: two copying at once leave an empty copy. Building and releasing the installers:
see [packaging/README.md](packaging/README.md).
