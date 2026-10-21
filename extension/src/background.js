/* Background: the toolbar button and shortcut, and the only code that talks
 * to the local It gets easier helper (server.py on 127.0.0.1:8765). Content
 * scripts can't reach it themselves because of the page's CORS rules. */
"use strict";

const api = globalThis.browser || globalThis.chrome;
const HELPER = "http://127.0.0.1:8765";
const SUPPORTED = /^https:\/\/([a-z0-9-]+\.)*(greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|myworkdaysite\.com)\//i;

async function helper(path, body) {
  // The custom header makes a browser preflight any cross-site attempt to
  // send the same request, and the helper never answers preflights.
  const init = { headers: { "X-JobRadar-Client": "autofill" } };
  if (body !== undefined) {
    init.method = "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(HELPER + path, init);
  } catch (e) {
    throw new Error("The It gets easier helper isn't running. Start it with: pythonw server.py");
  }
  let data = null;
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error((data && data.error) || `The helper answered ${res.status}.`);
  return data;
}

// The job (from It gets easier) that each tab's form belongs to, so the "application
// received" page can be tied to it even when its URL is different
// (Workday, embedded forms). Session storage survives the service worker
// being stopped; it is cleared when the browser closes.
const tabJobs = {
  async get(tabId) {
    try { return (await api.storage.session.get("tab:" + tabId))["tab:" + tabId] || null; } catch (e) { return null; }
  },
  async set(tabId, job) {
    try { await api.storage.session.set({ ["tab:" + tabId]: Object.assign({ at: Date.now() }, job) }); } catch (e) {}
  },
  async del(tabId) {
    try { await api.storage.session.remove("tab:" + tabId); } catch (e) {}
  },
};
if (api.tabs && api.tabs.onRemoved) api.tabs.onRemoved.addListener((tabId) => { tabJobs.del(tabId); });

function clip(s, n) {
  return String(s == null ? "" : s).slice(0, n);
}

async function fillTab(tab) {
  if (!tab || tab.id == null) return;
  let res;
  try {
    res = await api.tabs.sendMessage(tab.id, { type: "jr:fill" });
  } catch (e) {
    res = undefined;
  }
  if (res) return;
  if (SUPPORTED.test(tab.url || "")) {
    // A supported site, but no form yet (a job description or sign-in page).
    await api.action.setBadgeBackgroundColor({ tabId: tab.id, color: "#8a5a00" });
    await api.action.setBadgeText({ tabId: tab.id, text: "?" });
    await api.action.setTitle({ tabId: tab.id, title: "No application form on this page yet. Open the application, then try again." });
    setTimeout(() => {
      api.action.setBadgeText({ tabId: tab.id, text: "" }).catch(() => {});
      api.action.setTitle({ tabId: tab.id, title: "Autofill this application" }).catch(() => {});
    }, 4000);
    return;
  }
  api.tabs.create({ url: api.runtime.getURL("src/options/options.html#unsupported") });
}

api.action.onClicked.addListener(fillTab);

// Nothing can be filled until the profile exists, so start there.
api.runtime.onInstalled.addListener((details) => {
  if (details.reason === "install") api.runtime.openOptionsPage();
});

if (api.commands && api.commands.onCommand) {
  api.commands.onCommand.addListener(async (cmd) => {
    if (cmd !== "fill-form") return;
    const [tab] = await api.tabs.query({ active: true, currentWindow: true });
    fillTab(tab);
  });
}

const HANDLERS = {
  /* The resume and answers the helper prepared for this form's job. */
  async "jr:prepared"(msg, sender) {
    const url = clip(msg.url, 2000);
    let data;
    try {
      data = await helper("/api/prepared?url=" + encodeURIComponent(url));
    } catch (e) {
      return { found: false, error: e.message };
    }
    if (sender.tab && sender.tab.id != null && data.job) await tabJobs.set(sender.tab.id, data.job);
    return Object.assign({ found: true }, data);
  },
  /* The ATS showed its "application received" page. */
  async "jr:applied"(msg, sender) {
    const tabId = sender.tab && sender.tab.id;
    const known = tabId != null ? await tabJobs.get(tabId) : null;
    // Pages without a URL pattern (Ashby, Workday) only count for a tab that showed a known job's form.
    if (!known && !msg.byUrl) return { recorded: false };
    const res = await helper("/api/applied", { key: known ? clip(known.key, 500) : "", url: clip(msg.url, 2000) });
    if (tabId != null) await tabJobs.del(tabId);
    return Object.assign({ recorded: true, company: known ? known.company : "", title: known ? known.title : "" }, res);
  },
  async "jr:draftStart"(msg) {
    const p = msg.payload || {};
    return helper("/api/draft", {
      question: clip(p.question, 2000), context: clip(p.context, 2000), company: clip(p.company, 200),
      title: clip(p.title, 300), url: clip(p.url, 2000), jd: clip(p.jd, 30000),
      max_chars: Math.max(0, Math.min(Number(p.max_chars) || 0, 20000)),
      backend: clip(p.backend, 40),
    });
  },
  async "jr:answersStart"(msg) {
    const p = msg.payload || {};
    const questions = (Array.isArray(p.questions) ? p.questions : []).slice(0, 60).map((q) => ({
      id: clip(q.id, 20), question: clip(q.question, 1500), kind: clip(q.kind, 20), context: clip(q.context, 800),
      max_chars: Math.max(0, Math.min(Number(q.max_chars) || 0, 20000)),
      options: (Array.isArray(q.options) ? q.options : []).slice(0, 200).map((o) => clip(o, 300)),
    }));
    return helper("/api/answers", {
      questions, profile: p.profile && typeof p.profile === "object" ? p.profile : {},
      company: clip(p.company, 200), title: clip(p.title, 300), url: clip(p.url, 2000), jd: clip(p.jd, 30000),
      backend: clip(p.backend, 40),
    });
  },
  async "jr:draftStatus"(msg) {
    if (!/^[a-f0-9]{6,32}$/.test(String(msg.id))) throw new Error("Bad draft id");
    return helper("/api/draft/" + msg.id);
  },
  async "jr:helperStatus"() {
    return helper("/api/status");
  },
  async "jr:openOptions"() {
    await api.runtime.openOptionsPage();
    return { ok: true };
  },
};

api.runtime.onMessage.addListener((msg, sender, respond) => {
  if (sender.id !== api.runtime.id || !msg || !HANDLERS[msg.type]) return false;
  HANDLERS[msg.type](msg, sender).then(respond, (e) => respond({ error: e.message }));
  return true;
});
