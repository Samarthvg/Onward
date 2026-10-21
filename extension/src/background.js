/* Background: the toolbar button and shortcut, and the only code that talks
 * to the local Job Radar helper (server.py on 127.0.0.1:8765). Content
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
    throw new Error("The Job Radar helper isn't running. Start it with: pythonw server.py");
  }
  let data = null;
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error((data && data.error) || `The helper answered ${res.status}.`);
  return data;
}

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
  HANDLERS[msg.type](msg).then(respond, (e) => respond({ error: e.message }));
  return true;
});
