// Read-only check on live application forms: fills obviously fake details,
// reports what was filled and what was left, and closes the page. Nothing is
// submitted: the extension never presses Submit, and as a second guard this
// harness aborts any request that looks like a submission. No resume is
// seeded, so no file is sent to an ATS's resume parser.
import { launch, seed, runFill, expect, results, fixtureHistory } from "./harness.mjs";

const PAGES = [
  ["palantir", "https://jobs.lever.co/palantir/e1a6c138-98bf-45e2-97f7-2c70371cc38a/apply", { "input[name=name]": "Testy McTestface" }],
  ["greenhouse", "https://job-boards.greenhouse.io/chime/jobs/8875663002", { "#first_name": "Testy", "#last_name": "McTestface", "#email": "testy.mctestface@example.com", "#phone": "555-0100" }],
  ["greenhouse embed", "https://job-boards.greenhouse.io/embed/job_app?for=robinhood&token=8008723", { "#first_name": "Testy", "#email": "testy.mctestface@example.com" }],
  ["lever", "https://jobs.lever.co/veeva/8e4b8273-783e-420a-8f93-af521a341d1e/apply", { "input[name=name]": "Testy McTestface", "input[name=email]": "testy.mctestface@example.com", "input[name='urls[LinkedIn]']": "https://www.linkedin.com/in/testy-mctestface-example" }],
  ["ashby", "https://jobs.ashbyhq.com/netic/e645059a-a2e8-4cfd-84e8-085d92da6e62/application", { "#_systemfield_name": "Testy McTestface", "#_systemfield_email": "testy.mctestface@example.com" }],
];
const only = process.argv.find((a) => a.startsWith("--only="))?.slice(7);
const shots = process.argv.find((a) => a.startsWith("--shots="))?.slice(8);

const env = await launch({ headless: !process.argv.includes("--headed"), mocks: false });
const blocked = [];
// Work history on live forms comes from the fake fixture resume, never the user's own.
await env.ctx.route(/^http:\/\/127\.0\.0\.1:8765\/api\/resume\/entries/, (route) =>
  route.fulfill({ status: 200, contentType: "application/json", body: fixtureHistory() }));
await env.ctx.route(/^https:\/\//, (route) => {
  const r = route.request();
  // Greenhouse posts to .../applications, Lever to .../apply, Ashby runs a
  // GraphQL operation with Submit in its name.
  const path = new URL(r.url()).pathname;
  if (r.method() !== "GET" && (/submit/i.test(r.url()) || /\/(applications?|apply)$/.test(path))) {
    blocked.push(r.method() + " " + r.url());
    return route.abort();
  }
  return route.continue();
});

for (const [name, url, want] of PAGES) {
  if (only && !name.includes(only)) continue;
  // --ai: also answer the leftover questions through the helper (your ChatGPT login).
  const ai = process.argv.includes("--ai");
  await seed(env, ai ? { resume: null, settings: { autoRun: false, remember: true, fillRemembered: true, helper: true, aiFill: true, draftBackend: "chatgpt" } } : { resume: null });
  const page = await env.ctx.newPage();
  console.log("•", name, url);
  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
    const text = await runFill(page, ai ? 600000 : 30000);
    for (const [sel, v] of Object.entries(want)) {
      const got = await page.locator(sel).first().inputValue().catch(() => "(missing)");
      expect(`${name} ${sel}`, got === v, `got ${JSON.stringify(got)}`);
    }
    console.log(text.split("\n").map((l) => "    " + l).join("\n"));
    if (shots) await page.locator("#jobradar-autofill .panel").screenshot({ path: `${shots}/${name.replace(/\W+/g, "-")}.png` });
  } catch (e) {
    expect(name, false, e.message);
  } finally {
    await page.close();
  }
}
expect("no submission requests", blocked.length === 0, blocked.join("\n"));
await env.close();
const { failures, passes } = results();
console.log(`\n${passes} checks passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
