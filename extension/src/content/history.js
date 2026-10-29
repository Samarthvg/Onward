/* Work Experience and Education sections, filled from the master resume
 * field by field with no AI: job title, company, location, dates, the
 * "I currently work here" box and a role description made of that job's
 * own bullets; school, degree, field of study, GPA and years.
 *
 * Rows already filled (Workday's own resume parser does this, sometimes
 * badly) are matched to resume entries by company or school name, the rest
 * by order. Values that differ from the resume are never overwritten on
 * their own: the panel offers "Replace with my resume" for that section,
 * with Undo. On Workday, rows are added with the section's own Add button
 * when the resume has more entries than the form; nothing that submits or
 * moves the form along is ever pressed.
 */
(function (root) {
  "use strict";
  const JR = (root.JR = root.JR || {});
  const { historyField, normQ, alternatives } = JR.rules;
  const { textOf, typeInto, sleep, waitFor, clickSeq, visible, keyChar } = JR.dom;
  const W = JR.widgets;

  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const EDU_FIELDS = new Set(["school", "degree", "field", "gpa"]);
  const WORK_FIELDS = new Set(["title", "company", "description", "current"]);
  const SECTION_NAME = { work: "Work Experience", edu: "Education", lang: "Languages", skills: "Skills", web: "Websites", cert: "Certifications", proj: "Projects" };

  /** Which repeating row a control sits in: { type: "work"|"edu", n } or null. */
  function rowOf(q, field) {
    const ids = [q.el.id, q.el.getAttribute("data-automation-id"), q.el.name].filter(Boolean).join(" ");
    let m = ids.match(/\b(workExperience|education)-(\d+)--/i);
    if (m) return { type: /^work/i.test(m[1]) ? "work" : "edu", n: +m[2] };
    const wrap = q.el.closest("[data-automation-id^='workExperience-'], [data-automation-id^='education-']");
    const aid = wrap && wrap.getAttribute("data-automation-id");
    if (aid && /-\d+$/.test(aid)) return { type: aid.startsWith("work") ? "work" : "edu", n: +aid.split("-").pop() };
    // Greenhouse-style repeaters: "company-name--0", "school--0", "start-date-month--1".
    m = (q.el.id || q.el.name || "").match(/^(.+?)--(\d+)$/);
    if (m) {
      const sec = normQ(q.section);
      const type = EDU_FIELDS.has(field) ? "edu" : WORK_FIELDS.has(field) ? "work"
        : /educat|school|academic/.test(sec) ? "edu" : /employ|experience|work/.test(sec) ? "work" : null;
      if (type) return { type, n: +m[2] };
    }
    return null;
  }

  /** A Languages row: Workday's "language-2--language", or a wrapper "language-2". */
  function langRowOf(q) {
    const ids = [q.el.id, q.el.getAttribute("data-automation-id"), q.el.name].filter(Boolean).join(" ");
    const m = ids.match(/\blanguages?-(\d+)--/i);
    if (m) return +m[1];
    const wrap = q.el.closest("[data-automation-id^='language-'], [data-automation-id^='languages-']");
    const aid = wrap && wrap.getAttribute("data-automation-id");
    return aid && /-\d+$/.test(aid) ? +aid.split("-").pop() : null;
  }

  /** What a control in a Languages row asks: the language, "I am fluent", or a proficiency. */
  function langField(q) {
    const t = normQ(q.label + " " + q.attrs.replace(/([a-z])([A-Z])/g, "$1 $2"));
    if (q.kind === "checkbox" && /fluent|native/.test(t)) return "fluent";
    const p = t.match(/\b(reading|speaking|writing|comprehension|overall|listening)\b/);
    if (p) return "level:" + p[1];
    if (/proficien|\blevel\b/.test(t)) return "level:overall";
    if (/\blanguage\b/.test(t)) return "language";
    return null;
  }

  // Websites and Certifications rows: Workday's "webAddress-2--url",
  // "certification-1--issuedDate-dateSectionYear-input".
  const REPEATERS = {
    web: { id: /\bwebAddress-(\d+)--/i, wrap: "[data-automation-id^='webAddress-']" },
    cert: { id: /\bcertifications?-(\d+)--/i, wrap: "[data-automation-id^='certification-'], [data-automation-id^='certifications-']" },
    proj: { id: /\bprojects?-(\d+)--/i, wrap: "[data-automation-id^='project-'], [data-automation-id^='projects-']" },
  };

  function repeaterOf(q) {
    const ids = [q.el.id, q.el.getAttribute("data-automation-id"), q.el.name].filter(Boolean).join(" ");
    for (const [type, r] of Object.entries(REPEATERS)) {
      const m = ids.match(r.id);
      if (m) return { type, n: +m[1] };
      const wrap = q.el.closest(r.wrap);
      const aid = wrap && wrap.getAttribute("data-automation-id");
      if (aid && /-\d+$/.test(aid)) return { type, n: +aid.split("-").pop() };
    }
    return null;
  }

  function repeaterField(type, q) {
    const t = normQ(q.label + " " + q.attrs.replace(/([a-z])([A-Z])/g, "$1 $2"));
    if (type === "web") return /url|website|web address|link/.test(t) ? "url" : null;
    if (type === "proj") {
      if (q.kind === "checkbox") return /current|ongoing|present|in progress/.test(t) ? "current" : null;
      if (/description|summary|details|responsibilit/.test(t)) return "description";
      if (/url|link|website|repo/.test(t)) return "url";
      const part = /month/.test(t) ? "Month" : /year/.test(t) ? "Year" : "Date";
      if (/\b(start|from|begin)/.test(t)) return "start" + part;
      if (/\b(end|to|until|completed?)\b/.test(t)) return "end" + part;
      if (/\brole\b|position/.test(t)) return "role";
      if (/tools|technolog|skills used|\btech\b/.test(t)) return "tech";
      if (/project|name|title/.test(t)) return "name";
      return null;
    }
    if (/number|\bid\b/.test(t)) return "number";
    if (/expir/.test(t)) return "expiry";
    if (/issue|obtained|earned|date/.test(t)) return /year/.test(t) ? "issuedYear" : /month/.test(t) ? "issuedMonth" : "issuedDate";
    if (/certif|license|name|title/.test(t)) return "name";
    return null;
  }

  /** The repeating rows on the page: [{ type, n, fields: { field: question } }]. */
  function rows(qs) {
    const byKey = new Map();
    for (const q of qs) {
      const rep = repeaterOf(q);
      if (rep) {
        const field = repeaterField(rep.type, q);
        if (!field) continue;
        const key = rep.type + ":" + rep.n;
        if (!byKey.has(key)) byKey.set(key, { type: rep.type, n: rep.n, fields: {} });
        if (!byKey.get(key).fields[field]) byKey.get(key).fields[field] = q;
        q.history = { type: rep.type, n: rep.n, field };
        continue;
      }
      const ln = langRowOf(q);
      if (ln != null) {
        const field = langField(q);
        if (!field) continue;
        const key = "lang:" + ln;
        if (!byKey.has(key)) byKey.set(key, { type: "lang", n: ln, fields: {} });
        if (!byKey.get(key).fields[field]) byKey.get(key).fields[field] = q;
        q.history = { type: "lang", n: ln, field };
        continue;
      }
      const field = historyField(q.label, q.attrs);
      if (!field) continue;
      const r = rowOf(q, field);
      if (!r) continue;
      const key = r.type + ":" + r.n;
      if (!byKey.has(key)) byKey.set(key, { type: r.type, n: r.n, fields: {} });
      const row = byKey.get(key);
      if (!row.fields[field]) row.fields[field] = q;
      q.history = { type: r.type, n: r.n, field };
    }
    return [...byKey.values()].sort((a, b) => (a.type === b.type ? a.n - b.n : a.type < b.type ? 1 : -1));
  }

  /* ------------------------------------------------------------- values */

  function fitLines(lines, max) {
    let text = lines.join("\n");
    if (!max || text.length <= max) return text;
    // Whole bullets first; only a first bullet that is itself too long is cut.
    const kept = [];
    for (const l of lines) {
      const next = kept.concat(l).join("\n");
      if (next.length > max) break;
      kept.push(l);
    }
    if (kept.length) return kept.join("\n");
    text = lines[0].slice(0, max);
    return text.replace(/\s+\S*$/, "").trim();
  }

  function monthValues(m) {
    if (!m) return [];
    return [String(m).padStart(2, "0"), String(m), MONTHS[m - 1], MONTHS[m - 1].slice(0, 3)];
  }

  /** What the resume says for one field: the value first, then spellings forms use. [] when it says nothing. */
  function wanted(field, entry, q) {
    const s = entry.start || null;
    const e = entry.current ? null : entry.end || null;
    const date = (d) => (d ? (d.month ? String(d.month).padStart(2, "0") + "/" : "") + d.year : "");
    switch (field) {
      case "title": return [entry.title].filter(Boolean);
      case "company": return [entry.company].filter(Boolean);
      case "location": return [entry.location].filter(Boolean);
      case "current": return entry.current == null ? [] : [entry.current ? "yes" : "no"];
      case "description": {
        // The description edited in the options, else the resume's bullets; one point per line.
        const lines = (entry.description != null ? String(entry.description).split("\n") : entry.bullets || [])
          .map((l) => l.trim()).filter(Boolean);
        return lines.length ? [fitLines(lines, q.el.maxLength > 0 ? q.el.maxLength : 0)] : [];
      }
      case "startMonth": return monthValues(s && s.month);
      case "startYear": return s ? [String(s.year)] : [];
      case "endMonth": return monthValues(e && e.month);
      case "endYear": return e ? [String(e.year)] : [];
      case "startDate": return s ? [date(s)] : [];
      case "endDate": return e ? [date(e)] : [];
      case "school": return [entry.school, entry.school_full].filter(Boolean);
      case "degree": return entry.degree ? alternatives({ key: "degree" }, entry.degree) : [];
      case "field": return [entry.field].filter(Boolean);
      case "gpa": return [entry.gpa].filter(Boolean);
    }
    return [];
  }

  function same(cur, values, q) {
    if (!values.length) return true;
    const c = normQ(cur);
    if (q.kind === "checkbox" && q.single) return (cur === "checked") === (values[0] === "yes");
    if (/^\d+$/.test(c) && /^\d+$/.test(normQ(values[0]))) return parseInt(c, 10) === parseInt(normQ(values[0]), 10);
    return values.some((v) => normQ(v) === c) || JR.rules.pickOption(values[0], [cur]) === 0;
  }

  /* -------------------------------------------------------------- setting */

  async function put(q, values) {
    if (q.kind === "checkbox" && q.single) {
      const want = values[0] === "yes";
      if (q.el.checked !== want) q.el.click();
      return { ok: q.el.checked === want, chosen: want ? "checked" : "unchecked" };
    }
    if (q.el.getAttribute("role") === "spinbutton") return W.set(q, values, { key: "history" }, {});
    if (q.kind === "text" || q.kind === "textarea" || q.kind === "number" || q.kind === "date") {
      const ok = typeInto(q.el, values[0]);
      return ok ? { ok: true, chosen: values[0] } : { ok: false, why: "the page rejected the value" };
    }
    return W.set(q, values, { key: "history" }, {});
  }

  /* ------------------------------------------------------------ add rows */

  // A section's group: Workday labels it by its heading's id, "Work-Experience-section".
  const SECTION_SEL = {
    work: "[data-automation-id='workExperienceSection'], [aria-labelledby*='workExperience' i], [aria-labelledby*='work-experience' i], [role='group'][aria-label*='Work Experience' i]",
    edu: "[data-automation-id='educationSection'], [aria-labelledby*='education' i], [role='group'][aria-label*='Education' i]",
    lang: "[data-automation-id='languageSection'], [data-automation-id='languagesSection'], [aria-labelledby*='language' i], [role='group'][aria-label*='Language' i]",
    web: "[data-automation-id='websiteSection'], [data-automation-id='websitesSection'], [aria-labelledby*='website' i], [role='group'][aria-label*='Website' i]",
    cert: "[data-automation-id='certificationSection'], [data-automation-id='certificationsSection'], [aria-labelledby*='certification' i], [role='group'][aria-label*='Certification' i]",
    proj: "[data-automation-id='projectSection'], [data-automation-id='projectsSection'], [aria-labelledby*='project' i], [role='group'][aria-label*='Project' i]",
  };
  // Other names a section goes by. Some Workday sites call their websites section "LinkedIn".
  const SECTION_ALIASES = { web: ["website", "links", "linkedin"] };

  /** Workday's own Add and Add Another buttons, which (like any button without a type) are submit buttons. */
  function isAdd(b) {
    if (!visible(b) || b.disabled) return false;
    if (b.closest("[data-automation-id='pageFooter']") || /next|footer/i.test(b.getAttribute("data-automation-id") || "")) return false;
    const t = (textOf(b) + " " + (b.getAttribute("aria-label") || "") + " " + (b.getAttribute("data-automation-id") || "")).trim();
    if (/next|submit|continue|save|delete|remove|upload|select files/i.test(t)) return false;
    if (b.type === "submit" && b.getAttribute("data-automation-id") !== "add-button" && !/^add( another)?$/i.test(textOf(b))) return false;
    return /^add\b|\badd( another)?$|add-button|^\+/i.test(t);
  }

  function addButton(type, doc) {
    const sel = SECTION_SEL[type];
    const name = SECTION_NAME[type].toLowerCase();
    const names = [name, ...(SECTION_ALIASES[type] || [])];
    const addIn = (s) => [...s.querySelectorAll("button, [role='button']")].find(isAdd) || null;
    for (const s of doc.querySelectorAll(sel)) { const b = addIn(s); if (b) return b; }
    // Otherwise climb from the heading that names the section, stopping before a
    // wrapper that also holds another section's heading.
    const isHeading = (h, n) => textOf(h).toLowerCase().replace(/\s*\(.*$/, "").trim() === n;
    const others = Object.entries(SECTION_NAME).filter(([k]) => k !== type).map(([, x]) => x.toLowerCase())
      .concat(["social network urls", "resume/cv", "resume", "my information", "attachments"].filter((x) => !names.includes(x)));
    for (const h of doc.querySelectorAll("h2, h3, h4, h5, [role='heading'], legend")) {
      if (!names.some((n) => isHeading(h, n))) continue;
      // A "LinkedIn" heading only counts as a websites section when the section says it takes websites.
      if (type === "web" && isHeading(h, "linkedin") && !/website/i.test(textOf(h.parentElement))) continue;
      for (let n = h.parentElement, i = 0; n && i < 6; n = n.parentElement, i++) {
        const foreign = [...n.querySelectorAll("h2, h3, h4, h5, [role='heading'], legend")].some((x) => x !== h && others.some((o) => isHeading(x, o)));
        if (foreign) break;
        const b = addIn(n);
        if (b) return b;
      }
    }
    return null;
  }

  /** Presses a section's Add button until it has `want` rows (or the button stops adding). Returns how many were added. */
  async function addRows(type, want, have, recollect) {
    let added = 0;
    for (let guard = 0; have < want && guard < 20; guard++) {
      const b = addButton(type, document);
      if (!b) break;
      b.scrollIntoView({ block: "center" });
      clickSeq(b);
      const before = have;
      const grew = await waitFor(() => { const n = rows(recollect()).filter((r) => r.type === type).length; return n > before ? n : 0; }, 4000);
      if (!grew) break;
      have = grew;
      added++;
      await sleep(250);
    }
    return added;
  }

  /* ----------------------------------------------------------------- fill */

  /**
   * Fills the page's Work Experience and Education rows from hist (the
   * helper's /api/resume/entries). Returns the panel items for those fields,
   * the differing values per section, and the questions (re-read if rows
   * were added).
   */
  async function fill(qs, hist, { recollect, allowAdd, ours }) {
    let found = rows(qs);
    const extra = {};
    if (allowAdd) {
      for (const type of ["work", "edu"]) {
        const entries = (type === "work" ? hist.work : hist.education) || [];
        // Workday shows an empty section as just a heading and an Add button.
        const added = await addRows(type, entries.length, found.filter((r) => r.type === type).length, recollect);
        if (added) found = rows(recollect());
      }
      qs = recollect();
      found = rows(qs);
    }
    if (!found.some((r) => r.type === "work" || r.type === "edu")) return { qs, items: [], diffs: {}, extra };

    const items = [];
    const diffs = { work: [], edu: [] };
    for (const type of ["work", "edu"]) {
      const entries = ((type === "work" ? hist.work : hist.education) || []).slice();
      const mine = found.filter((r) => r.type === type);
      if (!mine.length) continue;
      const used = new Set();
      const plan = new Map();
      // Rows the page already filled keep the entry they name.
      for (const r of mine) {
        const nameQ = r.fields[type === "work" ? "company" : "school"];
        const name = nameQ ? normQ(W.get(nameQ)) : "";
        if (!name) continue;
        const i = entries.findIndex((e, k) => {
          if (used.has(k)) return false;
          const names = type === "work" ? [e.company] : [e.school, e.school_full];
          return names.some((x) => { const n = normQ(x); return n && (n.includes(name) || name.includes(n)); });
        });
        if (i >= 0) { used.add(i); plan.set(r, entries[i]); }
      }
      let next = 0;
      for (const r of mine) {
        if (plan.has(r)) continue;
        while (used.has(next)) next++;
        if (next < entries.length) { used.add(next); plan.set(r, entries[next]); }
      }
      extra[type] = entries.length - used.size;

      for (const r of mine) {
        const entry = plan.get(r);
        for (const [field, q] of Object.entries(r.fields)) {
          const base = { q, label: `${SECTION_NAME[type]} ${r.n}: ${q.label || field}`, kind: q.kind, history: type };
          if (!entry) { items.push(Object.assign(base, { status: "needs", why: "no matching entry in your resume" })); continue; }
          const values = wanted(field, entry, q);
          if (field.startsWith("end") && entry.current) { items.push(Object.assign(base, { status: "had", value: "(current job)" })); continue; }
          if (!values.length) continue;
          const cur = W.get(q);
          // An unticked "I currently work here" box is the resume's answer when the job ended.
          const unticked = q.kind === "checkbox" && q.single && !cur;
          if (unticked && values[0] === "no") { items.push(Object.assign(base, { status: "had", value: "not current" })); continue; }
          if (!cur) {
            const res = await put(q, values);
            if (res.ok) {
              ours.set(q.el, { value: W.get(q) || res.chosen, source: "resume" });
              items.push(Object.assign(base, { status: "filled", value: res.chosen, source: "resume" }));
            } else items.push(Object.assign(base, { status: "needs", why: res.why || "couldn't fill this from your resume" }));
            continue;
          }
          if (same(cur, values, q)) { items.push(Object.assign(base, { status: "had", value: cur })); continue; }
          const canReplace = q.kind !== "prompt";   // a multiselect would add, not replace
          const item = Object.assign(base, { status: "had", value: cur, differs: true, resume: values[0],
            why: canReplace ? "differs from your resume" : "differs from your resume; change it yourself" });
          items.push(item);
          diffs[type].push({ q, field, old: cur, values, canReplace, item });
        }
      }
    }
    return { qs, items, diffs, extra };
  }

  /* --------------------------------------------------- skills and languages */

  const LEVEL_WORDS = {
    native: ["Native", "Native or bilingual", "Native or Bilingual Proficiency", "Fluent", "Expert", "Advanced"],
    fluent: ["Fluent", "Full professional proficiency", "Professional", "Advanced", "Expert"],
    advanced: ["Advanced", "Professional", "Fluent"],
    intermediate: ["Intermediate", "Limited working proficiency", "Conversational", "Moderate"],
    beginner: ["Beginner", "Elementary", "Basic", "Elementary proficiency"],
  };

  /** The skills box: Workday's "Type to Add Skills" search, or a plain "Skills" text field. */
  function skillsQuestion(qs) {
    // A box that only takes a list of skills; "Describe your technical skills" is an essay.
    const LIST = /^(type to add |add |enter |list )?(your )?(technical |key |relevant |top )?skills?( set)?( list)?$/;
    return qs.find((q) => (!q.history || q.history.type === "skills") && ["prompt", "text", "textarea", "combo"].includes(q.kind)
      && (LIST.test(normQ(q.label)) || (q.kind === "prompt" && /(^|[\s-])skills?([\s-]|$)/i.test(q.attrs)))) || null;
  }

  /** Adds skills one at a time: search, pick the site's name for it ("Java (Programming Language)" for Java),
   * or take Workday's own pick of the only match, and check the pill appeared. */
  async function addSkills(q, skills, progress) {
    const R = JR.rules;
    const added = [], missing = [];
    const have = (skill) => W.promptChips(q).some((c) => R.sameSkill(skill, c));
    const list = R.splitSkills(skills);
    for (const [i, skill] of list.entries()) {
      if (have(skill)) { added.push(skill); continue; }
      progress(`Adding skills ${i + 1}/${list.length}…`);
      const match = (texts) => texts.findIndex((t) => R.sameSkill(skill, t));
      let ok = false;
      // The resume's name first, then its singular ("RAG pipeline") or other names ("Amazon Web Services").
      for (const term of R.skillSearchTerms(skill)) {
        const r = await W.pickPrompt(q, [skill], term, { wait: 3000, match });
        // Only a pill in the box counts: what the list showed is not proof.
        if (await waitFor(() => have(skill), r.ok ? 1500 : 0)) { ok = true; break; }
      }
      (ok ? added : missing).push(skill);
      await sleep(100);
    }
    await W.closePrompt(q);
    return { added, missing };
  }

  /** A box that just asks for your projects ("Projects", "Relevant projects"), not an essay about one. */
  function projectsQuestion(qs) {
    const LIST = /^(relevant |key |personal |academic |side |technical |notable )?projects?( you have worked on| you ve worked on)?$|^(list|share) (your|any) (relevant )?projects$/;
    return qs.find((q) => (!q.history || q.history.type === "proj") && (q.kind === "textarea" || q.kind === "text")
      && LIST.test(normQ(q.label))) || null;
  }

  /** Projects written out for one box: a heading line per project, its points, its link; whole projects only. */
  function projectsText(projs, max, oneLine = false) {
    const blocks = projs.map((p) => {
      const when = [p.start && p.start.year ? (p.start.month ? MONTHS[p.start.month - 1].slice(0, 3) + " " : "") + p.start.year : "",
        p.current ? "present" : p.end && p.end.year ? (p.end.month ? MONTHS[p.end.month - 1].slice(0, 3) + " " : "") + p.end.year : ""].filter(Boolean).join(" – ");
      const head = [p.name + (p.tech ? ` (${p.tech})` : ""), p.role, when].filter(Boolean).join(", ");
      const points = (p.description != null ? String(p.description).split("\n") : p.bullets || []).map((l) => l.trim()).filter(Boolean);
      // A one-line box drops line breaks, so its points run on as sentences.
      return oneLine ? [head + ":", ...points, p.link || ""].filter(Boolean).join(" ")
        : [head, ...points, p.link || ""].filter(Boolean).join("\n");
    });
    const sep = oneLine ? "; " : "\n\n";
    const kept = [];
    for (const b of blocks) {
      if (max && kept.concat(b).join(sep).length > max) break;
      kept.push(b);
    }
    return kept.length ? kept.join(sep) : (max ? blocks[0].slice(0, max) : "");
  }

  /**
   * Skills from the master resume and languages from the profile.
   * Languages rows are added with the section's Add button on Workday.
   */
  async function fillExtras(qs, { skills, skillsLeft = [], languages, websites, certifications, projects, recollect, allowAdd, ours, progress }) {
    const items = [];
    const sq = skillsQuestion(qs);
    // Resume skills that don't fit this posting, named so they can be added by hand.
    const leftOut = skillsLeft.length ? `left out as unrelated to this job: ${skillsLeft.join(", ")}` : "";
    if (sq && !(skills && skills.length) && skillsLeft.length) {
      sq.history = { type: "skills", field: "skills" };
      items.push({ q: sq, label: sq.label || "Skills", kind: sq.kind, history: "skills", status: "needs",
        why: `none of your resume's skills fit this posting; ${leftOut}` });
    } else if (sq && !(skills && skills.length) && !W.get(sq)) {
      sq.history = { type: "skills", field: "skills" };
      items.push({ q: sq, label: sq.label || "Skills", kind: sq.kind, history: "skills", status: "needs",
        why: "start the Onward app so your resume's skills can be added here" });
    }
    if (sq && skills && skills.length) {
      sq.history = { type: "skills", field: "skills" };
      const base = { q: sq, label: sq.label || "Skills", kind: sq.kind, history: "skills" };
      if (sq.kind === "prompt") {
        const { added, missing } = await addSkills(sq, skills.slice(0, 50), progress);
        if (added.length) ours.set(sq.el, { value: W.get(sq), source: "resume" });
        items.push(Object.assign(base, added.length
          ? { status: "filled", value: added.join(", "), source: "resume", why: leftOut }
          : { status: "needs", why: "none of your resume's skills were in this site's list" }));
        if (added.length && missing.length) {
          items.push({ q: sq, label: "Skills not in this site's list", kind: sq.kind, history: "skills", status: "needs",
            why: missing.join(", ") });
        }
      } else if (!W.get(sq)) {
        const max = sq.el.maxLength > 0 ? sq.el.maxLength : 0;
        let text = skills.join(", ");
        if (max && text.length > max) text = text.slice(0, max).replace(/,[^,]*$/, "");
        if (typeInto(sq.el, text)) {
          ours.set(sq.el, { value: text, source: "resume" });
          items.push(Object.assign(base, { status: "filled", value: text, source: "resume", why: leftOut }));
        }
      }
    }

    const langs = JR.rules.languageList(languages);
    if (langs.length) {
      let lr = rows(qs).filter((r) => r.type === "lang");
      if (allowAdd && lr.length < langs.length) {
        await addRows("lang", langs.length, lr.length, recollect);
        qs = recollect();
        lr = rows(qs).filter((r) => r.type === "lang");
      }
      for (const [i, r] of lr.entries()) {
        const l = langs[i];
        if (!l) break;
        const levelKey = normQ(l.level).split(" ")[0];
        const levels = l.level ? [l.level, ...(LEVEL_WORDS[levelKey] || [])] : [];
        for (const [field, q] of Object.entries(r.fields)) {
          const base = { q, label: `Language ${i + 1}: ${q.label || field}`, kind: q.kind, history: "lang" };
          if (W.get(q) && !(q.kind === "checkbox" && q.single)) { items.push(Object.assign(base, { status: "had", value: W.get(q) })); continue; }
          let values;
          if (field === "language") values = [l.name];
          else if (field === "fluent") values = [/native|fluent|bilingual|mother/i.test(l.level) ? "yes" : "no"];
          else values = levels;
          if (!values.length) {
            items.push(Object.assign(base, { status: "needs", why: `add a level for ${l.name} under Languages in the Onward Apply kit, e.g. "${l.name} (Fluent)"` }));
            continue;
          }
          if (field === "fluent" && values[0] === "no") continue;
          const res = await put(q, values);
          if (res.ok) {
            ours.set(q.el, { value: W.get(q) || res.chosen, source: "profile" });
            items.push(Object.assign(base, { status: "filled", value: res.chosen, source: "profile" }));
          } else items.push(Object.assign(base, { status: "needs", why: res.why || "couldn't pick this" }));
        }
      }
    }
    // Projects: rows where the form has them (Add pressed for more), else one "Projects" box.
    const projs = (projects || []).filter((p) => p && p.name);
    if (projs.length) {
      let pr = rows(qs).filter((r) => r.type === "proj");
      if (allowAdd && pr.length < projs.length && (pr.length || addButton("proj", document))) {
        await addRows("proj", projs.length, pr.length, recollect);
        qs = recollect();
        pr = rows(qs).filter((r) => r.type === "proj");
      }
      for (const [i, r] of pr.entries()) {
        const p = projs[i];
        if (!p) break;
        for (const [field, q] of Object.entries(r.fields)) {
          const base = { q, label: `Projects ${i + 1}: ${q.label || field}`, kind: q.kind, history: "proj" };
          const values = field === "name" ? [p.name] : field === "role" ? [p.role].filter(Boolean) : field === "url" ? [p.link].filter(Boolean)
            : field === "tech" ? [p.tech].filter(Boolean) : wanted(field, p, q);
          if (!values.length) continue;
          if (field.startsWith("end") && p.current) continue;
          if (q.kind === "checkbox" && q.single && values[0] === "no") continue;
          if (W.get(q) && !(q.kind === "checkbox" && q.single)) { items.push(Object.assign(base, { status: "had", value: W.get(q) })); continue; }
          const res = await put(q, values);
          if (res.ok) {
            ours.set(q.el, { value: W.get(q) || res.chosen, source: "resume" });
            items.push(Object.assign(base, { status: "filled", value: res.chosen, source: "resume" }));
          } else items.push(Object.assign(base, { status: "needs", why: res.why || "couldn't fill this" }));
        }
      }
      const box = !pr.length && projectsQuestion(qs);
      if (box && !W.get(box)) {
        box.history = { type: "proj", field: "list" };
        const text = projectsText(projs, box.el.maxLength > 0 ? box.el.maxLength : 0, box.kind !== "textarea");
        if (text && typeInto(box.el, text)) {
          ours.set(box.el, { value: text, source: "resume" });
          items.push({ q: box, label: box.label || "Projects", kind: box.kind, history: "proj", status: "filled", value: text, source: "resume" });
        }
      }
    }

    // Websites from the profile, Certifications from the resume's own section.
    for (const [type, list] of [["web", websites || []], ["cert", certifications || []]]) {
      if (!list.length) continue;
      let rs = rows(qs).filter((r) => r.type === type);
      // Links the page already holds aren't added twice.
      const have = new Set(rs.map((r) => r.fields.url || r.fields.name).filter(Boolean).map((q) => normQ(W.get(q))).filter(Boolean));
      const todo = list.filter((x) => !have.has(normQ(type === "web" ? x : x.name)));
      const empty = rs.filter((r) => { const q = r.fields.url || r.fields.name; return q && !W.get(q); }).length;
      if (allowAdd && todo.length > empty) {
        await addRows(type, rs.length + todo.length - empty, rs.length, recollect);
        qs = recollect();
        rs = rows(qs).filter((r) => r.type === type);
      }
      const queue = todo.slice();
      for (const r of rs) {
        const main = r.fields.url || r.fields.name;
        if (!main || W.get(main) || !queue.length) continue;
        const x = queue.shift();
        const values = type === "web" ? { url: [x] } : {
          name: [x.name], issuedYear: x.issued ? [String(x.issued.year)] : [],
          issuedMonth: x.issued ? monthValues(x.issued.month) : [],
          issuedDate: x.issued ? [(x.issued.month ? String(x.issued.month).padStart(2, "0") + "/" : "") + x.issued.year] : [],
        };
        for (const [field, q] of Object.entries(r.fields)) {
          const v = values[field];
          if (!v || !v.length || W.get(q)) continue;
          const base = { q, label: `${SECTION_NAME[type]} ${r.n}: ${q.label || field}`, kind: q.kind, history: type };
          const res = await put(q, v);
          if (res.ok) {
            ours.set(q.el, { value: W.get(q) || res.chosen, source: type === "web" ? "profile" : "resume" });
            items.push(Object.assign(base, { status: "filled", value: res.chosen, source: type === "web" ? "profile" : "resume" }));
          } else items.push(Object.assign(base, { status: "needs", why: res.why || "couldn't fill this" }));
        }
      }
      if (queue.length && (rs.length || addButton(type, document))) {
        items.push({ q: qs[0], label: SECTION_NAME[type], kind: "text", history: type, status: "needs",
          why: `couldn't add ${queue.length === 1 ? "a row" : "rows"} for ${queue.map((x) => (type === "web" ? x : x.name)).join(", ")}` });
      }
    }
    return { qs, items };
  }

  /** Overwrites a section's differing values with the resume's; returns what Undo needs. */
  async function replace(list, ours) {
    const undo = [];
    for (const d of list) {
      if (!d.canReplace) continue;
      const before = W.get(d.q);
      const r = await put(d.q, d.values);
      if (r.ok) {
        undo.push({ d, old: before });
        ours.set(d.q.el, { value: W.get(d.q) || r.chosen, source: "resume" });
        Object.assign(d.item, { status: "filled", value: r.chosen, source: "resume", why: "" });
      } else d.item.why = r.why || "couldn't replace this one";
    }
    return undo;
  }

  async function restore(undo, ours) {
    for (const { d, old } of undo.slice().reverse()) {
      if (d.q.kind === "checkbox" && d.q.single) await put(d.q, [old === "checked" ? "yes" : "no"]);
      else await put(d.q, [old]);
      ours.delete(d.q.el);
      Object.assign(d.item, { status: "had", value: old, source: undefined, why: "differs from your resume" });
    }
  }

  JR.history = { rows, fill, fillExtras, skillsQuestion, projectsQuestion, projectsText, addButton, replace, restore, fitLines, SECTION_NAME };
})(globalThis);
