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
  "school--0": "Northeastern University", "degree--0": "Bachelor's Degree",
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
  expectState("workday step 2", await page.evaluate(() => window.__mockState()), {
    resume: RESUME, "socialNetworkAccounts--linkedInAccount": "https://www.linkedin.com/in/testy-mctestface-example",
    "webAddress-1--url": "https://testy.example.com",
  });
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
