// Imports Apply kit details from the dashboard (served by server.py on
// 127.0.0.1:8765) into the extension, both ways: the one-click import
// through the dashboard bridge and the copy-and-paste route. Runs in a
// throwaway browser profile, so the real dashboard's saved details are untouched.
import { launch, expect, results } from "./harness.mjs";

const KIT = { firstName: "Kitty", lastName: "Importson", email: "kitty.importson@example.com", github: "https://github.com/kitty-example",
  workAuth: "F-1 OPT (TEST KIT ANSWER)", sponsorship: "Will need H-1B (TEST KIT ANSWER)" };

const env = await launch({ headless: true, mocks: false });
const dash = await env.ctx.newPage();
await dash.goto("http://127.0.0.1:8765/");
await dash.evaluate((kit) => localStorage.setItem("jr:profile", JSON.stringify(kit)), KIT);

const opt = await env.ctx.newPage();
await opt.goto(`chrome-extension://${env.id}/src/options/options.html`);
await opt.evaluate(() => chrome.storage.local.clear());
await opt.reload();
await opt.click("#importDash");
await opt.locator("#importMsg").filter({ hasText: /Imported/ }).waitFor({ timeout: 15000 });
const s = await opt.evaluate(() => chrome.storage.local.get(["profile", "answers"]));
expect("bridge: first name", s.profile.firstName === "Kitty", JSON.stringify(s.profile));
expect("bridge: github", s.profile.github === "https://github.com/kitty-example");
expect("bridge: work auth text", s.answers.workAuthText === KIT.workAuth, JSON.stringify(s.answers));
expect("bridge: sponsorship text", s.answers.sponsorshipText === KIT.sponsorship);
expect("bridge: shown in the form", (await opt.inputValue("#p-firstName")) === "Kitty");

// The paste route, using the dashboard's own Copy for extension output.
await env.ctx.grantPermissions(["clipboard-read", "clipboard-write"], { origin: "http://127.0.0.1:8765" });
await dash.reload();
await dash.click("#openKit");
await dash.click("#copyForExtension");
const copied = await dash.evaluate(() => navigator.clipboard.readText());
expect("dashboard export format", JSON.parse(copied).jobradar === "profile", copied.slice(0, 120));
await opt.evaluate(() => chrome.storage.local.clear());
await opt.reload();
await opt.click("#pasteToggle");
await opt.fill("#pasteText", copied);
await opt.click("#pasteImport");
await opt.locator("#importMsg").filter({ hasText: /Imported/ }).waitFor();
const s2 = await opt.evaluate(() => chrome.storage.local.get(["profile"]));
expect("paste: email", s2.profile.email === KIT.email, JSON.stringify(s2.profile));

await env.close();
const { failures, passes } = results();
console.log(`${passes} checks passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
