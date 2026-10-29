// The apply queue in the extension, against the mocks with a fake helper:
// the prepared (tailored) resume is attached instead of the stored one, the
// answers drafted ahead are filled for review (never into sponsorship or
// acknowledgement questions), the master is the fallback, and the ATS
// "application received" page marks the job applied and offers the next one.
import { launch, seed, runFill, panelText, expectState, expect, results, RESUME_BYTES } from "./harness.mjs";

const headless = !process.argv.includes("--headed");
const env = await launch({ headless });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// A different, recognizable PDF for the tailored resume.
const TAILORED = Buffer.from("%PDF-1.4\n% tailored for Mock Co (TEST)\n1 0 obj <<>> endobj\ntrailer <<>>\n%%EOF\n");
const NEXT = { key: "next|job", company: "Nextco", title: "Software Engineer I", score: 81,
  apply_url: "https://jobs.lever.co/mockco/abc-123/apply" };

// The fake helper. Routes registered after launch() take precedence over its catch-all.
const helper = { prepared: null, applied: [], lookups: [] };
await env.ctx.route(/^http:\/\/127\.0\.0\.1:8765\/api\/(prepared|applied)/, async (route) => {
  const req = route.request();
  const url = new URL(req.url());
  const ok = req.headers()["x-onward-client"] === "autofill";
  if (!ok) return route.fulfill({ status: 403, contentType: "application/json", body: '{"error":"forbidden"}' });
  if (url.pathname === "/api/prepared") {
    helper.lookups.push(url.searchParams.get("url"));
    if (!helper.prepared) return route.fulfill({ status: 404, contentType: "application/json", body: '{"error":"Onward doesn\'t know this job"}' });
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(helper.prepared) });
  }
  helper.applied.push(JSON.parse(req.postData() || "{}"));
  return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true, key: "mock|job", next: NEXT }) });
});

async function test(name, fn) {
  console.log("•", name);
  await seed(env);
  helper.applied = []; helper.lookups = [];
  const page = await env.ctx.newPage();
  page.on("pageerror", (e) => console.log("  page error:", e.message));
  try { await fn(page); }
  catch (e) { expect(name, false, e.stack); }
  finally { await page.close(); }
}

const job = { key: "mock|job", company: "Mock Co", title: "Software Engineer, New Grad" };
const tailored = (answers = []) => ({ job, state: "ready", choice: "tailored", answers,
  resume: { name: "Testy_McTestface_Mock_Co_Resume.pdf", data: TAILORED.toString("base64") } });

await test("greenhouse: attaches the prepared resume and fills answers drafted ahead", async (page) => {
  helper.prepared = tailored([
    { question: "Why do you want to work at Mock Co?", kind: "textarea", options: [], answer: "I build developer tools. (TEST)", choices: [] },
    { question: "When can you start?", kind: "text", options: [], answer: "June 2026", choices: [] },
    { question: "Are you currently subject to any agreement with a former employer (such as a non-compete agreement) that may limit your ability to perform the duties of the position?",
      kind: "choice", options: ["Yes", "No"], answer: "No", choices: [] },
    // Never used, whatever the helper says: acknowledgements and sponsorship are the user's.
    { question: "Do you acknowledge that this is a hybrid role based in Boston and you will be required to come into the office four days a week?",
      kind: "choice", options: ["Yes", "No"], answer: "Yes", choices: [] },
  ]);
  await seed(env, { answers: { sponsorNow: "", sponsorFuture: "", workAuth: "yes" } });
  await page.goto("https://job-boards.greenhouse.io/mockco/jobs/1001");
  const text = await runFill(page);
  expectState("prepared gh", await page.evaluate(() => window.__mockState()), {
    // Attached under the helper's name for it (Firstname_Lastname_Company_Resume.pdf), with the tailored bytes.
    resume: "Testy_McTestface_Mock_Co_Resume.pdf:" + TAILORED.length,
    why: "I build developer tools. (TEST)", question_8: "June 2026", question_7: "No",
    question_6: null, question_5: null, first_name: "Testy",
  });
  expect("prepared gh: panel says tailored", /tailored for this job/.test(text) && /attached the resume tailored/.test(text), text);
  expect("prepared gh: answers tagged for review", (text.match(/AI answer, check it/g) || []).length === 3, text);
  expect("prepared gh: looked up by the form's URL", helper.lookups.some((u) => u.startsWith("https://job-boards.greenhouse.io/mockco/jobs/1001")));
  expect("prepared gh: nothing submitted", (await page.evaluate(() => window.__submitted)) === 0);
});

await test("a drafted answer is redrafted with a note, never over the user's own edits", async (page) => {
  const drafts = [];
  await env.ctx.route(/^http:\/\/127\.0\.0\.1:8765\/api\/draft/, async (route) => {
    const req = route.request();
    if (req.method() === "POST") {
      drafts.push(JSON.parse(req.postData() || "{}"));
      return route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ id: "abc123" + drafts.length }) });
    }
    return route.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify({ state: "done", result: { answer: `Redraft ${drafts.length}: my Onward project. (TEST)` } }) });
  });
  helper.prepared = tailored([
    { question: "Why do you want to work at Mock Co?", kind: "textarea", options: [], answer: "I build developer tools. (TEST)", choices: [] },
  ]);
  await page.goto("https://job-boards.greenhouse.io/mockco/jobs/1001");
  await runFill(page);
  const host = page.locator("#onward-autofill");
  const li = host.locator("li", { hasText: "Why do you want to work at Mock Co?" });
  const redraft = async (note) => {
    await li.locator("button", { hasText: "Redraft with a note" }).click();
    await li.locator(".redo input").fill(note);
    await li.locator(".redo input").press("Enter");
  };
  await redraft("Use my Onward project instead");
  await page.waitForFunction(() => window.__mockState().why.startsWith("Redraft 1"), null, { timeout: 15000 });
  expect("redraft: the helper got the note and the answer it changes",
    drafts[0].note === "Use my Onward project instead" && drafts[0].previous === "I build developer tools. (TEST)" &&
    drafts[0].question === "Why do you want to work at Mock Co?", JSON.stringify(drafts[0]));
  expect("redraft: still marked for review", /draft, edit it/.test(await li.innerText()), await li.innerText());

  // The user edits the answer while a redraft runs: the new one is offered, not typed over theirs.
  await env.ctx.unroute(/^http:\/\/127\.0\.0\.1:8765\/api\/draft/);
  let release;
  const held = new Promise((r) => { release = r; });
  await env.ctx.route(/^http:\/\/127\.0\.0\.1:8765\/api\/draft/, async (route) => {
    if (route.request().method() === "POST") {
      drafts.push(JSON.parse(route.request().postData() || "{}"));
      return route.fulfill({ status: 202, contentType: "application/json", body: '{"id":"def456"}' });
    }
    await held;
    return route.fulfill({ status: 200, contentType: "application/json", body: '{"state":"done","result":{"answer":"Redraft 2. (TEST)"}}' });
  });
  await redraft("Shorter");
  await page.locator("#question_9").fill("My own words.");
  release();
  await li.locator("textarea").waitFor({ timeout: 15000 });
  expect("redraft over an edit: the user's words stay", (await page.evaluate(() => window.__mockState().why)) === "My own words.");
  expect("redraft over an edit: the new draft is offered to copy", (await li.locator("textarea").inputValue()) === "Redraft 2. (TEST)");
  expect("redraft: nothing submitted", (await page.evaluate(() => window.__submitted)) === 0);
  await env.ctx.unroute(/^http:\/\/127\.0\.0\.1:8765\/api\/draft/);
});

await test("ashby: attaches the prepared resume, fills drafted radios and essays", async (page) => {
  helper.prepared = tailored([
    { question: "Which team interests you most?", kind: "choice", options: ["Platform", "Product", "Infrastructure"], answer: "Platform", choices: [] },
    { question: "What's the most interesting paper, blog post, or documentation you've read in the past month?", kind: "textarea", options: [],
      answer: "The Raft paper. (TEST)", choices: [] },
  ]);
  await page.goto("https://jobs.ashbyhq.com/mockco/abc-123/application");
  const text = await runFill(page);
  expectState("prepared ashby", await page.evaluate(() => window.__mockState()), {
    _systemfield_resume: "Testy_McTestface_Mock_Co_Resume.pdf:" + TAILORED.length, team: "Platform", cd46f09e: "The Raft paper. (TEST)",
    "086af02e": "yes", _systemfield_name: "Testy McTestface",
  });
  expect("prepared ashby: nothing submitted", (await page.evaluate(() => window.__submitted)) === 0, text);
});

await test("master chosen, or the helper doesn't know the job: attaches the stored resume", async (page) => {
  helper.prepared = { job, state: "ready", choice: "master", answers: [], resume: null };
  await page.goto("https://job-boards.greenhouse.io/mockco/jobs/1001");
  let text = await runFill(page);
  expectState("master", await page.evaluate(() => window.__mockState()), { resume: "fake-resume.pdf:" + RESUME_BYTES.length });
  expect("master: panel says master", /attached your master resume/.test(text), text);
  helper.prepared = null;
  await page.reload();
  text = await runFill(page);
  expectState("unknown job", await page.evaluate(() => window.__mockState()), { resume: "fake-resume.pdf:" + RESUME_BYTES.length, first_name: "Testy" });
});

await test("greenhouse confirmation page marks the job applied and offers the next one", async (page) => {
  helper.prepared = null;
  await env.ctx.route("https://job-boards.greenhouse.io/mockco/jobs/1001/confirmation", (route) => route.fulfill({
    status: 200, contentType: "text/html", body: "<!doctype html><title>Thank you</title><h1>Thank you for applying.</h1><p>Your application has been received.</p>" }));
  await page.goto("https://job-boards.greenhouse.io/mockco/jobs/1001/confirmation");
  const panel = page.locator("#onward-autofill .panel");
  await panel.waitFor({ timeout: 10000 });
  const text = await panel.innerText();
  expect("confirm gh: recorded", helper.applied.length === 1 && /\/confirmation$/.test(helper.applied[0].url), JSON.stringify(helper.applied));
  expect("confirm gh: next job offered", /Nextco/.test(text) && /Open next application/.test(text), text);
  const href = await panel.locator("a", { hasText: "Open next application" }).getAttribute("href");
  expect("confirm gh: next link opens its form", href === NEXT.apply_url, href);
  // The next one opens by itself after a countdown, unless the user stays.
  expect("confirm gh: counting down to the next", /Opening the next one in \d+s/.test(text), text);
  await page.waitForURL(NEXT.apply_url, { timeout: 15000 }).catch(() => {});
  expect("confirm gh: the next form opened", page.url() === NEXT.apply_url, page.url());

  await page.goto("https://job-boards.greenhouse.io/mockco/jobs/1001/confirmation");
  await panel.locator("button", { hasText: "Stay here" }).click();
  await sleep(9500);
  expect("confirm gh: Stay here stays", page.url().endsWith("/confirmation"), page.url());
  expect("confirm gh: countdown gone", !/Opening the next one/.test(await panel.innerText()));
  await env.ctx.unroute("https://job-boards.greenhouse.io/mockco/jobs/1001/confirmation");
});

await test("ashby thank-you message counts only in a tab that showed a radar job's form", async (page) => {
  // A tab that never showed a known job's form: a thank-you heading is ignored.
  helper.prepared = null;
  await page.goto("https://jobs.ashbyhq.com/mockco/abc-123/application");
  await page.locator("#onward-autofill").waitFor({ state: "attached" });
  await page.evaluate(() => { document.querySelector(".ashby-application-form-container").innerHTML = "<h2>Thank you for applying!</h2>"; });
  await sleep(2500);
  expect("confirm ashby: unknown job not recorded", helper.applied.length === 0, JSON.stringify(helper.applied));

  helper.prepared = tailored();
  await page.reload();
  // A job the queue prepared fills itself as soon as its form opens.
  await page.locator("#onward-autofill .status").filter({ hasText: /^Filled \d+/ }).waitFor({ timeout: 30000 });
  expect("confirm ashby: the queued job filled itself", (await page.evaluate(() => window.__mockState()))._systemfield_name === "Testy McTestface");
  // What Ashby shows after Submit (the user's own click; this mock just swaps it in).
  await page.evaluate(() => {
    document.querySelector(".ashby-application-form-container").outerHTML =
      '<div class="ashby-application-form-success-container"><h2>Thank you for applying!</h2><p>We will be in touch.</p></div>';
  });
  await page.locator("#onward-autofill .panel .status").filter({ hasText: /Marked as applied/ }).waitFor({ timeout: 10000 });
  const text = await panelText(page);
  expect("confirm ashby: recorded with the tab's job", helper.applied.length === 1 && helper.applied[0].key === "mock|job", JSON.stringify(helper.applied));
  expect("confirm ashby: names the job", /Software Engineer, New Grad at Mock Co/.test(text), text);
});

await test("an application form itself is never taken for a confirmation", async (page) => {
  helper.prepared = tailored();
  for (const url of ["https://job-boards.greenhouse.io/mockco/jobs/1001", "https://jobs.lever.co/mockco/abc-123/apply",
    "https://jobs.ashbyhq.com/mockco/abc-123/application", "https://mockco.wd5.myworkdayjobs.com/en-US/External/job/Boston-MA/Software-Engineer_JR1001/apply/applyManually"]) {
    await page.goto(url);
    await page.locator("#onward-autofill").waitFor({ state: "attached" });
    await sleep(1500);
  }
  expect("no false confirmations", helper.applied.length === 0, JSON.stringify(helper.applied));
});

await env.close();
const { failures, passes } = results();
console.log(`\n${passes} checks passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
