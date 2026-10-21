/* Content-script entry point: finds the application form, fills what the
 * saved profile and answers cover, remembers answers the user types into
 * custom questions, and reports the rest in the panel.
 *
 * It never clicks Submit, Next, Apply or any other control that sends or
 * advances the form, and never creates accounts.
 */
(function (root) {
  "use strict";
  const JR = root.JR;
  const api = root.browser || root.chrome;
  const R = JR.rules;
  const { classify, answerFor, alternatives, asText, normQ, DECLINE } = R;
  const { textOf, typeInto, sleep } = JR.dom;
  const W = JR.widgets;
  const inFrame = window !== window.top;

  let site = JR.sites.detect(document);
  let store = null;
  let running = false;
  const filledByUs = new WeakMap();   // element -> value we put there
  const lastSaved = new Map();        // memory key -> value last written
  const stepsFilled = new Set();
  let stepSig = "";

  /* ------------------------------------------------------------- storage */

  async function load() {
    const s = await api.storage.local.get(["profile", "answers", "settings"]);
    store = {
      profile: Object.assign({}, R.DEFAULT_PROFILE, s.profile),
      answers: Object.assign({}, R.DEFAULT_ANSWERS, s.answers),
      settings: Object.assign({}, R.DEFAULT_SETTINGS, s.settings),
    };
    return store;
  }

  const memKey = (label) => {
    const k = normQ(label);
    return k.length >= 3 ? "mem:" + k : null;
  };

  /* ---------------------------------------------------------------- fill */

  function mark(q, state) {
    const n = q.kind === "file" || q.kind === "radio" || q.kind === "checkbox" || q.kind === "yesno" ? (q.container || q.el.parentElement) : q.el;
    if (!n) return;
    n.setAttribute("data-jr-state", state);
    n.addEventListener("focusin", () => n.removeAttribute("data-jr-state"), { once: true });
  }

  function ensureStyle() {
    if (document.getElementById("jobradar-autofill-style")) return;
    const st = document.createElement("style");
    st.id = "jobradar-autofill-style";
    st.textContent = '[data-jr-state="filled"]{outline:2px solid rgba(47,107,58,.55)!important;outline-offset:1px}' +
      '[data-jr-state="review"]{outline:2px solid rgba(200,140,0,.8)!important;outline-offset:1px}';
    (document.head || document.documentElement).append(st);
  }

  async function answerOne(q, cls, mem, ctx) {
    const base = { q, label: q.label || q.attrs, kind: q.kind };
    const cur = W.get(q);
    if (cur) {
      const ours = filledByUs.get(q.el);
      return ours != null ? Object.assign(base, { status: "filled", value: cur, source: ours.source })
        : Object.assign(base, { status: "had", value: cur });
    }
    if (cls.cat === "never") return Object.assign(base, { status: "needs", why: cls.why });

    if (cls.cat === "file") {
      const r = await W.set(q, [], cls, ctx);
      return r.ok ? Object.assign(base, { status: "filled", value: r.chosen, source: "resume" })
        : Object.assign(base, { status: "needs", why: r.why });
    }

    if (cls.cat === "profile" || cls.cat === "std" || cls.cat === "eeo") {
      const v = answerFor(cls, store.profile, store.answers);
      if (!v) {
        const where = cls.cat === "profile" ? "add it to your profile in the extension options" : "no saved answer; set one in the extension options";
        return Object.assign(base, { status: "needs", why: where });
      }
      const textual = R.TEXT_KINDS.has(q.kind) || q.kind === "textarea";
      if (textual && cls.cat === "eeo") return Object.assign(base, { status: "needs", why: "answer this yourself" });
      const values = textual ? [asText(cls, v)] : alternatives(cls, v);
      if (!values[0]) return Object.assign(base, { status: "needs", why: "answer this yourself" });
      if (q.kind === "checkbox" && q.multi && cls.key === "languages") {
        // Check all that apply: tick every language that's listed.
        const ticked = [];
        for (const lang of values) { const r = await W.set(q, [lang], cls, ctx); if (r.ok) ticked.push(r.chosen); }
        return ticked.length ? Object.assign(base, { status: "filled", value: ticked.join(", "), source: "profile" })
          : Object.assign(base, { status: "needs", why: "none of your languages are listed" });
      }
      const r = await W.set(q, values, cls, ctx);
      if (r.ok) return Object.assign(base, { status: "filled", value: r.chosen, source: cls.cat });
      return Object.assign(base, { status: "needs", why: r.why + (v === DECLINE ? " (no decline option here)" : "") });
    }

    // Custom question: offer what the user answered last time.
    const k = memKey(q.label);
    const m = k && mem[k];
    const canDraft = !!(cls.writing && store.settings.helper && (q.kind === "textarea" || q.kind === "text"));
    if (m && m.a) {
      if (cls.writing || q.kind === "textarea") {
        return Object.assign(base, { status: "needs", why: "write this one, or reuse a saved answer", offer: m, canDraft });
      }
      if (store.settings.fillRemembered) {
        const r = await W.set(q, [m.a], cls, ctx);
        if (r.ok) return Object.assign(base, { status: "filled", value: r.chosen, source: "remembered" });
      }
      return Object.assign(base, { status: "needs", why: "your saved answer didn't fit; answer it here", offer: q.kind === "text" ? m : null });
    }
    return Object.assign(base, { status: "needs", why: cls.writing ? "write this one" : "answer this yourself", canDraft });
  }

  async function fill() {
    if (running) return summary();
    if (!site) site = JR.sites.detect(document);
    if (!site || !JR.sites.formRoot(document, site)) return null;
    running = true;
    JR.panel.update({ open: true, busy: true, status: "Filling…" });
    try {
      await load();
      ensureStyle();
      const qs = JR.sites.collect(document, site);
      const keys = qs.map((q) => memKey(q.label)).filter(Boolean);
      const mem = keys.length ? await api.storage.local.get(keys) : {};
      const ctx = {};
      if (qs.some((q) => q.kind === "file")) ctx.resume = (await api.storage.local.get("resume")).resume || null;

      const items = [];
      for (const q of qs) {
        const cls = classify(q.label, q.kind, q.attrs, q.section);
        q.cls = cls;
        const it = await answerOne(q, cls, mem, ctx);
        if (it.status === "filled" && !filledByUs.has(q.el)) {
          filledByUs.set(q.el, { value: it.value, source: it.source });
          mark(q, it.source === "remembered" ? "review" : "filled");
        }
        items.push(it);
      }
      if (store.settings.aiFill && store.settings.helper && store.settings.draftBackend !== "manual") {
        await aiFill(items);
      }
      const n = items.filter((i) => i.status === "filled").length;
      const left = items.filter((i) => i.status === "needs").length;
      stepsFilled.add(stepSig);
      JR.panel.update({ busy: false, step: "", items,
        status: `Filled ${n} field${n === 1 ? "" : "s"}.` + (left ? ` ${left} need${left === 1 ? "s" : ""} you.` : " Review, then submit yourself.") });
      return summary();
    } finally {
      running = false;
    }
  }

  /* ------------------------------------------------------- AI answers */

  const BACKEND_NAMES = { chatgpt: "ChatGPT", "claude-code": "Claude", ollama: "the local model" };
  const AI_TEXT = new Set(["text", "email", "url", "number", "search", "date", "combo", "prompt", "textarea"]);

  async function helperJob(start) {
    if (!start || start.error) throw new Error((start && start.error) || "The Job Radar helper didn't answer.");
    const end = Date.now() + 15 * 60 * 1000;
    for (;;) {
      await sleep(2000);
      const st = await api.runtime.sendMessage({ type: "jr:draftStatus", id: start.id });
      if (st && st.state === "done") return st.result;
      if (!st || st.state === "error") throw new Error((st && st.error) || "The helper couldn't answer.");
      if (Date.now() > end) throw new Error("The helper took too long.");
    }
  }

  /**
   * Sends the questions nothing saved could answer to the helper in one
   * batch and fills in what comes back, marked for review. Only custom
   * questions go: sponsorship, demographic, legal and consent questions
   * never do, and nothing already typed is replaced.
   */
  async function aiFill(items) {
    const cands = [];
    for (const it of items) {
      const q = it.q;
      if (it.status !== "needs" || !q.cls || q.cls.cat !== "custom" || it.offer || q.single || q.kind === "file") continue;
      let kind = q.kind === "textarea" ? "textarea" : AI_TEXT.has(q.kind) ? "text" : q.kind === "checkbox" ? "multi" : "choice";
      let opts = [];
      if (kind === "choice" || kind === "multi") {
        opts = (await W.options(q)).filter(Boolean);
        // A searchable dropdown with nothing listed yet takes a typed answer.
        if (!opts.length) { if (q.kind !== "rselect") continue; kind = "text"; }
      }
      cands.push({ it, kind, opts });
    }
    if (!cands.length) return;
    const who = BACKEND_NAMES[store.settings.draftBackend] || store.settings.draftBackend;
    JR.panel.update({ items, status: `Asking ${who} about ${cands.length} more question${cands.length === 1 ? "" : "s"}…` });
    const info = JR.sites.jobInfo(document);
    const payload = {
      questions: cands.map((c, i) => ({ id: String(i), question: c.it.label, kind: c.kind, options: c.opts,
        max_chars: c.it.q.el.maxLength > 0 ? c.it.q.el.maxLength : 0,
        context: c.it.q.container ? textOf(c.it.q.container).slice(0, 800) : "" })),
      profile: store.profile, company: info.company, title: info.title, url: location.href,
      jd: JR.sites.jobDescription(document), backend: store.settings.draftBackend,
    };
    let result;
    try {
      result = await helperJob(await api.runtime.sendMessage({ type: "jr:answersStart", payload }));
    } catch (e) {
      for (const c of cands) c.it.why += ` (${who} couldn't help: ${e.message})`;
      return;
    }
    const got = new Map((result.answers || []).map((a) => [a.id, a]));
    for (const [i, c] of cands.entries()) {
      const a = got.get(String(i));
      const q = c.it.q;
      if (!a || W.get(q)) { if (!a) c.it.why = `${who} left this for you`; continue; }
      const picks = a.choices && a.choices.length ? a.choices : a.answer ? [a.answer] : [];
      const done = [];
      for (const p of c.kind === "multi" ? picks : picks.slice(0, 1)) {
        const r = c.kind === "textarea" || (c.kind === "text" && q.kind !== "rselect")
          ? (typeInto(q.el, p) ? { ok: true, chosen: p } : { ok: false })
          : await W.set(q, [p], q.cls, {});
        if (r.ok) done.push(r.chosen);
      }
      if (!done.length) { c.it.why = `${who}'s answer didn't fit; answer it here`; continue; }
      const value = done.join(", ");
      filledByUs.set(q.el, { value: W.get(q) || value, source: "ai" });
      mark(q, "review");
      Object.assign(c.it, { status: "filled", value, source: "ai", why: "", canDraft: false });
    }
  }

  function summary() {
    const items = JR.panel.view.items;
    return { site: site && site.name, filled: items.filter((i) => i.status === "filled").map((i) => i.label),
      needs: items.filter((i) => i.status === "needs").map((i) => i.label) };
  }

  /* ------------------------------------------------------------- memory */

  async function snapshot() {
    if (!store) await load();
    if (!store.settings.remember || !site) return;
    const qs = JR.sites.collect(document, site);
    const info = JR.sites.jobInfo(document);
    const writes = {};
    for (const q of qs) {
      if (q.kind === "file") continue;
      const cls = classify(q.label, q.kind, q.attrs, q.section);
      if (cls.cat !== "custom") continue;   // profile, sponsorship and EEO answers live in the options page
      const k = memKey(q.label);
      const v = W.get(q);
      if (!k || !v || v.length > 5000) continue;
      const ours = filledByUs.get(q.el);
      if (ours && ours.value === v) continue;   // still what we put there
      if (lastSaved.get(k) === v) continue;
      lastSaved.set(k, v);
      writes[k] = { q: q.label, a: v, kind: q.kind, writing: !!cls.writing, company: info.company || "", host: location.hostname, updated: Date.now() };
    }
    if (Object.keys(writes).length) await api.storage.local.set(writes);
  }

  let snapTimer = 0;
  function onEdit(e) {
    // Only the user's own typing and clicking count; the events this script
    // fires while filling are untrusted.
    if (!e.isTrusted || running) return;
    if (e.target && e.target.closest && e.target.closest("[data-jr-ignore]")) return;
    clearTimeout(snapTimer);
    snapTimer = setTimeout(() => snapshot().catch(() => {}), 800);
  }

  /* -------------------------------------------------------------- drafts */

  async function draft(it) {
    it.drafting = true;
    it.why = "Drafting with your " + (store.settings.draftBackend === "claude-code" ? "Claude" : store.settings.draftBackend === "chatgpt" ? "ChatGPT" : store.settings.draftBackend) + " login…";
    JR.panel.render();
    const info = JR.sites.jobInfo(document);
    const payload = {
      question: it.label, context: it.q.container ? textOf(it.q.container).slice(0, 1500) : "",
      company: info.company, title: info.title, url: location.href,
      jd: JR.sites.jobDescription(document), max_chars: it.q.el.maxLength > 0 ? it.q.el.maxLength : 0,
      backend: store.settings.draftBackend,
    };
    try {
      const start = await api.runtime.sendMessage({ type: "jr:draftStart", payload });
      if (!start || start.error) throw new Error((start && start.error) || "The Job Radar helper didn't answer.");
      if (start.prompt) { it.prompt = start.prompt; it.why = "Manual mode: copy the prompt below."; return; }
      const end = Date.now() + 15 * 60 * 1000;
      for (;;) {
        await sleep(2000);
        const st = await api.runtime.sendMessage({ type: "jr:draftStatus", id: start.id });
        if (st && st.state === "done") {
          const answer = String(st.result && st.result.answer || "").trim();
          if (!answer) throw new Error("The draft came back empty.");
          if (W.get(it.q)) { it.why = "You started typing, so the draft wasn't inserted."; it.prompt = answer; return; }
          typeInto(it.q.el, answer);
          filledByUs.set(it.q.el, { value: answer, source: "draft" });
          mark(it.q, "review");
          Object.assign(it, { status: "filled", value: answer, source: "draft", why: "" });
          return;
        }
        if (!st || st.state === "error") throw new Error((st && st.error) || "Drafting failed.");
        if (Date.now() > end) throw new Error("Drafting took too long.");
      }
    } catch (e) {
      it.why = e.message;
    } finally {
      it.drafting = false;
      JR.panel.render();
    }
  }

  /* ---------------------------------------------------------------- panel */

  const handlers = {
    fill: () => fill(),
    focus(it) {
      const n = it.q.container || it.q.el;
      n.scrollIntoView({ block: "center", behavior: "smooth" });
      if (typeof it.q.el.focus === "function" && it.q.kind !== "file") try { it.q.el.focus({ preventScroll: true }); } catch (e) {}
    },
    async useSaved(it) {
      const r = await W.set(it.q, [it.offer.a], it.q.cls, {});
      if (r.ok) {
        filledByUs.set(it.q.el, { value: r.chosen, source: "remembered" });
        mark(it.q, "review");
        Object.assign(it, { status: "filled", value: r.chosen, source: "remembered", why: "" });
      } else it.why = r.why;
      JR.panel.render();
    },
    draft,
    async copy(text, btn) {
      try { await navigator.clipboard.writeText(text); btn.textContent = "Copied"; }
      catch (e) { btn.textContent = "Select the text and press Ctrl+C"; }
    },
    options: () => api.runtime.sendMessage({ type: "jr:openOptions" }),
  };

  /* ------------------------------------------------------------- watching */

  function stepSignature() {
    const t = document.querySelector("[data-automation-id='applyFlowPage'] h2, [data-automation-id='pageHeaderText'], [data-automation-id='progressBarActiveStep']");
    return location.pathname + "|" + (t ? textOf(t) : "");
  }

  let checkTimer = 0;
  function check() {
    if (!site) site = JR.sites.detect(document);
    const form = site && JR.sites.formRoot(document, site);
    if (!form) return;
    JR.panel.mount(handlers);
    if (inFrame) JR.panel.update({ frame: true });
    const sig = stepSignature();
    if (sig !== stepSig) {
      const first = !stepSig;
      stepSig = sig;
      if (!first && JR.panel.view.open) JR.panel.update({ step: "new", items: [], status: "New step. Press Fill this step when you're ready." });
      load().then(() => {
        if (store.settings.autoRun && !stepsFilled.has(sig)) fill();
      });
    }
  }

  /* Greenhouse (Remix) and other server-rendered React apps hydrate the
     whole document. Anything added to the DOM before hydration finishes
     makes React discard and re-render the page, which breaks its handlers,
     so nothing is added until the page has loaded and gone quiet. */
  function settled() {
    return new Promise((resolve) => {
      const start = () => {
        let quiet = 0;
        const done = () => { clearTimeout(quiet); clearTimeout(cap); mo.disconnect(); resolve(); };
        const mo = new MutationObserver(() => { clearTimeout(quiet); quiet = setTimeout(done, 700); });
        mo.observe(document.documentElement, { childList: true, subtree: true });
        quiet = setTimeout(done, 700);
        const cap = setTimeout(done, 6000);
      };
      if (document.readyState === "complete") start();
      else window.addEventListener("load", start, { once: true });
    });
  }

  for (const t of ["input", "change", "click", "keyup"]) document.addEventListener(t, onEdit, true);
  settled().then(() => {
    const obs = new MutationObserver(() => {
      clearTimeout(checkTimer);
      checkTimer = setTimeout(check, 600);
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
    check();
  });

  api.runtime.onMessage.addListener((msg, sender, respond) => {
    if (!msg || msg.type !== "jr:fill") return false;
    if (!site) site = JR.sites.detect(document);
    if (!site || !JR.sites.formRoot(document, site)) return false;   // let a frame with the form answer
    JR.panel.mount(handlers);
    fill().then(respond, (e) => respond({ error: e.message }));
    return true;
  });

  api.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && (changes.profile || changes.answers || changes.settings)) store = null;
  });

  JR.main = { fill, snapshot, summary };
})(globalThis);
