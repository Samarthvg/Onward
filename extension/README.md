# Job Radar Autofill

A browser extension that fills job applications on Greenhouse, Lever, Ashby and Workday with your saved details. It replaces the dashboard's "Fill application" bookmark: on top of the text fields the bookmark fills, it also attaches your resume, picks dropdown, radio and checkbox answers, answers standard questions (work authorization, sponsorship, relocation, how you heard about the job, demographic questions), remembers what you type into other questions, and can draft written answers on your ChatGPT or Claude login.

It never submits anything. You always review the form and press Submit yourself.

Built for Firefox first (Manifest V3). The same folder loads in Chrome and Edge.

## Install

### Firefox (release)

Release Firefox only runs signed extensions permanently. Without signing, you can load it until Firefox restarts:

1. Open `about:debugging#/runtime/this-firefox`.
2. Press **Load Temporary Add-on…** and choose `extension/manifest.json`.
3. The options page opens. Fill in your profile and choose your resume.

You have to repeat this after every Firefox restart. To install it permanently, sign it for yourself (self-distribution, not listed on the store):

1. Sign in at <https://addons.mozilla.org/developers/> with your own Firefox account, and create API credentials under **Tools → Manage API Keys**. Do this yourself: it's your account.
2. From `extension/test`, run, using your own key and secret:

   ```bash
   npx web-ext sign --source-dir .. --ignore-files test web-ext-artifacts --channel unlisted --api-key YOUR_JWT_ISSUER --api-secret YOUR_JWT_SECRET
   ```

3. Mozilla's automated review returns a signed `.xpi` in `extension/web-ext-artifacts/`. Drag it into Firefox to install. Bump `version` in `manifest.json` before you sign each new release.

### Firefox Developer Edition or Nightly

These can run unsigned extensions permanently: set `xpinstall.signatures.required` to `false` in `about:config`, run `npm run build` in `extension/test`, then install the zip from `extension/web-ext-artifacts/` through `about:addons` → gear → **Install Add-on From File…**.

### Chrome or Edge

1. Open `chrome://extensions` (or `edge://extensions`) and turn on **Developer mode**.
2. Press **Load unpacked** and choose the `extension` folder. It stays installed across restarts.

### Site access

The extension asks to read and change data on the four ATS sites, plus `127.0.0.1` and `localhost` for the Job Radar helper and dashboard. Firefox match patterns can't name a port, so access covers every local port, but the extension only talks to port 8765. If the options page shows **Allow access to job sites**, press it.

## Set up

Open the options page (Firefox: `about:addons` → Job Radar Autofill → Preferences; Chrome: right-click the toolbar icon → Options).

- **Resume:** choose your PDF once. It's stored in the extension and attached to each form's resume field. Each site still reads it its own way; check the result.
- **Profile:** press **Import from dashboard** to copy the Apply kit details from the Job Radar dashboard. The helper must be running; if no dashboard tab is open, the extension opens one in the background. Or press **Copy for extension** in the dashboard's Apply kit and paste the result here. Import only fills empty fields here.
- **Standard answers:** work authorization, sponsorship now and later, unrestricted authorization, citizenship, relocation, age, how you heard about the job. Each starts as **Ask me each time**, and such questions are left for you until you set an answer.
- **Voluntary self-identification:** gender, race, veteran, disability and the rest default to **Decline to answer**. If a form has no decline option, the question is left for you.

## Use

On an application form, press the **Autofill** button in the bottom-right corner (top-right inside embedded forms), the toolbar icon, or **Alt+Shift+F**. A panel lists what was filled and what still needs you. Click a question in the panel to jump to it. Filled fields get a green outline; remembered and drafted answers get an amber one, so you can check them.

- **Workday** is a multi-step wizard, and each company's Workday site needs its own account. Create the account and sign in yourself. Then press **Fill this step** on each page. You move between steps yourself.
- **Remembered answers:** answers you type into questions the extension doesn't know are saved, keyed by the question's wording. The next time the same question appears, short answers are filled in again and long written answers are offered with a **Use saved answer** button. Edit or delete them under **Saved answers** in the options.
- **AI answers for the rest:** after filling, the questions nothing saved could answer are sent in one batch to the helper on your ChatGPT login (or the backend chosen under Drafting answers). This covers choice questions, short answers and essays. Answers are filled in with an amber outline and an "AI answer, check it" tag. The model uses only your resume, profile and the posting, and leaves questions it can't know (offer deadlines, consents, family ties) for you. Sponsorship, demographic, salary, legal and consent questions are never sent. Turn this off under Drafting answers, or pick the Manual backend.
- **Drafts:** writing questions ("Why do you want to work here?") get a **Draft an answer** button. The draft goes into the field for you to edit and is never sent anywhere else. Drafting uses the Job Radar helper (`pythonw server.py`), your master resume uploaded in the dashboard, the job description (looked up from the posting's URL in `jobs.db`, otherwise read from the page), and your choice of backend:
  - ChatGPT, through `codex` on your ChatGPT login;
  - Claude, through the `claude` CLI on your Claude login;
  - the local Ollama model;
  - **Manual**, which shows a prompt to paste into any chat.

  The paid Anthropic API is never used for drafts.

## Apply queue

The Job Radar helper prepares good matches before you open them (see **Apply queue** in the dashboard): after each radar run, new postings at or above your match threshold get a resume tailored to them, or a note that your master resume already fits, plus drafts for the custom questions their form publishes (Greenhouse, Lever and Ashby). When you open one of these forms:

- The resume field gets the tailored PDF, under your stored resume's file name. If you chose **Use master instead**, the job wasn't prepared, or the helper isn't running, it gets your stored resume as before. The panel says which one it attached.
- Questions with a drafted answer are filled and marked like any AI answer (amber outline, "AI answer, check it"). The extension's own rules still decide which questions those may be: sponsorship, work authorization, demographic, salary, legal and consent questions only ever take your saved answers.
- After you press Submit, the ATS's "application received" page marks the job applied in Job Radar, and the panel offers the next job in the queue. Greenhouse and Lever have their own confirmation URLs; for Ashby and Workday the thank-you message only counts in a tab that showed a Job Radar job's form.

## Safety rules

The extension never:

- presses Submit, Next, Apply, Save and Continue or any other button that sends the form or moves it along, and never submits a form;
- creates an account or signs in;
- answers sponsorship, work authorization or demographic questions without an answer you saved in the options. Unrecognized questions in a demographic section are left alone too;
- answers salary, consent, signature, background-check, export-control, clearance or other legal questions, or ticks agreement boxes;
- fills middle name, pronouns, address lines, phone extension or phone country code, or a preferred name you haven't set. Fields about someone else (emergency contact, referrer, reference) are left alone too;
- uses a label it can't tie to one field: a wrapper's label is only trusted when the wrapper holds a single question;
- overwrites something already in a field, or picks a dropdown option when more than one could match your answer;
- remembers values it filled itself. Only your own typing and clicking count.

## How it works

| File | Job |
|---|---|
| `src/shared/rules.js` | Question classification, answer lookup and option matching. Pure functions, unit tested. |
| `src/content/sites.js` | Finds questions per ATS and gives each the label that belongs to it. |
| `src/content/widgets.js` | Reads and sets each control: text, native selects, react-select, comboboxes, radios, checkboxes, Ashby yes/no buttons, Workday listboxes and search prompts, file inputs (through `DataTransfer`). |
| `src/content/main.js` | Runs the fill, remembers answers, drafts, follows Workday steps. Waits for the page to finish loading and go quiet first, because adding anything to a React page before it hydrates breaks it (Greenhouse's Remix boards). |
| `src/content/panel.js` | The in-page panel, in a shadow root. |
| `src/background.js` | Toolbar button, shortcut, and the only code that calls the helper. Remembers which job each tab's form belongs to (session storage), for the confirmation page. |
| `src/options/` | Options page. |

The helper side is `/api/draft` in `server.py` and `draft_answer()` in `tailor.py`; the apply queue's `/api/prepared` and `/api/applied` are in `server.py`, backed by `prep.py`. The endpoint accepts requests from the dashboard's origin, or from an extension origin that sends the `X-JobRadar-Client: autofill` header. Websites can't send that header without a CORS preflight, which the helper never answers.

## Tests

From `extension/test` (run `npm install` once):

| Command | What it checks |
|---|---|
| `npm test` | Rule unit tests, then the real extension in Chromium against mocks of each ATS (React 18, real react-select, a cross-origin iframe embed, a four-step Workday wizard). The mocks are served on the real ATS host names, and the test reads the pages' own React state. |
| `npm run test:firefox` | The same mocks in real Firefox, with the extension installed as a temporary add-on through Selenium. Needs `pip install selenium`. |
| `npm run test:live` | Read-only runs on live Greenhouse, Lever and Ashby forms in both browsers. It uses obviously fake details, attaches no resume, and the Chromium run aborts any request that looks like a submission. |
| `node dashboard-import.mjs` | Profile import from the running dashboard, both ways. Needs the helper running. |
| `node run-mocks.mjs --real-draft` | One real draft through your ChatGPT login. |
| `node prepared.mjs` | The apply queue, with a fake helper: prepared resume and drafted answers on the Greenhouse and Ashby mocks, the master fallback, and confirmation pages. Part of `npm test`. |
| `python test_draft_endpoint.py` | Who may call `/api/draft`, on a spare port. Run from the `jobradar` folder. |

Workday mocks follow commonly seen `data-automation-id` names. Real Workday tenants vary, and a live Workday form needs an account, so Workday hasn't been checked on a live site. If a live Workday step misses fields, look at the input's `data-automation-id` and its `formField-*` wrapper in devtools.
