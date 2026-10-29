"use strict";

const api = globalThis.browser || globalThis.chrome;
const R = globalThis.JR.rules;
const $ = (id) => document.getElementById(id);
let state = { settings: {} };

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
  const s = await api.storage.local.get("settings");
  state.settings = Object.assign({}, R.DEFAULT_SETTINGS, s.settings);
  renderSettings();
  renderMemory();
  checkHelper();
}

/* ------------------------------------------------------------- settings */

function renderSettings() {
  for (const k of ["autoRun", "fillSteps", "fitSkills", "remember", "fillRemembered", "helper", "aiFill"]) {
    const box = $("set-" + k);
    box.checked = !!state.settings[k];
    box.onchange = () => { state.settings[k] = box.checked; save("settings"); };
  }
}

/* ---------------------------------------------------------------- onward */

const BACKENDS = { chatgpt: "ChatGPT", "claude-code": "Claude", "openai-api": "your OpenAI key", "claude-api": "your Anthropic key" };

async function checkHelper() {
  const out = $("helperStatus");
  out.textContent = "Checking…";
  const kit = await api.runtime.sendMessage({ type: "jr:kit", fresh: true }).catch((e) => ({ error: e.message }));
  if (!kit || kit.error) {
    out.textContent = (kit && kit.error) || "No answer from Onward.";
    return;
  }
  const name = [kit.profile && kit.profile.value && kit.profile.value.firstName, kit.profile && kit.profile.value && kit.profile.value.lastName]
    .filter(Boolean).join(" ");
  const bits = [kit.offline ? "Onward isn't running, so the last copy it sent is used" : "Connected to Onward",
    name ? "filling as " + name : "the Apply kit is still empty",
    kit.resume ? "attaching " + kit.resume.name + " unless a job has its own" : "no master resume yet",
    "drafting with " + (BACKENDS[kit.draft_backend] || kit.draft_backend)];
  out.textContent = bits.join(", ") + ".";
}
$("helperCheck").addEventListener("click", checkHelper);

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

/** Saved before answers tied to one company stopped being kept: they name it or its products, or their question does. */
const tiedToCompany = (m) => R.savedAnswerUse(m, "").use === "warn" || (!!m.company && R.mentions(m.q, R.companyNames(m.company)).length > 0);

function drawTied() {
  const n = memCache.filter(tiedToCompany).length;
  const b = $("memTied");
  b.hidden = !n;
  b.textContent = `Remove ${n} answer${n === 1 ? "" : "s"} tied to one company`;
}

$("memTied").addEventListener("click", async () => {
  const tied = memCache.filter(tiedToCompany);
  if (!tied.length || !confirm(`Remove ${tied.length} saved answer${tied.length === 1 ? "" : "s"} that name a company or its products? Where they were used is removed too.`)) return;
  await api.storage.local.remove(tied.flatMap((m) => [m.key, "use:" + m.key.slice(4)]));
  memCache = memCache.filter((m) => !tied.includes(m));
  drawMemory();
  saved(`Removed ${tied.length}`);
});

function drawMemory() {
  const q = R.normQ($("memSearch").value);
  const hits = q ? memCache.filter((m) => R.normQ(m.q + " " + m.a + " " + (m.company || "")).includes(q)) : memCache;
  $("memCount").textContent = memCache.length ? `(${memCache.length})` : "";
  drawTied();
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
