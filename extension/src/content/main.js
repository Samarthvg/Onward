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
  let sections = [];                  // Work Experience / Education sections that differ from the resume

  /** Work-history and education fields are the resume's to answer, never the AI's. */
  const notForAI = (q) => !!(q.history || R.historyField(q.label, q.attrs));

  /* ------------------------------------------------------------- storage */

  async function load() {
    const s = await api.storage.local.get(["profile", "answers", "settings"]);
    store = {
      profile: Object.assign({}, R.DEFAULT_PROFILE, s.profile),
      answers: Object.assign({}, R.DEFAULT_ANSWERS, s.answers),
      settings: R.withDefaults(s.settings).settings,
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
      return r.ok ? Object.assign(base, { status: "filled", value: r.chosen, source: ctx.resume && ctx.resume.tailored ? "tailored" : "resume" })
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
    const canDraft = !!(cls.writing && store.settings.helper && (q.kind === "textarea" || q.kind === "text") && !notForAI(q));
    if (m && m.a) {
      // An answer written for another company that names it stays behind a warning.
      const use = R.savedAnswerUse(m, ctx.company);
      const offerWarn = use.use === "warn" ? { company: use.company, names: use.names } : null;
      if (cls.writing || q.kind === "textarea") {
        return Object.assign(base, { status: "needs", why: offerWarn ? "write this one" : "write this one, or reuse a saved answer",
          offer: m, offerKey: k, offerWarn, canDraft });
      }
      if (store.settings.fillRemembered && !offerWarn) {
        const r = await W.set(q, [m.a], cls, ctx);
        if (r.ok) {
          logUse(k, m);
          return Object.assign(base, { status: "filled", value: r.chosen, source: "remembered" });
        }
      }
      return Object.assign(base, { status: "needs", why: offerWarn ? "answer this yourself" : "your saved answer didn't fit; answer it here",
        offer: q.kind === "text" || offerWarn ? m : null, offerKey: k, offerWarn });
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
      sections = [];
      let qs = JR.sites.collect(document, site);

      // Work Experience, Education, Skills and Languages come straight from the
      // master resume and the profile, never from the AI.
      const items = [];
      let hist = null;
      const H = JR.history;
      const recollect = () => JR.sites.collect(document, site);
      const workday = !!site.workday;
      // Workday shows an empty section as a heading and an Add button, with no fields yet.
      const wantsHistory = H.rows(qs).some((r) => r.type === "work" || r.type === "edu")
        || (workday && !!(H.addButton("work", document) || H.addButton("edu", document)));
      const skillsQ = H.skillsQuestion(qs);
      if (store.settings.helper && (wantsHistory || skillsQ)) {
        JR.panel.update({ status: "Filling work history and education from your resume…" });
        hist = await api.runtime.sendMessage({ type: "jr:history", url: location.href }).catch(() => null);
        if (!hist || hist.error || !(hist.work || hist.education)) hist = null;
      }
      if (hist && wantsHistory) {
        const r = await H.fill(qs, hist, { recollect, allowAdd: workday, ours: filledByUs });
        qs = r.qs;
        for (const it of r.items) { if (it.status === "filled") mark(it.q, "filled"); items.push(it); }
        sections = Object.entries(r.diffs).filter(([, d]) => d.some((x) => x.canReplace))
          .map(([type, d]) => ({ type, label: H.SECTION_NAME[type], diffs: d, undo: null }));
        for (const [type, n] of Object.entries(r.extra)) {
          if (n > 0) items.push({ q: qs[0], label: H.SECTION_NAME[type], status: "needs", history: type,
            why: `your resume has ${n} more ${type === "work" ? "job" : "school"}${n === 1 ? "" : "s"} than this form shows; add ${n === 1 ? "a row" : "rows"} and press Fill again` });
        }
      }
      const extraTypes = ["lang", "web", "cert"];
      if (H.skillsQuestion(qs) || H.rows(qs).some((r) => extraTypes.includes(r.type))
          || (workday && extraTypes.some((t) => H.addButton(t, document)))) {
        // Links already asked for on their own (a LinkedIn field) aren't repeated as websites.
        const own = (re) => qs.some((q) => !q.history && re.test(q.label + " " + q.attrs));
        const p = store.profile;
        const websites = [p.portfolio, p.github, own(/linked ?in/i) ? "" : p.linkedin].filter(Boolean);
        const r = await H.fillExtras(qs, { skills: hist ? hist.skills || [] : [], languages: p.languages, websites,
          certifications: hist ? hist.certifications || [] : [],
          recollect, allowAdd: workday, ours: filledByUs, progress: (status) => JR.panel.update({ status }) });
        qs = r.qs;
        for (const it of r.items) { if (it.status === "filled") mark(it.q, "filled"); items.push(it); }
      }
      // Those rows are answered above; mark them so the loop below and the AI leave them alone.
      H.rows(qs);
      const skillsNow = H.skillsQuestion(qs);
      if (skillsNow) skillsNow.history = { type: "skills", field: "skills" };
      const histTouched = !!hist || items.length > 0;

      const keys = qs.map((q) => memKey(q.label)).filter(Boolean);
      const mem = keys.length ? await api.storage.local.get(keys) : {};
      const ctx = { company: JR.sites.jobInfo(document).company || "" };
      const prep = store.settings.helper ? await getPrepared() : null;
      if (qs.some((q) => q.kind === "file")) ctx.resume = resumeFor(prep, (await api.storage.local.get("resume")).resume || null);

      for (const q of qs) {
        if (histTouched && q.history) continue;   // filled above
        const cls = classify(q.label, q.kind, q.attrs, q.section);
        q.cls = cls;
        const it = await answerOne(q, cls, mem, ctx);
        if (it.status === "filled" && !filledByUs.has(q.el)) {
          filledByUs.set(q.el, { value: it.value, source: it.source });
          mark(q, it.source === "remembered" ? "review" : "filled");
        }
        items.push(it);
      }
      if (prep && prep.answers && prep.answers.length) await usePrepared(items, prep.answers);
      if (store.settings.aiFill && store.settings.helper && store.settings.draftBackend !== "manual") {
        await aiFill(items);
      }
      const n = items.filter((i) => i.status === "filled").length;
      const left = items.filter((i) => i.status === "needs").length;
      stepsFilled.add(stepSig);
      JR.panel.update({ busy: false, step: "", items, sections, prepared: preparedLine(prep, items),
        status: `Filled ${n} field${n === 1 ? "" : "s"}.` + (left ? ` ${left} need${left === 1 ? "s" : ""} you.` : " Review, then submit yourself.") });
      return summary();
    } finally {
      running = false;
    }
  }

  /* ----------------------------------------------- prepared by the helper */

  // The Onward helper prepares good matches ahead of time: a resume
  // tailored to the job (or a note to use the master) and drafts for the
  // form's own questions. Asked once per page; null when the helper doesn't
  // know this job or isn't running.
  let prepared = null;
  function getPrepared() {
    if (!prepared) {
      prepared = api.runtime.sendMessage({ type: "jr:prepared", url: location.href })
        .then((r) => (r && r.found ? r : null), () => null);
    }
    return prepared;
  }

  /** The tailored PDF when one was prepared and chosen, under the name the helper gives it
   * (Firstname_Lastname_Company_Resume.pdf), else the stored resume's; without one, the stored resume. */
  function resumeFor(prep, master) {
    if (!prep || !prep.resume || !prep.resume.data) return master;
    const base = master && master.name ? master.name.replace(/\.[a-z0-9]+$/i, "") : "Resume";
    const name = /^[\w.-]{1,150}\.pdf$/i.test(prep.resume.name || "") ? prep.resume.name : base + ".pdf";
    return { name, type: "application/pdf", data: prep.resume.data, updated: Date.now(), tailored: true };
  }

  function preparedLine(prep, items) {
    if (!prep || !prep.state) return null;
    const res = items.find((i) => i.kind === "file" && i.status === "filled" && (i.source === "tailored" || i.source === "resume"));
    const parts = [];
    if (res) parts.push(res.source === "tailored" ? "attached the resume tailored for this job" : "attached your master resume");
    const n = items.filter((i) => i.source === "ai" && i.prepared).length;
    if (n) parts.push(`filled ${n} answer${n === 1 ? "" : "s"} drafted ahead`);
    return parts.length ? "Onward " + parts.join(" and ") + "." : null;
  }

  /** Fills custom questions with the answers the helper drafted ahead, marked for review like any AI answer. */
  async function usePrepared(items, answers) {
    const byQ = new Map(answers.filter((a) => a && a.question).map((a) => [normQ(a.question), a]));
    const find = (label) => {
      const k = normQ(label);
      if (byQ.has(k)) return byQ.get(k);
      // Labels on the page can carry a description after the question itself.
      for (const [q, a] of byQ) if (q.length >= 12 && k.length >= 12 && (k.startsWith(q) || q.startsWith(k))) return a;
      return null;
    };
    for (const it of items) {
      const q = it.q;
      if (it.status !== "needs" || !q.cls || q.cls.cat !== "custom" || (it.offer && !it.offerWarn) || q.single || q.kind === "file" || notForAI(q)) continue;
      const a = find(it.label);
      if (!a || W.get(q)) continue;
      const picks = a.choices && a.choices.length ? a.choices : a.answer ? [a.answer] : [];
      const typed = q.kind === "textarea" || (AI_TEXT.has(q.kind) && q.kind !== "rselect" && q.kind !== "prompt");
      const done = [];
      for (const p of q.kind === "checkbox" ? picks : picks.slice(0, 1)) {
        const r = typed ? (typeInto(q.el, p) ? { ok: true, chosen: p } : { ok: false }) : await W.set(q, [p], q.cls, {});
        if (r.ok) done.push(r.chosen);
      }
      if (!done.length || !W.get(q)) continue;
      const value = done.join(", ");
      filledByUs.set(q.el, { value: W.get(q) || value, source: "ai" });
      mark(q, "review");
      Object.assign(it, { status: "filled", value, source: "ai", prepared: true, why: "", canDraft: false });
    }
  }

  /* ------------------------------------------- application received page */

  // Greenhouse and Lever move to their own confirmation URL; Ashby and
  // Workday replace the form with a message, which only counts in a tab
  // that showed the form of a job from Onward (the background checks that).
  const CONFIRM_URL = /\/jobs\/\d+\/confirmation\b|\/job_app\/confirmation\b|^\/[^/]+\/[0-9a-f-]{36}\/thanks\b/;
  const CONFIRM_TEXT = /thanks? (you )?for (applying|your application|submitting)|application (was |has been )?(successfully )?(submitted|received)|(we've|we have) received your application|your application (was|has been) (successfully )?(submitted|received|sent)/i;
  const CONFIRM_SPOTS = "h1, h2, h3, h4, [role=alert], [role=status], [role=dialog], [class*='success'], [class*='confirmation'], [class*='thank'], [data-automation-id*='onfirm']";
  let confirmed = false;

  function confirmationShown() {
    if (CONFIRM_URL.test(location.pathname)) return "url";
    for (const n of document.querySelectorAll(CONFIRM_SPOTS)) {
      if (n.closest("#jobradar-autofill")) continue;
      const t = textOf(n);
      if (t.length <= 400 && CONFIRM_TEXT.test(t)) return "text";
    }
    return null;
  }

  async function checkConfirmation() {
    if (confirmed) return;
    const how = confirmationShown();
    if (!how) return;
    confirmed = true;
    let res;
    try {
      res = await api.runtime.sendMessage({ type: "jr:applied", url: location.href, byUrl: how === "url" });
    } catch (e) {
      res = { error: e.message };
    }
    if (!res || (!res.recorded && !res.error)) return;   // not a job from the radar
    if (!JR.panel.mount(handlers)) return;
    if (inFrame) JR.panel.update({ frame: true });
    JR.panel.update({ open: true, done: { company: res.company || "", title: res.title || "", next: res.next || null,
      error: res.error ? "Couldn't mark this application in Onward: " + res.error : "" } });
  }

  /* ------------------------------------------------------- AI answers */

  const BACKEND_NAMES = { chatgpt: "ChatGPT", "claude-code": "Claude", "openai-api": "OpenAI API", "claude-api": "Anthropic API" };
  const AI_TEXT = new Set(["text", "email", "url", "number", "search", "date", "combo", "prompt", "textarea"]);

  async function helperJob(start) {
    if (!start || start.error) throw new Error((start && start.error) || "The Onward helper didn't answer.");
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
      if (it.status !== "needs" || !q.cls || q.cls.cat !== "custom" || (it.offer && !it.offerWarn) || q.single || q.kind === "file" || notForAI(q)) continue;
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
        const r = c.kind === "textarea" || (c.kind === "text" && q.kind !== "rselect" && q.kind !== "prompt")
          ? (typeInto(q.el, p) ? { ok: true, chosen: p } : { ok: false })
          : await W.set(q, [p], q.cls, {});
        if (r.ok) done.push(r.chosen);
      }
      if (!done.length || !W.get(q)) { c.it.why = `${who}'s answer didn't fit; answer it here`; continue; }
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
    let posting = null;
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
      if (posting === null) posting = JR.sites.jobDescription(document);
      // The company and product names the answer mentions, so it's never
      // offered to another company as if it were neutral.
      const names = R.namesIn(v, info.company, info.title, posting);
      writes[k] = { q: q.label, a: v, kind: q.kind, writing: !!cls.writing, company: info.company || "", title: info.title || "",
        names, host: location.hostname, updated: Date.now() };
    }
    if (Object.keys(writes).length) await api.storage.local.set(writes);
  }

  /** Where saved answers went: kept under use:<key>, so editing the answer keeps its history. */
  async function logUse(key, saved) {
    if (!key) return;
    const info = JR.sites.jobInfo(document);
    const uk = "use:" + key.slice(4);
    try {
      const got = await api.storage.local.get([uk, "useLogSince"]);
      const list = Array.isArray(got[uk]) ? got[uk] : [];
      list.push({ company: info.company || "", title: info.title || "", host: location.hostname, date: Date.now(),
        q: saved && saved.q ? saved.q : "" });
      const out = { [uk]: list.slice(-100) };
      if (!got.useLogSince) out.useLogSince = Date.now();
      await api.storage.local.set(out);
    } catch (e) {}
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

  /** An answer the model wrote may be written again with a note ("use my Y project, not X"). */
  function canRedraft(it) {
    return !!(store.settings.helper && (it.source === "draft" || it.source === "ai") && it.q
      && (it.q.kind === "textarea" || it.q.kind === "text") && !notForAI(it.q));
  }

  /** A draft for `it`; with `note`, a rewrite of the answer now in the field, as the note asks. */
  async function draft(it, note = "") {
    it.drafting = true;
    it.prompt = "";
    const b = store.settings.draftBackend;
    const verb = note ? "Redrafting" : "Drafting";
    it.why = b === "chatgpt" || b === "claude-code" ? verb + " with your " + BACKEND_NAMES[b] + " login…"
      : verb + " with your " + (BACKEND_NAMES[b] || b) + " key…";
    JR.panel.render();
    const info = JR.sites.jobInfo(document);
    const before = note ? W.get(it.q) || "" : "";
    const payload = {
      question: it.label, context: it.q.container ? textOf(it.q.container).slice(0, 1500) : "",
      company: info.company, title: info.title, url: location.href,
      jd: JR.sites.jobDescription(document), max_chars: it.q.el.maxLength > 0 ? it.q.el.maxLength : 0,
      backend: store.settings.draftBackend, note, previous: before,
    };
    try {
      const start = await api.runtime.sendMessage({ type: "jr:draftStart", payload });
      if (!start || start.error) throw new Error((start && start.error) || "The Onward helper didn't answer.");
      if (start.prompt) {
        it.prompt = start.prompt;
        it.why = "Manual mode: paste this into ChatGPT or Claude, then paste the answer into the field.";
        return;
      }
      if (start.estimate != null) {
        it.why = "Drafting with your " + (BACKEND_NAMES[b] || b) + " key, about " + (start.estimate < 0.01 ? "under 1¢" : "$" + start.estimate.toFixed(2)) + "…";
        JR.panel.render();
      }
      const end = Date.now() + 15 * 60 * 1000;
      for (;;) {
        await sleep(2000);
        const st = await api.runtime.sendMessage({ type: "jr:draftStatus", id: start.id });
        if (st && st.state === "done") {
          const answer = String(st.result && st.result.answer || "").trim();
          if (!answer) throw new Error("The draft came back empty.");
          // Never over what the user typed: an empty field, or (for a redraft) the answer as it was asked about.
          if ((W.get(it.q) || "") !== before) {
            it.why = note ? "You changed the answer meanwhile, so the new draft wasn't put in. Here it is to copy:"
              : "You started typing, so the draft wasn't inserted.";
            it.prompt = answer;
            return;
          }
          typeInto(it.q.el, answer);
          filledByUs.set(it.q.el, { value: answer, source: "draft" });
          mark(it.q, "review");
          Object.assign(it, { status: "filled", value: answer, source: "draft", why: "", noteOpen: false, note: "" });
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
    revealSaved(it) {
      it.revealed = true;
      JR.panel.render();
    },
    async useSaved(it) {
      if (it.offerWarn && !it.revealed) return;   // a second, deliberate click is needed
      const r = await W.set(it.q, [it.offer.a], it.q.cls, {});
      if (r.ok) {
        logUse(it.offerKey, it.offer);
        filledByUs.set(it.q.el, { value: r.chosen, source: "remembered" });
        mark(it.q, "review");
        Object.assign(it, { status: "filled", value: r.chosen, source: "remembered", why: "" });
      } else it.why = r.why;
      JR.panel.render();
    },
    draft,
    canRedraft,
    /** "Replace with my resume": overwrite what the page put in this section, keeping Undo. */
    async replaceSection(sec) {
      if (running || sec.undo) return;
      running = true;
      sec.busy = true;
      JR.panel.render();
      try {
        sec.undo = await JR.history.replace(sec.diffs, filledByUs);
        for (const u of sec.undo) mark(u.d.q, "filled");
      } finally {
        sec.busy = false;
        running = false;
        JR.panel.render();
      }
    },
    async undoSection(sec) {
      if (running || !sec.undo) return;
      running = true;
      sec.busy = true;
      JR.panel.render();
      try {
        await JR.history.restore(sec.undo, filledByUs);
        sec.undo = null;
      } finally {
        sec.busy = false;
        running = false;
        JR.panel.render();
      }
    },
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
    checkConfirmation();
    if (!site) site = JR.sites.detect(document);
    const form = site && JR.sites.formRoot(document, site);
    if (!form) return;
    // Ties this tab to the job, so its confirmation page is recognized later.
    if (!prepared) load().then(() => { if (store.settings.helper) getPrepared(); });
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
