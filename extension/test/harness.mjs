// Shared test plumbing: launches Chromium with the unpacked extension, seeds
// its storage with obviously fake data, and serves the mocks on the real ATS
// host names so the manifest's content-script matching is exercised as is.
import { chromium } from "playwright";
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const here = dirname(fileURLToPath(import.meta.url));
export const EXT = resolve(here, "..");
const DIST = join(here, "mocks", "dist");

const FAKE = JSON.parse(readFileSync(join(here, "fixtures", "fake-profile.json"), "utf8"));
export const FAKE_PROFILE = FAKE.profile;
export const FAKE_ANSWERS = FAKE.answers;
export const RESUME_BYTES = readFileSync(join(here, "fixtures", "fake-resume.pdf"));

export const ROUTES = [
  [/^https:\/\/job-boards\.greenhouse\.io\/(mockco\/jobs\/1001|embed\/job_app)/, "greenhouse.html"],
  [/^https:\/\/careers\.mockco\.test\//, "embed.html"],
  [/^https:\/\/jobs\.lever\.co\/mockco\//, "lever.html"],
  [/^https:\/\/jobs\.ashbyhq\.com\/mockco\//, "ashby.html"],
  [/^https:\/\/mockco\.wd5\.myworkdayjobs\.com\//, "workday.html"],
];

let historyJson = null;
export function fixtureHistory() {
  if (!historyJson) {
    const repo = resolve(here, "..", "..");
    historyJson = execFileSync("python", ["-c",
      "import json, resume_tex as rt; print(json.dumps(rt.history(rt.parse(open('tests/fixtures/history_master.tex', encoding='utf-8').read()))))"],
      { cwd: repo, encoding: "utf8" });
  }
  return historyJson;
}

export async function launch({ headless = true, mocks = true, ext = EXT } = {}) {
  const profileDir = mkdtempSync(join(tmpdir(), "jr-ext-"));
  const ctx = await chromium.launchPersistentContext(profileDir, {
    channel: "chromium", headless, viewport: { width: 1280, height: 900 },
    args: [`--disable-extensions-except=${ext}`, `--load-extension=${ext}`],
  });
  if (mocks) {
    await ctx.route(/^https?:\/\//, (route) => {
      const url = route.request().url();
      for (const [re, file] of ROUTES) {
        if (re.test(url)) return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: readFileSync(join(DIST, file)) });
      }
      // Work history comes from a fake resume in the same LaTeX layout, read
      // by the real parser, never from the user's own master resume.
      if (url.startsWith("http://127.0.0.1:8765/api/resume/entries")) {
        return route.fulfill({ status: 200, contentType: "application/json", body: fixtureHistory() });
      }
      if (url.startsWith("http://127.0.0.1:8765/")) return route.continue();   // the local helper
      // The mocks need nothing else; keep tests off the network.
      return route.abort();
    });
  }
  let [sw] = ctx.serviceWorkers();
  if (!sw) sw = await ctx.waitForEvent("serviceworker", { timeout: 15000 });
  const id = new URL(sw.url()).host;
  // The extension opens its options page on install; close it so it can't
  // take over a page a test is about to use.
  const isOptions = (p) => p.url().startsWith(`chrome-extension://${id}/src/options/`);
  const end = Date.now() + 5000;
  while (!ctx.pages().some(isOptions) && Date.now() < end) await new Promise((r) => setTimeout(r, 100));
  for (const p of ctx.pages()) if (isOptions(p)) await p.close();
  const close = async () => { await ctx.close(); rmSync(profileDir, { recursive: true, force: true }); };
  return { ctx, id, sw, close };
}

export async function seed(env, extra = {}) {
  const page = await env.ctx.newPage();
  await page.goto(`chrome-extension://${env.id}/src/options/options.html`);
  await page.evaluate(async ({ profile, answers, settings, resume, extra }) => {
    await chrome.storage.local.clear();
    await chrome.storage.local.set(Object.assign({
      profile, answers, settings,
      resume: { name: "fake-resume.pdf", type: "application/pdf", size: resume.length, updated: 1, data: btoa(String.fromCharCode(...resume)) },
    }, extra));
  }, { profile: FAKE.profile, answers: FAKE.answers, settings: FAKE.settings, resume: [...RESUME_BYTES], extra });
  await page.close();
}

/** Clicks the in-page Autofill launcher (or the panel's fill button) and waits for the result. */
export async function runFill(frame, timeout = 30000) {
  const host = frame.locator("#onward-autofill");
  await host.waitFor({ state: "attached", timeout: 10000 });
  const btn = host.locator("button.launch, header button.primary");
  await btn.first().click();
  await host.locator(".status").filter({ hasText: /^Filled \d+/ }).waitFor({ timeout });
  return panelText(frame);
}

export async function panelText(frame) {
  return frame.locator("#onward-autofill .panel").innerText();
}

let failures = 0, passes = 0;
export function expectState(name, state, want) {
  for (const [k, v] of Object.entries(want)) {
    const got = state[k];
    const ok = typeof v === "function" ? v(got) : JSON.stringify(got ?? null) === JSON.stringify(v);
    if (ok) passes++;
    else { failures++; console.log(`  FAIL ${name}: ${k} = ${JSON.stringify(got)}, want ${typeof v === "function" ? v.toString() : JSON.stringify(v)}`); }
  }
}
export function expect(name, cond, detail = "") {
  if (cond) passes++; else { failures++; console.log(`  FAIL ${name} ${detail}`); }
}
export function results() { return { failures, passes }; }
