// Field detection on Workday pages as live tenants serve them: the four steps
// saved (cleaned of personal data) in fixtures/workday-real/. These are static
// copies, so this checks what the extension finds and how it reads each
// question, not the widgets' behavior (the mock in mocks/src/workday.jsx,
// built to the same markup, covers that).
import { chromium } from "playwright";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(here, "..", "src");
const SCRIPTS = ["shared/rules.js", "content/dom.js", "content/sites.js", "content/widgets.js", "content/history.js"]
  .map((f) => readFileSync(join(SRC, f), "utf8"));
const page = (name) => readFileSync(join(here, "fixtures", "workday-real", name + ".html"), "utf8");

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
let failed = 0, passed = 0;

/** Loads a saved step on a Workday host and returns what the extension makes of each question. */
async function read(name) {
  const p = await ctx.newPage();
  await p.route(/^https:\/\/realco\.wd1\.myworkdayjobs\.com\//, (r) => r.fulfill({
    status: 200, contentType: "text/html; charset=utf-8",
    body: `<!doctype html><html><head><meta charset="utf-8"><title>Careers</title></head><body>${page(name)}</body></html>`,
  }));
  await p.goto("https://realco.wd1.myworkdayjobs.com/en-US/External/job/Boston-MA/Engineer_R1/apply/applyManually");
  for (const s of SCRIPTS) await p.addScriptTag({ content: s });
  const out = await p.evaluate(() => {
    const site = JR.sites.detect(document);
    const qs = JR.sites.collect(document, site);
    const R = JR.rules;
    const rows = JR.history.rows(qs);
    const adds = Object.fromEntries(["work", "edu", "lang", "web", "cert"].map((t) => {
      const b = JR.history.addButton(t, document);
      const sec = b && b.closest("[role='group'][aria-labelledby$='-section']");
      return [t, b ? b.textContent.trim() + " in " + (sec ? sec.getAttribute("aria-labelledby") : "?") : null];
    }));
    return {
      site: site && site.name,
      questions: qs.map((q) => {
        const c = R.classify(q.label, q.kind, q.attrs, q.section, q.options);
        return { id: q.el.id, kind: q.kind, label: q.label, cat: c.cat, key: c.key || null, transform: c.transform || null, history: q.history ? q.history.field : null };
      }),
      rows: rows.map((r) => r.type + ":" + Object.keys(r.fields).sort().join(",")),
      adds,
    };
  });
  await p.close();
  return out;
}

function check(name, fn) {
  try { fn(); passed++; } catch (e) { failed++; console.log("FAIL", name, "\n  ", e.message.split("\n").join("\n   ")); }
}
const q = (r, id) => r.questions.find((x) => x.id === id) || assert.fail(`no question ${id}; found ${r.questions.map((x) => x.id).join(", ")}`);
const reads = (r, id, want) => check(`${id}`, () => {
  const got = q(r, id);
  for (const [k, v] of Object.entries(want)) assert.equal(got[k], v, `${id} (${got.label}): ${k} = ${got[k]}, want ${v}`);
});

console.log("• My Information");
const info = await read("myinfo");
check("detected as Workday", () => assert.equal(info.site, "workday"));
reads(info, "source--source", { kind: "prompt", cat: "std", key: "hearAbout" });
reads(info, "country--country", { kind: "listbutton", cat: "profile", key: "country" });
reads(info, "name--legalName--firstName", { kind: "text", cat: "profile", key: "firstName" });
reads(info, "name--legalName--lastName", { kind: "text", cat: "profile", key: "lastName" });
reads(info, "address--addressLine1", { kind: "text", cat: "profile", key: "address", transform: null });
reads(info, "address--city", { kind: "text", cat: "profile", key: "location", transform: "city" });
reads(info, "address--countryRegion", { kind: "listbutton", cat: "profile", key: "location", transform: "state" });
reads(info, "address--postalCode", { kind: "text", cat: "profile", key: "postalCode" });
reads(info, "phoneNumber--phoneType", { kind: "listbutton", cat: "profile", key: "phoneType" });
reads(info, "phoneNumber--countryPhoneCode", { kind: "prompt", cat: "profile", key: "country" });
reads(info, "phoneNumber--phoneNumber", { kind: "text", cat: "profile", key: "phone" });
reads(info, "phoneNumber--extension", { kind: "text", cat: "never" });
reads(info, "name--preferredCheck", { kind: "checkbox" });
check("SMS opt-in is left alone", () => {
  const sms = info.questions.find((x) => /sms/i.test(x.id) || /sms/i.test(x.label));
  assert.ok(!sms || sms.cat === "never" || sms.cat === "custom", JSON.stringify(sms));
});

console.log("• My Experience");
const exp = await read("experience");
reads(exp, "workExperience-721--jobTitle", { kind: "text", history: "title" });
reads(exp, "workExperience-721--companyName", { kind: "text", history: "company" });
reads(exp, "workExperience-721--location", { kind: "text", history: "location" });
reads(exp, "workExperience-721--currentlyWorkHere", { kind: "checkbox", history: "current" });
reads(exp, "workExperience-721--startDate-dateSectionMonth-input", { history: "startMonth" });
reads(exp, "workExperience-721--startDate-dateSectionYear-input", { history: "startYear" });
reads(exp, "workExperience-721--endDate-dateSectionMonth-input", { history: "endMonth" });
reads(exp, "workExperience-721--endDate-dateSectionYear-input", { history: "endYear" });
reads(exp, "workExperience-721--roleDescription", { kind: "textarea", history: "description" });
reads(exp, "education-776--schoolName", { kind: "text", history: "school" });
reads(exp, "education-776--degree", { kind: "listbutton", history: "degree" });
reads(exp, "education-776--fieldOfStudy", { kind: "prompt", history: "field" });
reads(exp, "education-776--gradeAverage", { kind: "text", history: "gpa" });
reads(exp, "education-776--firstYearAttended-dateSectionYear-input", { history: "startYear" });
reads(exp, "education-776--lastYearAttended-dateSectionYear-input", { history: "endYear" });
reads(exp, "skills--skills", { kind: "prompt" });
check("one work row and one education row", () => assert.deepEqual(exp.rows.map((r) => r.split(":")[0]).sort(), ["edu", "work"]));
// Add buttons are submit buttons on live Workday; each section's own is found, never the footer's.
check("Add Another under Work Experience", () => assert.equal(exp.adds.work, "Add Another in Work-Experience-section"));
check("Add Another under Education", () => assert.equal(exp.adds.edu, "Add Another in Education-section"));
check("Add under Languages", () => assert.equal(exp.adds.lang, "Add in Languages-section"));
check("Add under Certifications", () => assert.equal(exp.adds.cert, "Add in Certifications-section"));
check("the LinkedIn section takes websites", () => assert.equal(exp.adds.web, "Add in LinkedIn-section"));

console.log("• Application Questions");
const aq = await read("questions");
if (process.argv.includes("--show")) console.log(aq.questions);
const byLabel = (r, re) => r.questions.find((x) => re.test(x.label)) || assert.fail(`no question like ${re}`);
check("labels come from each question's legend", () => assert.equal(aq.questions.filter((x) => x.kind === "listbutton" && x.label).length, 6));
check("citizen", () => assert.equal(byLabel(aq, /U\.S\. Citizen/).key, "citizen"));
check("relocate", () => assert.equal(byLabel(aq, /relocate/).key, "relocate"));
check("clearance is the user's", () => assert.equal(byLabel(aq, /Security Clearance/).cat, "never"));
check("former employee is a custom question", () => assert.equal(byLabel(aq, /Amentum employee/).cat, "custom"));

console.log("• Voluntary Disclosures");
const vd = await read("disclosures");
reads(vd, "personalInfoUS--veteranStatus", { kind: "listbutton", cat: "eeo", key: "eeoVeteran" });
reads(vd, "personalInfoUS--gender", { kind: "listbutton", cat: "eeo", key: "eeoGender" });
reads(vd, "personalInfoUS--ethnicity", { kind: "listbutton", cat: "eeo", key: "eeoRace" });
reads(vd, "termsAndConditions--acceptTermsAndAgreements", { kind: "checkbox", cat: "never" });

await browser.close();
console.log(`\n${passed} checks passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
