# Onward Autofill

A browser extension that fills job applications on Greenhouse, Lever, Ashby and Workday from the Onward app: your Apply kit details, your standard and demographic answers, and the resume Onward picked for that job (tailored, or your master). It picks dropdown, radio and checkbox answers, fills work history from your master resume, remembers what you type into other questions, and drafts written answers on your ChatGPT or Claude login. A form for a job your apply queue prepared fills itself as soon as it opens.

It never submits anything. You always review the form and press Submit yourself.

Built for Firefox first (Manifest V3). The same folder loads in Chrome and Edge.

## Install

### Firefox (release)

Install the signed add-on once; it stays installed and updates itself from the GitHub releases. Any of:

- Onward's setup steps (or Settings, **Run setup again**): **Install in Firefox**.
- With Onward running, open <http://127.0.0.1:8765/api/extension.xpi> in Firefox, then **Continue to
  installation** and **Add**.
- From a GitHub release, drag `onward-autofill-<version>.xpi` into Firefox.

In `about:addons` it shows its version with no "temporary" label.

**Only for working on the extension itself:** a copy you changed isn't signed, and release Firefox runs unsigned
extensions only until it restarts. Remove the signed copy first, since both have the same id:

1. Open `about:debugging#/runtime/this-firefox`.
2. Press **Load Temporary Add-on…** and choose `extension/manifest.json`.

You have to repeat this after every Firefox restart. To install a copy you changed permanently, sign it for yourself (self-distribution, not listed on the store). `python packaging/extension.py` does this too, with the keys in `AMO_JWT_ISSUER` and `AMO_JWT_SECRET`:

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

The extension asks to read and change data on the four ATS sites, plus `127.0.0.1` and `localhost` for the Onward helper and dashboard. Firefox match patterns can't name a port, so access covers every local port, but the extension only talks to port 8765. If the options page shows **Allow access to job sites**, press it.

## Set up

There's nothing to set up in the extension. It reads everything from Onward on this computer, and keeps the last copy for when the app isn't running. Edit it in Onward's **Apply kit**:

- **Your details:** name, contact details, links, school. Workday asks for a **street address** and **phone type** on every application, with State and Postal Code, which come from your City, State and ZIP. Address Line 2, apartment and suite stay for you.
- **Standard questions:** work authorization, sponsorship now and later, unrestricted authorization, citizenship, relocation, age, how you heard about the job. Each starts as **Ask me each time**, and such questions are left for you until you set an answer.
- **Voluntary self-identification:** gender, race, veteran, disability and the rest default to **Decline to answer**. If a form has no decline option, the question is left for you.
- **Resume:** the master resume you gave Onward, as a PDF, unless the job has a tailored one.

The extension's options page (Firefox: `about:addons` → Onward Autofill → Preferences; Chrome: right-click the toolbar icon → Options) shows whether it reached Onward, and holds its behaviour switches and your saved answers. Before 0.3 the extension kept its own profile; the first time 0.3 reaches Onward, anything you had typed there that the Apply kit lacks moves into it.

## Use

On an application form, press the **Autofill** button in the bottom-right corner (top-right inside embedded forms), the toolbar icon, or **Alt+Shift+F**. A panel lists what was filled and what still needs you. Click a question in the panel to jump to it. Filled fields get a green outline; remembered and drafted answers get an amber one, so you can check them.

- **Workday** is a multi-step wizard, and each company's Workday site needs its own account. Create the account and sign in yourself. Then press **Fill this step** on each page. You move between steps yourself. Its search boxes (How Did You Hear About Us, Country Phone Code, School, Field of Study, Skills) are searched with your answer and the match is picked, or Workday picks the only match itself. Degrees are matched to the site's own names, abbreviations like "MS" and "M.S." included. Security clearance and the terms checkbox are left for you. Pages saved from live Workday sites, cleaned of personal details, are in `test/fixtures/workday-real/`; `node real-workday.mjs` checks the field detection against them.
- **Projects** from your master resume go into a form's Projects rows (added with Add like jobs) or into a plain "Projects" box, written out one project per block and kept within the box's limit.
- **Work experience and education** come from your master resume, field by field, with no AI. That covers job title, company, location, start and end dates, "I currently work here", and a role description made of that job's own bullets, one per line, within the field's limit. For education it covers school, degree, field of study, GPA and years. It works on Workday's My Experience step and on Greenhouse's Education and Employment rows.
  - On Workday, Fill this step adds the rows itself with each section's Add button, including sections that start empty with only an Add button: Work Experience, Education, Languages, Websites and Certifications. It never presses Next.
  - **Skills:** Workday's "Type to Add Skills" box gets the skills in your resume's skills section that fit the posting, searched and picked one at a time: the ones the posting names first, then close neighbours in the same kind of work (TypeScript for a React job), then ones any software job takes (Git). A general software posting takes general software skills; ML, data and mobile skills go in only when the posting is about them, so an HTML/CSS job never gets fine-tuning. When Onward knows the job, the keywords its match score uses (read from the full description it stored) count as named too; either way the posting's own text is read, on Workday from the site's job API, since the application steps don't show it. Skills left out, and skills the site doesn't list, are named in the panel. Turn off **Only add skills that fit the job** in the options to add every skill.
  - **Languages** come from your Apply kit. Add a level to fill the proficiency boxes, e.g. "English (Fluent), Hindi (Native)". "I am fluent" is ticked for Fluent or Native.
  - **Websites** come from your Apply kit's portfolio, GitHub and (if the form has no LinkedIn field) LinkedIn links. **Certifications** come from a Certifications section in your master resume, if it has one.
  - Rows the site already filled, for example from Workday's "Autofill with Resume", are matched to your resume by company or school name and left as they are. If they differ from your resume, the panel shows **Replace with my resume** for that section, with **Undo**.
- **Remembered answers:** answers you type into questions the extension doesn't know are saved, keyed by the question's wording. The next time the same question appears, short answers are filled in again and long written answers are offered with a **Use saved answer** button. Edit or delete them under **Saved answers** in the options. Onward keeps a copy of them, which a new install of the extension gets back.
- **What the AI is sent:** with each AI answer or draft, only your resume's jobs and projects that share real terms with the posting go along (at most three of each); the rest stay on this computer. Answers that name the company they were written for, or its products, or whose question names the company, aren't saved, since they only fit that company; **Remove answers tied to one company** in the options clears older ones.
- **AI answers for the rest:** after filling, the questions nothing saved could answer are sent in one batch to Onward, which answers them with the model picked under Settings, "Draft form answers with" (your ChatGPT or Claude login, or an OpenAI or Anthropic API key). This covers choice questions, short answers and essays. Answers are filled in with an amber outline and an "AI answer, check it" tag. The model uses only your resume, profile and the posting, and leaves questions it can't know (offer deadlines, consents, family ties) for you. Sponsorship, demographic, salary, legal and consent questions are never sent, and neither are work-history or education fields (job title, company, dates, role description, school, degree, field of study, GPA). Turn this off under Drafting answers in the options.
- **Drafts:** writing questions ("Why do you want to work here?") get a **Draft an answer** button. The draft goes into the field for you to edit and is never sent anywhere else. Drafting uses Onward (the installed app, or `python server.py` from source), your master resume, your "About me" notes, the job description (looked up from the posting's URL, otherwise read from the page), and the model picked in Onward's settings:
  - ChatGPT, through `codex` on your ChatGPT login;
  - Claude, through the `claude` CLI on your Claude login;
  - an OpenAI or Anthropic API key saved in Onward's settings, billed to that key within its monthly budget.
- **Redraft with a note:** any drafted or AI answer in the panel has **Redraft with a note**. Say what to change ("use my Onward project instead", "shorter") and press Enter: the model writes the answer again from the same resume and notes, with the current answer and your note. It replaces the answer only if you haven't edited it meanwhile; otherwise the new draft is shown for you to copy. Drafts only use what your resume and "About me" notes say, never your chat history.

## Apply queue

Onward prepares your best matches before you open them (see **Apply queue** in the dashboard): after each search, new postings from the last 7 days at or above your minimum match, up to the number you set (15 to start), get a resume tailored to them, or a note that your master resume already fits, plus drafts for the custom questions their form publishes (Greenhouse, Lever and Ashby). When you open one of these forms it fills itself, with no button to press:

- The resume field gets the tailored PDF, named `Firstname_Lastname_Company_Resume.pdf` (your name from the Apply kit). If you chose **Use master instead**, or the job wasn't prepared, it gets your master resume. The panel says which one it attached.
- Questions with a drafted answer are filled and marked like any AI answer (amber outline, "AI answer, check it"). The extension's own rules still decide which questions those may be: sponsorship, work authorization, demographic, salary, legal and consent questions only ever take your saved answers.
- After you press Submit, the ATS's "application received" page marks the job applied in Onward, and the next job in your list or queue opens after a few seconds (**Stay here** stops it). Greenhouse and Lever have their own confirmation URLs; for Ashby and Workday the thank-you message only counts in a tab that showed an Onward job's form.

## Safety rules

The extension never:

- presses Submit, Next, Apply, Save and Continue or any other button that sends the form or moves it along, and never submits a form;
- creates an account or signs in;
- answers sponsorship, work authorization or demographic questions without an answer in your Apply kit. Unrecognized questions in a demographic section are left alone too;
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
| `src/content/history.js` | Work Experience and Education rows, filled from the helper's `/api/resume/entries` (parsed from the master resume by `resume_tex.history()`). |
| `src/background.js` | Toolbar button, shortcut, and the only code that calls the helper. Keeps the last copy of `/api/kit` (the Apply kit, answers, master resume), backs saved answers up to Onward, and remembers which job each tab's form belongs to (session storage), for the confirmation page. |
| `src/options/` | Options page: the connection, the behaviour switches and the saved answers. |

The helper side is `/api/kit` and `/api/draft` in `server.py` (the kit itself is kept by `kit.py`) and `draft_answer()` in `tailor.py`; the apply queue's `/api/prepared` and `/api/applied` are in `server.py`, backed by `prep.py`. The endpoint accepts requests from the dashboard's origin, or from an extension origin that sends the `X-Onward-Client: autofill` header. Websites can't send that header without a CORS preflight, which the helper never answers.

## Tests

From `extension/test` (run `npm install` once):

| Command | What it checks |
|---|---|
| `npm test` | Rule unit tests, then the real extension in Chromium against mocks of each ATS (React 18, real react-select, a cross-origin iframe embed, a four-step Workday wizard). The mocks are served on the real ATS host names, and the test reads the pages' own React state. |
| `npm run test:firefox` | The same mocks in real Firefox, with the extension installed as a temporary add-on through Selenium. Needs `pip install selenium`, and Onward quit: it stands in for Onward on its port. |
| `npm run test:live` | Read-only runs on live Greenhouse, Lever and Ashby forms in both browsers. It uses obviously fake details, attaches no resume, and the Chromium run aborts any request that looks like a submission. |
| `node run-mocks.mjs --real-draft` | One real draft through your ChatGPT login. |
| `node prepared.mjs` | The apply queue, with a fake helper: prepared resume and drafted answers on the Greenhouse and Ashby mocks, a queued form filling itself, the master fallback, confirmation pages and opening the next job. Part of `npm test`. |
| `python test_draft_endpoint.py` | Who may call `/api/draft`, on a spare port. Run from the repository root. |

Workday mocks follow commonly seen `data-automation-id` names. Real Workday tenants vary and a live Workday form needs an account, so the automated tests can't cover live Workday; it has been used on real Workday applications by hand. If a live Workday step misses fields, look at the input's `data-automation-id` and its `formField-*` wrapper in devtools.
