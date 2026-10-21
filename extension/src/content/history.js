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
  const SECTION_NAME = { work: "Work Experience", edu: "Education" };

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

  /** The repeating rows on the page: [{ type, n, fields: { field: question } }]. */
  function rows(qs) {
    const byKey = new Map();
    for (const q of qs) {
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
        const lines = (entry.bullets || []).filter(Boolean);
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
    if (q.kind === "text" || q.kind === "textarea" || q.kind === "number" || q.kind === "date") {
      // Workday's month and year boxes are spinbuttons that also listen for keys.
      const ok = typeInto(q.el, values[0]);
      if (q.el.getAttribute("role") === "spinbutton") keyChar(q.el, values[0].slice(-1));
      return ok ? { ok: true, chosen: values[0] } : { ok: false, why: "the page rejected the value" };
    }
    return W.set(q, values, { key: "history" }, {});
  }

  /* ------------------------------------------------------------ add rows */

  function addButton(type, doc) {
    const sel = type === "work"
      ? "[data-automation-id='workExperienceSection'], [aria-labelledby*='workExperience' i], [role='group'][aria-label*='Work Experience' i]"
      : "[data-automation-id='educationSection'], [aria-labelledby*='education' i], [role='group'][aria-label*='Education' i]";
    const name = SECTION_NAME[type].toLowerCase();
    const sections = [...doc.querySelectorAll(sel)];
    if (!sections.length) {
      // Fall back to the heading that names the section.
      for (const h of doc.querySelectorAll("h2, h3, h4, [role='heading']")) {
        if (textOf(h).toLowerCase().startsWith(name) && h.parentElement) sections.push(h.parentElement);
      }
    }
    for (const s of sections) {
      for (const b of s.querySelectorAll("button, [role='button']")) {
        const t = (textOf(b) + " " + (b.getAttribute("aria-label") || "") + " " + (b.getAttribute("data-automation-id") || "")).trim();
        if (!visible(b) || b.disabled || b.type === "submit") continue;
        if (/next|submit|continue|save|delete|remove/i.test(t)) continue;
        if (/^add\b|\badd( another)?$|add-button|^\+/i.test(t)) return b;
      }
    }
    return null;
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
    if (!found.length) return { qs, items: [], diffs: {}, extra: {} };
    const extra = {};
    if (allowAdd) {
      for (const type of ["work", "edu"]) {
        const entries = (type === "work" ? hist.work : hist.education) || [];
        let have = found.filter((r) => r.type === type).length;
        if (!have) continue;   // no such section on this step
        const btn = () => addButton(type, document);
        while (have < entries.length && btn()) {
          clickSeq(btn());
          const grew = await waitFor(() => rows(recollect()).filter((r) => r.type === type).length > have, 3000);
          if (!grew) break;
          have++;
        }
      }
      qs = recollect();
      found = rows(qs);
    }

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

  JR.history = { rows, fill, replace, restore, fitLines, SECTION_NAME };
})(globalThis);
