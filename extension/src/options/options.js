"use strict";

const api = globalThis.browser || globalThis.chrome;
const R = globalThis.JR.rules;
const $ = (id) => document.getElementById(id);
const MAX_RESUME = 8 * 1024 * 1024;
// Firefox match patterns can't name a port; the port is checked separately.
const DASHBOARD = ["http://127.0.0.1/*", "http://localhost/*"];
const DASHBOARD_PORT = "8765";

let state = { profile: {}, answers: {}, settings: {} };

function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "text") n.textContent = v;
    else if (k === "value") n.value = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) n.append(k);
  return n;
}

let savedTimer = 0;
function saved(msg = "Saved") {
  $("saved").textContent = msg;
  clearTimeout(savedTimer);
  savedTimer = setTimeout(() => { $("saved").textContent = ""; }, 1800);
}

const pending = {};
let saveTimer = 0;
function save(area) {
  pending[area] = state[area];
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const out = Object.assign({}, pending);
    for (const k of Object.keys(pending)) delete pending[k];
    await api.storage.local.set(out);
    saved();
  }, 300);
}

async function load() {
  const s = await api.storage.local.get(["profile", "answers", "settings", "resume", "history"]);
  state.profile = Object.assign({}, R.DEFAULT_PROFILE, s.profile);
  state.answers = Object.assign({}, R.DEFAULT_ANSWERS, s.answers);
  const loaded = R.withDefaults(s.settings);
  state.settings = loaded.settings;
  state.notice = loaded.notice;
  renderProfile();
  renderStd();
  renderEeo();
  renderSettings();
  renderResume(s.resume);
  state.history = normalizeHistory(s.history);
  renderHistory();
  renderMemory();
}

/* -------------------------------------------------------------- profile */

const INPUT_TYPES = { email: "email", phone: "tel", linkedin: "url", github: "url", portfolio: "url" };

function renderProfile() {
  const box = $("profileFields");
  box.replaceChildren();
  for (const [key, label] of R.PROFILE_FIELDS) {
    const id = "p-" + key;
    const input = el("input", { id, type: INPUT_TYPES[key] || "text", value: state.profile[key] || "", autocomplete: "off" });
    input.addEventListener("input", () => { state.profile[key] = input.value.trim(); save("profile"); });
    box.append(el("label", { for: id, text: label }), input, el("span"));
  }
}

/* -------------------------------------------------------- standard answers */

function renderStd() {
  const box = $("stdFields");
  box.replaceChildren();
  for (const [key, question, allowed] of R.STANDARD_ANSWERS) {
    const id = "a-" + key;
    let input;
    if (allowed) {
      input = el("select", { id },
        el("option", { value: "", text: "Ask me each time" }),
        el("option", { value: "yes", text: "Yes" }),
        el("option", { value: "no", text: "No" }));
      input.value = state.answers[key] || "";
      input.addEventListener("change", () => { state.answers[key] = input.value; save("answers"); });
    } else {
      input = el("input", { id, type: "text", value: state.answers[key] || "", autocomplete: "off",
        placeholder: key === "hearAbout" ? "LinkedIn" : "Leave empty to answer yourself" });
      input.addEventListener("input", () => { state.answers[key] = input.value.trim(); save("answers"); });
    }
    box.append(el("label", { for: id, text: question }), input, el("span"));
  }
}

function renderEeo() {
  const box = $("eeoFields");
  box.replaceChildren();
  for (const [key, label, choices] of R.EEO_ANSWERS) {
    const id = "e-" + key;
    const cur = state.answers[key];
    const known = cur === R.DECLINE || cur === "" || choices.includes(cur);
    const select = el("select", { id },
      el("option", { value: R.DECLINE, text: "Decline to answer" }),
      choices.map((c) => el("option", { value: c, text: c })),
      el("option", { value: "__custom", text: "Other (type it)" }),
      el("option", { value: "", text: "Leave it for me" }));
    select.value = known ? cur : "__custom";
    const custom = el("input", { type: "text", value: known ? "" : cur, "aria-label": label + " (your wording)", placeholder: "Your answer as the form words it" });
    custom.hidden = known;
    select.addEventListener("change", () => {
      custom.hidden = select.value !== "__custom";
      state.answers[key] = select.value === "__custom" ? custom.value.trim() : select.value;
      save("answers");
      if (!custom.hidden) custom.focus();
    });
    custom.addEventListener("input", () => { state.answers[key] = custom.value.trim(); save("answers"); });
    // The wrapper keeps the grid cell even while the text box is hidden.
    box.append(el("label", { for: id, text: label }), select, el("span", { class: "cell" }, custom));
  }
}

/* ------------------------------------------------------------- settings */

function renderSettings() {
  for (const k of ["autoRun", "remember", "fillRemembered", "helper", "aiFill"]) {
    const box = $("set-" + k);
    box.checked = !!state.settings[k];
    box.onchange = () => { state.settings[k] = box.checked; save("settings"); };
  }
  const b = $("draftBackend");
  b.value = state.settings.draftBackend;
  b.onchange = () => { state.settings.draftBackend = b.value; save("settings"); };
  if (state.notice) {
    // Shown once: saving the corrected backend means it won't come back.
    $("backendNotice").textContent = state.notice;
    $("backendNotice").hidden = false;
    state.notice = "";
    save("settings");
  }
}

$("helperCheck").addEventListener("click", async () => {
  const out = $("helperStatus");
  out.textContent = "Checking…";
  const r = await api.runtime.sendMessage({ type: "jr:helperStatus" });
  if (!r || r.error) { out.textContent = (r && r.error) || "No answer from the helper."; return; }
  const b = r.backends || {};
  const c = r.costs || {};
  const cost = (id) => (c[id] ? ", about " + (c[id].draft < 0.01 ? "under 1¢" : "$" + c[id].draft.toFixed(2)) + " a draft" : "");
  const parts = [
    "ChatGPT " + (b.chatgpt ? "signed in" : "not signed in"),
    "Claude " + (b.claude_code ? "signed in" : "not signed in"),
    "OpenAI key " + (b.openai_api ? "saved" + cost("openai-api") : "not added"),
    "Anthropic key " + (b.claude_api ? "saved" + cost("claude-api") : "not added"),
  ];
  out.textContent = "Running. " + parts.join(", ") + "." + (r.master ? "" : " Upload your master .tex resume in the dashboard to enable drafts.");
});

/* --------------------------------------------------------------- resume */

function renderResume(r) {
  $("resumeInfo").textContent = r ? `${r.name} (${Math.round(r.size / 1024)} KB), saved ${new Date(r.updated).toLocaleDateString()}` : "No resume chosen.";
  $("resumeRemove").hidden = !r;
}

function toB64(buf) {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

$("resumeFile").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  if (f.size > MAX_RESUME) { saved("That file is over 8 MB."); return; }
  const resume = { name: f.name, type: f.type || "application/pdf", size: f.size, updated: Date.now(), data: toB64(await f.arrayBuffer()) };
  await api.storage.local.set({ resume });
  renderResume(resume);
  saved("Resume saved");
});

$("resumeRemove").addEventListener("click", async () => {
  await api.storage.local.remove("resume");
  renderResume(null);
  saved("Resume removed");
});

/* --------------------------------------- experience, education, projects */

// Saved under "history" in the same shape the helper's /api/resume/entries
// returns, plus an employment type and an editable description per entry.
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const JOB_TYPES = ["", "Full-time", "Part-time", "Internship", "Contract", "Co-op", "Freelance"];
const KINDS = {
  work: { box: "histWork", label: "Work experience", current: "I currently work here",
    fields: [["title", "Job title"], ["company", "Company"], ["location", "Location"], ["type", "Type", JOB_TYPES]] },
  education: { box: "histEdu", label: "Education", current: "I currently study here",
    fields: [["school", "School"], ["degree", "Degree"], ["field", "Field of study"], ["gpa", "GPA"], ["location", "Location"]] },
  projects: { box: "histProj", label: "Project", current: "Ongoing",
    fields: [["name", "Project name"], ["role", "Your role"], ["tech", "Tools used"], ["link", "Link"], ["location", "Location"]] },
};

const lines = (e) => (e.description != null ? e.description : (e.bullets || []).join("\n"));

function normalizeHistory(h) {
  h = h && typeof h === "object" ? h : {};
  const list = (x) => (Array.isArray(x) ? x.filter((e) => e && typeof e === "object") : []);
  return { work: list(h.work), education: list(h.education), projects: list(h.projects),
    skills: Array.isArray(h.skills) ? h.skills : [], certifications: list(h.certifications), imported: h.imported || 0 };
}

function saveHistory() {
  state.history.updated = Date.now();
  save("history");
}

function fmtDate(d) {
  return d && d.year ? (d.month ? MONTHS[d.month - 1] + " " : "") + d.year : "";
}

function dateInputs(entry, key, label) {
  const d = entry[key] || {};
  const month = el("select", { "aria-label": label + " month" },
    el("option", { value: "", text: "Month" }), MONTHS.map((m, i) => el("option", { value: String(i + 1), text: m })));
  month.value = d.month ? String(d.month) : "";
  const year = el("input", { type: "text", inputmode: "numeric", maxlength: "4", placeholder: "Year", "aria-label": label + " year",
    value: d.year ? String(d.year) : "" });
  const put = () => {
    const y = parseInt(year.value, 10);
    entry[key] = y >= 1950 && y <= 2100 ? { month: month.value ? +month.value : null, year: y } : null;
    saveHistory();
  };
  month.addEventListener("change", put);
  year.addEventListener("input", put);
  return el("span", { class: "date" }, month, year);
}

function entryCard(kind, entry, i) {
  const K = KINDS[kind];
  const grid = el("div", { class: "grid" });
  for (const [key, label, choices] of K.fields) {
    const id = `h-${kind}-${i}-${key}`;
    let input;
    if (choices) {
      input = el("select", { id }, choices.map((c) => el("option", { value: c, text: c || "Not set" })));
      input.value = entry[key] || "";
      input.addEventListener("change", () => { entry[key] = input.value; saveHistory(); });
    } else {
      input = el("input", { id, type: "text", value: entry[key] || "", autocomplete: "off" });
      input.addEventListener("input", () => { entry[key] = input.value.trim(); saveHistory(); });
    }
    grid.append(el("label", { for: id, text: label }), input, el("span"));
  }
  const endWrap = el("span", null, dateInputs(entry, "end", "End"));
  const cur = el("input", { type: "checkbox" });
  cur.checked = !!entry.current;
  endWrap.hidden = cur.checked;
  cur.addEventListener("change", () => {
    entry.current = cur.checked;
    if (cur.checked) entry.end = null;
    endWrap.hidden = cur.checked;
    saveHistory();
  });
  grid.append(el("span", { class: "lbl" }, "Start"), dateInputs(entry, "start", "Start"), el("span"),
    el("span", { class: "lbl" }, "End"), el("span", { class: "dates" }, endWrap, el("label", { class: "check inline" }, cur, " " + K.current)), el("span"));
  const descId = `h-${kind}-${i}-desc`;
  const desc = el("textarea", { id: descId, rows: Math.min(10, Math.max(3, lines(entry).split("\n").length + 1)) }, lines(entry));
  desc.addEventListener("input", () => { entry.description = desc.value; saveHistory(); });
  const title = (kind === "work" ? [entry.title, entry.company] : kind === "education" ? [entry.degree, entry.school] : [entry.name])
    .filter(Boolean).join(" · ");
  const when = [fmtDate(entry.start), entry.current ? "now" : fmtDate(entry.end)].filter(Boolean).join(" – ");
  return el("details", { class: "entry", open: !title },
    el("summary", null, el("b", null, title || "New " + K.label.toLowerCase()), when ? el("span", { class: "muted" }, "  " + when) : null),
    grid,
    el("label", { for: descId, class: "muted" }, kind === "education" ? "Notes (courses, honors)" : "Description, one point per line"),
    desc,
    el("div", { class: "row" }, el("button", { type: "button", class: "ghost danger", onclick: () => {
      state.history[kind].splice(i, 1);
      saveHistory();
      renderHistory();
      saved("Removed");
    } }, "Remove")));
}

function renderHistory() {
  for (const kind of Object.keys(KINDS)) {
    const box = $(KINDS[kind].box);
    box.replaceChildren(...state.history[kind].map((e, i) => entryCard(kind, e, i)));
    if (!state.history[kind].length) box.append(el("p", { class: "muted" }, "None yet."));
  }
  $("histSkills").value = state.history.skills.join(", ");
  $("histMsg").textContent = state.history.imported ? "Imported from your resume " + new Date(state.history.imported).toLocaleDateString() + "." : "";
}

for (const [kind, btn] of [["work", "addWork"], ["education", "addEdu"], ["projects", "addProj"]]) {
  $(btn).addEventListener("click", () => {
    state.history[kind].push({ start: null, end: null, current: false, description: "" });
    saveHistory();
    renderHistory();
  });
}

$("histSkills").addEventListener("input", () => {
  state.history.skills = $("histSkills").value.split(",").map((x) => x.trim()).filter(Boolean);
  saveHistory();
});

$("histImport").addEventListener("click", async () => {
  const h = state.history;
  if ((h.work.length || h.education.length || h.projects.length) &&
      !confirm("Replace these entries with what your master resume says? Edits made here will be lost.")) return;
  $("histMsg").textContent = "Reading your resume…";
  const r = await api.runtime.sendMessage({ type: "jr:history", url: "" }).catch((e) => ({ error: e.message }));
  if (!r || r.error) {
    $("histMsg").textContent = (r && r.error) || "The Onward helper didn't answer. Is the Onward app running?";
    return;
  }
  const withText = (list) => (list || []).map((e) => Object.assign({}, e, { description: (e.bullets || []).join("\n") }));
  state.history = normalizeHistory({ work: withText(r.work), education: r.education, projects: withText(r.projects),
    skills: r.skills, certifications: r.certifications, imported: Date.now() });
  saveHistory();
  renderHistory();
  saved("Imported");
});

/* --------------------------------------------------------------- import */

const KIT_TO_ANSWERS = { workAuth: "workAuthText", sponsorship: "sponsorshipText" };

function importKit(kit) {
  if (!kit || typeof kit !== "object") return "Nothing to import.";
  let added = 0, kept = 0;
  const profileKeys = new Set(R.PROFILE_FIELDS.map(([k]) => k));
  for (const [k, raw] of Object.entries(kit)) {
    const v = typeof raw === "string" ? raw.trim() : "";
    if (!v) continue;
    const area = profileKeys.has(k) ? "profile" : KIT_TO_ANSWERS[k] ? "answers" : null;
    if (!area) continue;
    const key = area === "answers" ? KIT_TO_ANSWERS[k] : k;
    const cur = state[area][key];
    if (cur && !(key === "country" && cur === R.DEFAULT_PROFILE.country)) { if (cur !== v) kept++; continue; }
    state[area][key] = v;
    added++;
  }
  api.storage.local.set({ profile: state.profile, answers: state.answers });
  renderProfile();
  renderStd();
  return `Imported ${added} field${added === 1 ? "" : "s"}.` + (kept ? ` Kept ${kept} you had already set here.` : "");
}

async function profileFromDashboard() {
  // The helper keeps the dashboard's Apply kit; an older helper doesn't, so
  // then it's read from an open dashboard tab.
  try {
    const r = await api.runtime.sendMessage({ type: "jr:dashboardProfile" });
    if (r && r.profile && Object.keys(r.profile).length) return r.profile;
  } catch (e) { /* fall back to the tab */ }
  let tabs = (await api.tabs.query({ url: DASHBOARD })).filter((t) => {
    try { return new URL(t.url).port === DASHBOARD_PORT; } catch (e) { return false; }
  });
  let opened = null;
  if (!tabs.length) {
    opened = await api.tabs.create({ url: "http://127.0.0.1:8765/", active: false });
    tabs = [opened];
  }
  for (let i = 0; i < 20; i++) {
    for (const t of tabs) {
      try {
        const r = await api.tabs.sendMessage(t.id, { type: "jr:exportProfile" });
        if (r) { if (opened) api.tabs.remove(opened.id); return r.profile; }
      } catch (e) { /* not loaded yet */ }
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  if (opened) api.tabs.remove(opened.id);
  throw new Error("Couldn't reach the dashboard. Is the helper running (pythonw server.py)? You can also paste an export.");
}

$("importDash").addEventListener("click", async () => {
  $("importMsg").textContent = "Reading the dashboard's Apply kit…";
  try {
    const kit = await profileFromDashboard();
    $("importMsg").textContent = kit ? importKit(kit) : "The dashboard's Apply kit is empty. Fill it in there first, or type your details below.";
  } catch (e) {
    $("importMsg").textContent = e.message;
  }
});

$("pasteToggle").addEventListener("click", () => { $("pasteBox").hidden = !$("pasteBox").hidden; if (!$("pasteBox").hidden) $("pasteText").focus(); });
$("pasteImport").addEventListener("click", () => {
  try {
    const data = JSON.parse($("pasteText").value);
    $("importMsg").textContent = importKit(data && data.profile && typeof data.profile === "object" ? data.profile : data);
    $("pasteText").value = "";
    $("pasteBox").hidden = true;
  } catch (e) {
    $("importMsg").textContent = "That isn't an Onward export. In the dashboard's Apply kit, press Copy for extension and paste the result.";
  }
});

/* --------------------------------------------------------------- memory */

let memCache = [];

let useSince = 0;

async function readMemory() {
  const all = await api.storage.local.get(null);
  useSince = all.useLogSince || 0;
  memCache = Object.entries(all).filter(([k]) => k.startsWith("mem:"))
    .map(([k, v]) => Object.assign({ key: k, uses: Array.isArray(all["use:" + k.slice(4)]) ? all["use:" + k.slice(4)] : [] }, v))
    .sort((a, b) => (b.updated || 0) - (a.updated || 0));
}

const day = (t) => new Date(t).toLocaleDateString();

const SHOW = 100;
async function renderMemory() {
  await readMemory();
  drawMemory();
}

/** A flag for answers that name the company they were written for, or its products. */
function named(m) {
  const u = R.savedAnswerUse(m, "");
  return u.use === "warn" ? el("div", { class: "flag" }, `Names ${u.names.join(", ")}. Only offered on other companies' forms behind a warning.`) : null;
}

function drawMemory() {
  const q = R.normQ($("memSearch").value);
  const hits = q ? memCache.filter((m) => R.normQ(m.q + " " + m.a + " " + (m.company || "")).includes(q)) : memCache;
  $("memCount").textContent = memCache.length ? `(${memCache.length})` : "";
  const list = $("memList");
  list.replaceChildren();
  for (const m of hits.slice(0, SHOW)) {
    const ta = el("textarea", { rows: m.a.length > 120 ? 4 : 1, "aria-label": "Answer to: " + m.q }, m.a);
    ta.addEventListener("change", async () => {
      m.a = ta.value;
      m.updated = Date.now();
      const { key, ...rest } = m;
      await api.storage.local.set({ [key]: rest });
      saved();
    });
    list.append(el("li", null,
      el("div", { class: "q" }, m.q),
      ta,
      named(m),
      el("div", { class: "meta" }, m.uses.length
        ? "Used on: " + m.uses.slice(-8).reverse().map((u) => `${u.company || u.host}${u.title ? " (" + u.title + ")" : ""}, ${day(u.date)}`).join("; ")
          + (m.uses.length > 8 ? `; and ${m.uses.length - 8} earlier` : "")
        : "Not used on any form since uses started being recorded."),
      el("div", { class: "meta" },
        el("span", null, [m.company ? "Written for " + m.company : "", m.host, m.updated ? "saved " + day(m.updated) : ""].filter(Boolean).join(" · ")),
        el("button", { type: "button", class: "ghost danger", onclick: async () => {
          await api.storage.local.remove([m.key, "use:" + m.key.slice(4)]);
          memCache = memCache.filter((x) => x !== m);
          drawMemory();
          saved("Deleted");
        } }, "Delete"))));
  }
  $("useSince").textContent = "Where an answer was used is recorded from " + (useSince ? day(useSince) : "today")
    + ". Uses before that weren't recorded, so a saved answer may have gone onto forms that aren't listed.";
  if (!memCache.length) list.append(el("li", { class: "muted" }, "Nothing saved yet. Answers you type into custom questions on application forms show up here."));
  $("memMore").textContent = hits.length > SHOW ? `Showing ${SHOW} of ${hits.length}. Search to narrow down.` : "";
}

$("memSearch").addEventListener("input", drawMemory);

/* --------------------------------------------------------------- backup */

$("exportAll").addEventListener("click", async () => {
  const all = await api.storage.local.get(null);
  delete all.resume;
  const blob = new Blob([JSON.stringify({ jobradar: "autofill-backup", version: 1, data: all }, null, 1)], { type: "application/json" });
  const a = el("a", { href: URL.createObjectURL(blob), download: "onward-autofill-backup.json" });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
});

$("importAll").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  try {
    const doc = JSON.parse(await f.text());
    if (!doc || doc.jobradar !== "autofill-backup" || typeof doc.data !== "object") throw new Error();
    const data = {};
    for (const [k, v] of Object.entries(doc.data)) {
      if (["profile", "answers", "settings", "useLogSince", "history"].includes(k) || k.startsWith("mem:") || k.startsWith("use:")) data[k] = v;
    }
    await api.storage.local.set(data);
    await load();
    saved("Backup imported");
  } catch (err) {
    saved("That file isn't an Onward Autofill backup.");
  }
});

/* ---------------------------------------------------------- permissions */

async function checkPermissions() {
  const origins = api.runtime.getManifest().host_permissions;
  const ok = await api.permissions.contains({ origins });
  $("perm").hidden = ok;
  $("grant").onclick = async () => {
    const granted = await api.permissions.request({ origins });
    $("perm").hidden = granted;
  };
}

$("unsupported").hidden = location.hash !== "#unsupported";
api.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && Object.keys(changes).some((k) => k.startsWith("mem:"))) renderMemory();
});
checkPermissions();
load();
