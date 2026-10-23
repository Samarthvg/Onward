// End-to-end tests: the real extension in Chromium against mocks of each
// ATS served on the real host names. Checks the pages' own React state, that
// nothing was submitted or advanced, and that saved answers come back.
import { launch, seed, runFill, panelText, expectState, expect, results, RESUME_BYTES } from "./harness.mjs";

const headless = !process.argv.includes("--headed");
const only = process.argv.find((a) => a.startsWith("--only="))?.slice(7);
const RESUME = "fake-resume.pdf:" + RESUME_BYTES.length;
const env = await launch({ headless });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function test(name, fn) {
  if (only && !name.includes(only)) return;
  console.log("•", name);
  await seed(env);
  const page = await env.ctx.newPage();
  page.on("pageerror", (e) => console.log("  page error:", e.message));
  try { await fn(page); }
  catch (e) { expect(name, false, e.stack); }
  finally { await page.close(); }
}

const GH_WANT = {
  first_name: "Testy", last_name: "McTestface", preferred_name: "Testy", email: "testy.mctestface@example.com",
  country: "United States", phone: "555-0100", location: "Boston, Massachusetts, United States", resume: RESUME, cover_letter: "",
  question_1: "https://www.linkedin.com/in/testy-mctestface-example", question_2: "https://testy.example.com", question_3: "LinkedIn",
  question_4: "Yes", question_5: "Yes", question_6: null, question_7: null, question_8: "", why: "",
  // Education rows come from the (fixture) master resume's first school, not the profile.
  "school--0": "Example State University", "degree--0": "Master's Degree",
  // So do employment rows: the first job, with months picked from a select.
  "company-name--0": "Example Labs", "title--0": "Full Stack Developer Intern",
  "start-date-month--0": "7", "start-date-year--0": "2025", "end-date-month--0": "1", "end-date-year--0": "2026",
  "current-role--0": false,
  "4024620002": "Decline To Self Identify", "4024625002": ["Decline To Self Identify"], "4024623002": "I don't wish to answer",
  "4024624002": "I do not want to answer", "4024621002": null, consent: false,
};

await test("greenhouse: fills text, react-select, async select, combobox, resume and EEO", async (page) => {
  await page.goto("https://job-boards.greenhouse.io/mockco/jobs/1001");
  const text = await runFill(page);
  expectState("greenhouse", await page.evaluate(() => window.__mockState()), GH_WANT);
  expect("greenhouse: nothing submitted", (await page.evaluate(() => window.__submitted)) === 0);
  expect("greenhouse: hybrid acknowledgement left for the user", /Do you acknowledge/.test(text));
  expect("greenhouse: consent left for the user", /I consent/.test(text));
});

await test("greenhouse: remembers custom answers and offers written ones", async (page) => {
  await page.goto("https://job-boards.greenhouse.io/mockco/jobs/1001");
  await page.locator("#jobradar-autofill").waitFor({ state: "attached" });
  // The user answers three custom questions by hand.
  await page.click("#question_7");
  await page.locator("#react-select-question_7-listbox [role=option]", { hasText: "No" }).click();
  await page.fill("#question_8", "Two weeks after an offer");
  await page.fill("#question_9", "I like building developer tools. (TEST ANSWER)");
  await sleep(1500);
  await page.reload();
  const text = await runFill(page);
  const st = await page.evaluate(() => window.__mockState());
  expectState("memory", st, { question_7: "No", question_8: "Two weeks after an offer", why: "", first_name: "Testy" });
  expect("memory: written answer offered, not filled", /Use saved answer/.test(text), text);
  await page.locator("#jobradar-autofill button", { hasText: "Use saved answer" }).click();
  await sleep(300);
  expectState("memory: use saved", await page.evaluate(() => window.__mockState()), { why: "I like building developer tools. (TEST ANSWER)" });
  // Saved answers show in the options page.
  const opt = await env.ctx.newPage();
  await opt.goto(`chrome-extension://${env.id}/src/options/options.html`);
  await opt.locator("#memList li").first().waitFor();
  const memText = await opt.locator("#memList").innerText();
  expect("options lists saved answers", /When can you start/.test(memText) && /non-compete/.test(memText), memText);
  await opt.close();
});

await test("greenhouse: saved answers written for another company stay behind a warning", async (page) => {
  const now = Date.now();
  await seed(env, {
    // Written at ElevenLabs and naming it: warned, never filled on its own.
    "mem:why do you want to work at mock co": { q: "Why do you want to work at Mock Co?", writing: true, kind: "textarea", company: "ElevenLabs",
      names: ["ElevenLabs", "ElevenMusic"], a: "I want to build ElevenMusic at ElevenLabs. (TEST ANSWER)", updated: now },
    "mem:when can you start": { q: "When can you start?", kind: "text", company: "ElevenLabs", a: "Right after my ElevenLabs offer deadline", updated: now },
    // Neutral: still reused at any company.
    "mem:are you currently subject to any agreement with a former employer such as a non compete agreement that may limit your ability to perform the duties of the position":
      { q: "non-compete", kind: "rselect", company: "ElevenLabs", a: "No", updated: now },
  });
  await page.goto("https://job-boards.greenhouse.io/mockco/jobs/1001");
  const text = await runFill(page);
  const st = await page.evaluate(() => window.__mockState());
  expectState("cross-company", st, { why: "", question_8: "", question_7: "No" });
  expect("cross-company: warning shown", /written for ElevenLabs and mentions ElevenLabs, ElevenMusic/.test(text), text);
  const panel = page.locator("#jobradar-autofill .panel");
  expect("cross-company: no one-click reuse", (await panel.locator("button", { hasText: "Use saved answer" }).count()) === 0);
  await panel.locator("li", { hasText: "Why do you want to work at Mock Co" }).locator("button", { hasText: "Show it" }).click();
  await panel.locator("button", { hasText: "Use it anyway" }).first().click();
  await sleep(300);
  expectState("cross-company: second click uses it", await page.evaluate(() => window.__mockState()), { why: "I want to build ElevenMusic at ElevenLabs. (TEST ANSWER)" });
  const log = await env.sw.evaluate(() => chrome.storage.local.get(null));
  const uses = log["use:why do you want to work at mock co"] || [];
  expect("use logged", uses.length === 1 && uses[0].company === "Mock Co", JSON.stringify(uses));
  const nc = Object.keys(log).find((k) => k.startsWith("use:are you currently subject"));
  expect("auto-filled use logged", nc && log[nc].length === 1, Object.keys(log).join(","));
  expect("warned short answer not used", !log["use:when can you start"]);
});

await test("greenhouse: answers saved at the same company are offered as before", async (page) => {
  await seed(env, {
    "mem:why do you want to work at mock co": { q: "Why do you want to work at Mock Co?", writing: true, kind: "textarea", company: "Mock Co",
      names: ["Mock Co"], a: "Mock Co builds the tools I use every day. (TEST ANSWER)", updated: Date.now() },
    "mem:when can you start": { q: "When can you start?", kind: "text", company: "Mock Co", a: "Mock Co can expect me in June", updated: Date.now() },
  });
  await page.goto("https://job-boards.greenhouse.io/mockco/jobs/1001");
  const text = await runFill(page);
  expectState("same company", await page.evaluate(() => window.__mockState()), { question_8: "Mock Co can expect me in June", why: "" });
  expect("same company: plain offer", /Use saved answer/.test(text) && !/written for/.test(text), text);
});

await test("greenhouse: works inside a company site's iframe embed", async (page) => {
  await page.goto("https://careers.mockco.test/jobs");
  const frame = page.frameLocator("#grnhse_iframe");
  await runFill(frame);
  const f = page.frames().find((x) => x.url().includes("embed/job_app"));
  expectState("embed", await f.evaluate(() => window.__mockState()), { first_name: "Testy", email: "testy.mctestface@example.com", resume: RESUME, question_5: "Yes" });
  expect("embed: no panel on the host page", (await page.locator("#jobradar-autofill").count()) === 0);
});

await test("greenhouse: leaves sponsorship alone without a saved answer", async (page) => {
  await seed(env, { answers: { sponsorNow: "", sponsorFuture: "", workAuth: "" } });
  await page.goto("https://job-boards.greenhouse.io/mockco/jobs/1001");
  const text = await runFill(page);
  expectState("no answers", await page.evaluate(() => window.__mockState()), { question_4: null, question_5: null, first_name: "Testy" });
  expect("no answers: listed as needing the user", /require immigration support/.test(text));
});

await test("lever: fills text, links, radios, native selects and resume", async (page) => {
  await page.goto("https://jobs.lever.co/mockco/abc-123/apply");
  const text = await runFill(page);
  expectState("lever", await page.evaluate(() => window.__mockState()), {
    resume: RESUME, name: "Testy McTestface", email: "testy.mctestface@example.com", phone: "555-0100", location: "Boston, MA, USA",
    org: "Example Test Corp", "urls[LinkedIn]": "https://www.linkedin.com/in/testy-mctestface-example",
    "urls[GitHub]": "https://github.com/testy-example", "urls[Portfolio]": "https://testy.example.com",
    "cards[a][field0]": "No", "cards[a][field1]": "Yes", "cards[b][field0]": null, "cards[b][field1]": "", "cards[b][field2]": "",
    "cards[b][field3]": null, "eeo[gender]": "Decline to self-identify", "eeo[race]": "Decline to self-identify",
    "eeo[veteran]": "Decline to self-identify", "consent[marketing]": null,
  });
  expect("lever: nothing submitted", (await page.evaluate(() => window.__submitted)) === 0);
  expect("lever: AI-agent question left for the user", /built and deployed/.test(text));
});

await test("ashby: fills inputs, yes/no buttons, location combobox, resume and EEO radios", async (page) => {
  await page.goto("https://jobs.ashbyhq.com/mockco/abc-123/application");
  const text = await runFill(page);
  expectState("ashby", await page.evaluate(() => window.__mockState()), {
    _systemfield_name: "Testy McTestface", _systemfield_email: "testy.mctestface@example.com",
    "74f31fc6": "https://www.linkedin.com/in/testy-mctestface-example", _systemfield_resume: RESUME, cadd1ed8: "555-0100",
    _systemfield_location: "Boston, Massachusetts, United States", "4645121d": "", "56f07ce0": null, "086af02e": "yes",
    team: "", d9d19e65: "", cd46f09e: "", eeo_gender: "Decline to self-identify", eeo_veteran: "I don't wish to answer",
    autofillPane: null,
  });
  expect("ashby: nothing submitted", (await page.evaluate(() => window.__submitted)) === 0);
  expect("ashby: office question left for the user", /SF office/.test(text));
});

await test("workday: fills each step only when asked, never presses Next", async (page) => {
  await page.goto("https://mockco.wd5.myworkdayjobs.com/en-US/External/job/Boston-MA/Software-Engineer_JR1001/apply/applyManually");
  await runFill(page);
  expectState("workday step 1", await page.evaluate(() => window.__mockState()), {
    "source--source": ["LinkedIn"], previousWorker: "", "country--country": "United States of America",
    "name--legalName--firstName": "Testy", "name--legalName--middleName": "", "name--legalName--lastName": "McTestface",
    "address--addressLine1": "", "address--city": "Boston", "address--postalCode": "02115",
    "emailAddress--emailAddress": "testy.mctestface@example.com", "phoneNumber--phoneType": "",
    "phoneNumber--countryPhoneCode": [], "phoneNumber--phoneNumber": "555-0100",
  });
  expect("workday: no navigation by the extension", (await page.evaluate(() => window.__navClicks)) === 0);

  const next = async () => { await page.click("[data-automation-id='bottom-navigation-next-button']"); await sleep(900); };
  await next();   // the user moves on
  const panel = page.locator("#jobradar-autofill .panel");
  await panel.locator("button", { hasText: "Fill this step" }).waitFor({ timeout: 5000 });
  await runFill(page);
  const DESC2 = "Shipped React.js and Angular dashboards for 3 client products, cutting page load time by 30%.\n" +
    "Wrote Node.js microservices over MongoDB with 85% test coverage.";
  const DESC1 = "Built offline-first estimate screens in Vue.js backed by PouchDB sync, used by 40 field staff.\n" +
    "Extended REST endpoints in Node.js & Express for quote approvals, with unit tests.\n" +
    "Completed an internal Generative AI certification course.";
  const w = (n, f) => `workExperience-${n}--${f}`;
  let st = await page.evaluate(() => window.__mockState());
  expectState("workday step 2", st, {
    resume: RESUME, "socialNetworkAccounts--linkedInAccount": "https://www.linkedin.com/in/testy-mctestface-example",
    "webAddress-1--url": "https://testy.example.com",
    // Two rows added for the resume's other jobs, filled from their own entries.
    __jobs: 3, __schools: 2,
    [w(2, "jobTitle")]: "Full stack Developer", [w(2, "companyName")]: "Example Solutions Pvt Ltd", [w(2, "location")]: "Gujarat, India",
    [w(2, "startDate-dateSectionMonth-input")]: "06", [w(2, "startDate-dateSectionYear-input")]: "2022",
    [w(2, "endDate-dateSectionMonth-input")]: "08", [w(2, "endDate-dateSectionYear-input")]: "2023",
    [w(2, "currentlyWorkHere")]: false, [w(2, "roleDescription")]: DESC2,
    [w(3, "companyName")]: "Example Startup", [w(3, "currentlyWorkHere")]: true, [w(3, "endDate-dateSectionYear-input")]: "",
    [w(3, "roleDescription")]: "Maintain the billing service in Python.",
    // The parser's row keeps its values, garbled description included, until the user asks.
    [w(1, "jobTitle")]: "Full Stack Developer Intern", [w(1, "roleDescription")]: ".js",
    "education-1--schoolName": "Example State University", "education-1--degree": "Master's Degree",
    "education-1--fieldOfStudy": ["Computer Science"], "education-1--gradeAverage": "4",
    "education-1--firstYearAttended-dateSectionYear-input": "2024", "education-1--lastYearAttended-dateSectionYear-input": "2026",
    "education-2--schoolName": "Example Institute of Technology", "education-2--degree": "Bachelor's Degree",
    "education-2--fieldOfStudy": ["Computer Engineering"], "education-2--gradeAverage": "3.5",
  });
  expect("workday: Add pressed, never Next", (await page.evaluate(() => window.__navClicks)) === 1);
  const sec = panel.locator(".sec");
  expect("workday: both sections offer Replace", (await sec.count()) === 2, await panel.innerText());
  await sec.filter({ hasText: "Work Experience" }).locator("button", { hasText: "Replace with my resume" }).click();
  await sec.filter({ hasText: "replaced" }).first().waitFor({ timeout: 5000 });
  await page.locator("body").click({ position: { x: 5, y: 5 } });   // let Workday's fields commit on blur
  st = await page.evaluate(() => window.__mockState());
  expectState("workday replace", st, { [w(1, "roleDescription")]: DESC1, "education-1--gradeAverage": "4" });
  await sec.filter({ hasText: "Work Experience" }).locator("button", { hasText: "Undo" }).click();
  await sec.filter({ hasText: "Work Experience" }).locator("button", { hasText: "Replace with my resume" }).waitFor({ timeout: 5000 });
  expectState("workday undo", await page.evaluate(() => window.__mockState()), { [w(1, "roleDescription")]: ".js" });
  expect("workday: AI never asked about history fields", !/Role Description[\s\S]*AI answer/.test(await panel.innerText()));
  await next();
  await runFill(page);
  expectState("workday step 3", await page.evaluate(() => window.__mockState()), { q1: "Yes", q2: "Yes", q3: "Yes", q4: "" });
  await next();
  await runFill(page);
  expectState("workday step 4", await page.evaluate(() => window.__mockState()), {
    gender: "I do not wish to answer", ethnicity: "I do not wish to answer.", veteran: "I do not wish to self-identify", agreement: false,
  });
  expect("workday: only the test's own 3 Next clicks", (await page.evaluate(() => window.__navClicks)) === 3);
  expect("workday: nothing submitted", (await page.evaluate(() => window.__submitted)) === 0);
});

await test("workday: empty sections get rows from the resume, plus skills and languages", async (page) => {
  await page.goto("https://mockco.wd5.myworkdayjobs.com/en-US/External/job/Boston-MA/Software-Engineer_JR1001/apply/applyManually?empty=1");
  await page.locator("#jobradar-autofill").waitFor({ state: "attached" });
  await page.click("[data-automation-id='bottom-navigation-next-button']");   // the user moves to My Experience
  await sleep(900);
  const text = await runFill(page, 120000);
  const w = (n, f) => `workExperience-${n}--${f}`;
  const st = await page.evaluate(() => window.__mockState());
  expectState("empty workday", st, {
    // Rows added with each section's Add button, found from its heading alone.
    __jobs: 3, __schools: 2, __langs: 2,
    [w(1, "companyName")]: "Example Labs", [w(1, "jobTitle")]: "Full Stack Developer Intern",
    [w(1, "startDate-dateSectionMonth-input")]: "07", [w(1, "endDate-dateSectionYear-input")]: "2026",
    [w(2, "companyName")]: "Example Solutions Pvt Ltd", [w(3, "currentlyWorkHere")]: true,
    "education-1--schoolName": "Example State University", "education-1--degree": "Master's Degree",
    "education-2--schoolName": "Example Institute of Technology",
    // Every resume skill the site lists, one search at a time.
    "skills--skills": ["Python", "Java", "TypeScript", "JavaScript", "SQL", "Node.js", "Express.js", "React.js", "Angular"],
    "language-1--language": "English", "language-1--native": true, "language-1--reading": "Fluent", "language-1--writing": "Fluent",
    "language-2--language": "Spanish", "language-2--native": false, "language-2--speaking": "Intermediate",
    // Websites from the profile (LinkedIn has its own field); no certifications in the resume, so no rows.
    __sites: 2, "webAddress-1--url": "https://testy.example.com", "webAddress-2--url": "https://github.com/testy-example",
    __certs: 0,
  });
  expect("empty workday: missing skill reported", /Skills not in this site's list[\s\S]*Vue\.js/.test(text), text);
  expect("empty workday: only the user's own Next", (await page.evaluate(() => window.__navClicks)) === 1);
  expect("empty workday: nothing submitted", (await page.evaluate(() => window.__submitted)) === 0);
});

await test("toolbar button fills the active tab", async (page) => {
  await page.goto("https://jobs.lever.co/mockco/abc-123/apply");
  await page.locator("#jobradar-autofill").waitFor({ state: "attached" });
  await page.bringToFront();
  // Same code path as the toolbar button and Alt+Shift+F.
  await env.sw.evaluate(async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    await fillTab(tab);
  });
  await page.locator("#jobradar-autofill .status").filter({ hasText: /^Filled \d+/ }).waitFor({ timeout: 15000 });
  expectState("toolbar", await page.evaluate(() => window.__mockState()), { name: "Testy McTestface" });
});

await test("draft (manual mode) shows a prompt built by the helper", async (page) => {
  await page.goto("https://job-boards.greenhouse.io/mockco/jobs/1001");
  const text = await runFill(page);
  if (!/Draft an answer/.test(text)) { expect("draft button offered", false, text); return; }
  const status = await env.sw.evaluate(async () => { try { return (await fetch("http://127.0.0.1:8765/api/status")).ok; } catch (e) { return false; } });
  if (!status) { console.log("  (skipped: helper not running)"); return; }
  await page.locator("#jobradar-autofill button", { hasText: "Draft an answer" }).first().click();
  const ta = page.locator("#jobradar-autofill textarea");
  await ta.waitFor({ timeout: 15000 }).catch(() => {});
  const prompt = (await ta.count()) ? await ta.inputValue() : await panelText(page);
  expect("draft prompt has the question and resume", /Why do you want to work at Mock Co/.test(prompt) && /RESUME/.test(prompt), prompt.slice(0, 400));
  expectState("draft: nothing inserted in manual mode", await page.evaluate(() => window.__mockState()), { why: "" });
});

// Uses the ChatGPT login through the helper, so it only runs when asked.
if (process.argv.includes("--real-draft")) await test("draft (ChatGPT login) is inserted for review, not sent", async (page) => {
  await seed(env, { settings: { autoRun: false, remember: true, fillRemembered: true, helper: true, draftBackend: "chatgpt" } });
  await page.goto("https://job-boards.greenhouse.io/mockco/jobs/1001");
  await runFill(page);
  await page.locator("#jobradar-autofill button", { hasText: "Draft an answer" }).first().click();
  await page.locator("#jobradar-autofill .tag", { hasText: "draft, edit it" }).waitFor({ timeout: 300000 });
  const st = await page.evaluate(() => window.__mockState());
  expect("draft inserted", st.why.length > 40 && st.why.length <= 600, JSON.stringify(st.why));
  console.log("  draft:", st.why);
  expect("draft: nothing submitted", (await page.evaluate(() => window.__submitted)) === 0);
});

await env.close();
const { failures, passes } = results();
console.log(`\n${passes} checks passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
