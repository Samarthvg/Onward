/* Finds the questions on an application form.
 *
 * A question is one thing the applicant answers: a text box, a dropdown, a
 * radio group, a file input. Each gets the label that belongs to it alone:
 * a wrapper's label is only trusted when that wrapper holds no other
 * question, so a section heading or a neighbour's label is never mistaken
 * for this field's.
 */
(function (root) {
  "use strict";
  const JR = (root.JR = root.JR || {});
  const { visible, textOf, labelFor, wrappingLabel } = JR.dom;

  const SITES = {
    greenhouse: {
      host: /(^|\.)greenhouse\.io$/,
      form: "#application-form, #application_form, form[action*='applications'], .application--form, #main_fields",
      containers: ".field-wrapper, .text-input-wrapper, .input-wrapper, .select, .checkbox, fieldset, .field, .question, li",
      labels: "label, legend, .label, .upload-label, [id$='-label']",
    },
    lever: {
      host: /(^|\.)lever\.co$/,
      form: "#application-form, form.application-form, .application-form",
      containers: "li.application-question, .application-question, .custom-question",
      labels: ".application-label .text, .application-label, label",
      // Location suggestions are plain divs, not ARIA options, and the field
      // empties itself on blur unless one of them was picked.
      suggest: { ".location-input": { list: ".dropdown-results", option: ".dropdown-location" } },
    },
    ashby: {
      host: /(^|\.)ashbyhq\.com$/,
      form: ".ashby-application-form-container, form",
      containers: ".ashby-application-form-field-entry, fieldset",
      labels: ".ashby-application-form-question-title, legend, label",
    },
    workday: {
      host: /(^|\.)myworkday(jobs|site)\.com$/,
      form: "[data-automation-id='applyFlowPage'], [data-automation-id^='applyFlow'], [data-automation-id='wizardPageContent'], form",
      containers: "[data-automation-id^='formField-'], fieldset, [role='group'], [role='radiogroup']",
      labels: "legend, label, [data-automation-id='richText']",
      workday: true,
    },
  };

  function detect(doc) {
    const host = doc.location.hostname;
    for (const [name, s] of Object.entries(SITES)) if (s.host.test(host)) return Object.assign({ name }, s);
    // Unknown host (tests, custom domains): recognize by markup.
    if (doc.querySelector("[data-automation-id^='applyFlow'], [data-automation-id='legalNameSection_firstName']")) return Object.assign({ name: "workday" }, SITES.workday);
    if (doc.querySelector(".ashby-application-form-container")) return Object.assign({ name: "ashby" }, SITES.ashby);
    if (doc.querySelector("li.application-question")) return Object.assign({ name: "lever" }, SITES.lever);
    if (doc.querySelector("#application-form, #application_form")) return Object.assign({ name: "greenhouse" }, SITES.greenhouse);
    return null;
  }

  function formRoot(doc, site) {
    if (!site) return null;
    for (const sel of site.form.split(",")) {
      const el = doc.querySelector(sel.trim());
      if (el && el.querySelector("input, textarea, select, button[aria-haspopup='listbox']")) return el;
    }
    return null;
  }

  const CONTROL = "input, textarea, select, button[aria-haspopup='listbox']";
  const SKIP_TYPES = new Set(["hidden", "submit", "button", "reset", "image", "password"]);

  function usable(el) {
    if (el.disabled) return false;
    if (el.closest(".iti__dropdown-content, .ashby-application-form-autofill-pane, [data-jr-ignore]")) return false;
    if (el.tagName === "INPUT") {
      const t = (el.type || "text").toLowerCase();
      if (SKIP_TYPES.has(t)) return false;
      if (t === "file") return true;   // visually hidden behind an "Attach" button
      if (el.getAttribute("aria-hidden") === "true") return false;
      // Workday keeps a hidden text input beside each listbox button; the button is the question.
      const prev = el.previousElementSibling;
      if (t === "text" && !el.id && !el.name && prev && prev.matches("button[aria-haspopup='listbox']")) return false;
      if (t === "radio" || t === "checkbox") return visible(el) || visible(el.closest("label")) || visible(el.parentElement);
    }
    return visible(el);
  }

  function kindOf(el) {
    const tag = el.tagName;
    if (tag === "SELECT") return "select";
    if (tag === "TEXTAREA") return "textarea";
    if (tag === "BUTTON") return "listbutton";
    const t = (el.type || "text").toLowerCase();
    if (t === "file" || t === "radio" || t === "checkbox") return t;
    if (el.closest(".select__control") || /(^| )select__input( |$)/.test(el.className)) return "rselect";
    // Workday's search-and-pick box: a "selectinput" that only becomes "searchBox" while its list is open.
    if (el.getAttribute("data-automation-id") === "searchBox" || el.getAttribute("data-uxi-widget-type") === "selectinput"
        || el.closest("[data-automation-id='multiselectInputContainer']")) return "prompt";
    if (el.getAttribute("role") === "combobox") return "combo";
    if (t === "date") return "date";
    return ["email", "tel", "url", "number", "search"].includes(t) ? t : "text";
  }

  function attrsOf(el) {
    const wrap = el.closest("[data-automation-id^='formField-']");
    return [el.id, el.name, el.getAttribute("autocomplete"), el.getAttribute("data-automation-id"),
      wrap && wrap.getAttribute("data-automation-id").replace(/^formField-/, "")].filter(Boolean).join(" ");
  }

  /** Groups controls into questions. */
  function collect(doc, site) {
    const form = formRoot(doc, site);
    if (!form) return [];
    const qs = [];
    const byRadioName = new Map();
    const owner = new Map();   // control -> question

    const add = (q) => { qs.push(q); for (const c of q.controls) owner.set(c, q); return q; };

    for (const el of form.querySelectorAll(CONTROL)) {
      if (owner.has(el) || !usable(el)) continue;
      const yesno = el.closest(".ashby-application-form-input-yesno");
      if (yesno) {
        if (!qs.some((q) => q.box === yesno)) {
          add({ kind: "yesno", el: yesno, box: yesno, controls: [...yesno.querySelectorAll("button, input")] });
        }
        continue;
      }
      const kind = kindOf(el);
      if (kind === "radio") {
        const key = el.name || null;
        const group = key && byRadioName.get(key);
        if (group) { group.controls.push(el); owner.set(el, group); continue; }
        const q = add({ kind: "radio", el, controls: [el] });
        if (key) byRadioName.set(key, q);
        continue;
      }
      if (kind === "checkbox") {
        // The nearest wrapper holding several checkboxes: Workday wraps each
        // option of "Please check one of the boxes below" in its own field.
        const GROUP = "fieldset, [role='group'], ul, [data-automation-id^='formField-'], .checkbox-group";
        let box = el.closest(GROUP);
        let peers = box ? [...box.querySelectorAll("input[type='checkbox']")].filter(usable) : [el];
        for (let up = box && box.parentElement && box.parentElement.closest(GROUP); peers.length < 2 && up && form.contains(up);
          up = up.parentElement && up.parentElement.closest(GROUP)) {
          // Only a wrapper of checkboxes alone: one holding other fields is a whole section.
          if (up.querySelector("input:not([type='checkbox']):not([type='hidden']), select, textarea, button[aria-haspopup='listbox']")) break;
          const more = [...up.querySelectorAll("input[type='checkbox']")].filter(usable);
          if (more.length > 1) { box = up; peers = more; }
        }
        if (peers.length > 1) { add({ kind: "checkbox", el, box, controls: peers, multi: true }); continue; }
        add({ kind: "checkbox", el, controls: [el], single: true });
        continue;
      }
      if (kind === "prompt") {
        const box = el.closest("[data-automation-id='multiselectInputContainer']") || el.parentElement;
        add({ kind, el, box, controls: [el] });
        continue;
      }
      const sug = site.suggest && Object.keys(site.suggest).find((sel) => el.matches(sel));
      if (sug) { add({ kind: "combo", el, controls: [el], suggest: site.suggest[sug] }); continue; }
      add({ kind, el, controls: [el] });
    }

    const headings = [...form.querySelectorAll("h1, h2, h3, h4, legend, [role='heading'], .section-header, [data-automation-id='pageHeaderText']")];
    // Workday's step title sits outside the form body.
    const title = doc.querySelector("[data-automation-id='applyFlowPage'] h2, [data-automation-id='pageHeaderText']");

    for (const q of qs) {
      q.container = containerOf(q, form, site, owner);
      q.label = labelOf(q, site);
      q.attrs = attrsOf(q.el);
      q.section = sectionOf(q, headings) || (title ? textOf(title) : "");
      q.options = q.kind === "radio" || q.kind === "checkbox" ? q.controls.map(optionText) : null;
    }
    return qs.filter((q) => q.label || q.attrs);
  }

  /** The nearest site wrapper that holds this question and no other. */
  function containerOf(q, form, site, owner) {
    for (let n = q.el.parentElement; n && n !== form.parentElement; n = n.parentElement) {
      if (!n.matches(site.containers)) continue;
      let alone = true;
      for (const c of n.querySelectorAll(CONTROL)) {
        const o = owner.get(c);
        if (o && o !== q) { alone = false; break; }
      }
      if (alone) return n;
      break;   // wider wrappers only get wider
    }
    return null;
  }

  function isOptionLabel(l, q) {
    if (l.querySelector("input, select, textarea")) return true;
    const f = l.htmlFor && l.ownerDocument.getElementById(l.htmlFor);
    return !!(f && q.controls.length > 1 && q.controls.includes(f));
  }

  function labelOf(q, site) {
    const group = q.kind === "radio" || (q.kind === "checkbox" && q.multi) || q.kind === "yesno";
    // A file input's own label is the "Attach" button text; the wrapper names it.
    const direct = group ? "" : labelFor(q.el);
    if (direct && q.kind !== "file") return direct;
    if (q.container) {
      for (const l of q.container.querySelectorAll(site.labels)) {
        if (isOptionLabel(l, q)) continue;
        const t = textOf(l);
        if (t) return t;
      }
    }
    if (group) {
      const fs = q.el.closest("fieldset");
      const lg = fs && fs.querySelector("legend");
      if (lg && (!q.container || q.container.contains(fs) || fs.contains(q.container))) return textOf(lg);
      const rg = q.el.closest("[role='radiogroup'], [role='group']");
      if (rg) { const t = labelFor(rg) || rg.getAttribute("aria-label"); if (t) return t; }
    }
    if (direct) return direct;
    if (q.single) return wrappingLabel(q.el) || q.el.getAttribute("aria-label") || "";
    const aria = q.el.getAttribute("aria-label");
    if (aria) return aria;
    if (!group) {
      const w = wrappingLabel(q.el);
      if (w) return w;
      const prev = q.el.previousElementSibling;
      if (prev && !prev.querySelector("input, textarea, select") && textOf(prev).length < 80) return textOf(prev);
    }
    return q.el.getAttribute("placeholder") && !/type here|start typing|select/i.test(q.el.placeholder) ? q.el.placeholder : "";
  }

  function optionText(el) {
    return wrappingLabel(el) || labelFor(el) || el.getAttribute("aria-label") || el.value || "";
  }

  function sectionOf(q, headings) {
    let best = null;
    for (const h of headings) {
      if (h.contains(q.el)) continue;
      if (h.compareDocumentPosition(q.el) & Node.DOCUMENT_POSITION_FOLLOWING) best = h; else break;
    }
    return best ? textOf(best) : "";
  }

  /** Company and job title, for drafting answers. */
  function jobInfo(doc) {
    const t = doc.title || "";
    let m = t.match(/application for (.+?) at (.+)$/i);   // Greenhouse
    if (m) return { title: m[1].trim(), company: m[2].trim() };
    const h = doc.querySelector(".posting-headline h2, h1.ashby-job-posting-heading, [data-automation-id='jobPostingHeader'], h1");
    const title = h ? textOf(h) : "";
    m = t.match(/^(.+?)\s+[-–|]\s+(.+)$/);   // Lever: "Company - Title"
    const company = m ? m[1].trim() : (doc.querySelector("img[alt]") || {}).alt || "";
    return { title: title || (m ? m[2].trim() : t), company };
  }

  function jobDescription(doc) {
    const el = doc.querySelector(".job__description, #content .body, .posting-page .section-wrapper, [data-automation-id='jobPostingDescription'], .ashby-job-posting-left-pane, #content");
    return el ? el.innerText.slice(0, 20000) : "";
  }

  /**
   * The posting's text, also on pages that don't show it: Workday's application steps
   * leave the description behind, so it's read from Workday's own job API for this URL
   * (/{site}/job/{location}/{title_id}/apply -> /wday/cxs/{tenant}/{site}/job/...).
   */
  const postingCache = new Map();
  async function postingText(doc) {
    const onPage = jobDescription(doc);
    if (onPage.trim().length >= 200) return onPage;
    const loc = doc.location || {};
    if (!/\.myworkdayjobs\.com$/i.test(loc.hostname || "")) return onPage;
    const parts = String(loc.pathname || "").split("/").filter(Boolean);
    const j = parts.indexOf("job");
    if (j < 1) return onPage;
    const stop = parts.indexOf("apply", j);
    const url = `/wday/cxs/${loc.hostname.split(".")[0]}/${parts[j - 1]}/${parts.slice(j, stop > j ? stop : undefined).join("/")}`;
    if (!postingCache.has(url)) {
      postingCache.set(url, fetch(url, { headers: { accept: "application/json" }, credentials: "include" })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          const html = d && d.jobPostingInfo && d.jobPostingInfo.jobDescription;
          if (!html) return "";
          // A parsed document isn't laid out, so its text runs "Functions:Develop" together without a break per tag.
          const spaced = String(html).replace(/<(br|\/p|\/li|\/div|\/h\d|\/ul|\/ol)\b[^>]*>/gi, "$&\n").replace(/></g, "> <");
          return (new DOMParser().parseFromString(spaced, "text/html").body.textContent || "").slice(0, 20000);
        })
        .catch(() => ""));
    }
    return (await postingCache.get(url)) || onPage;
  }

  JR.sites = { SITES, detect, formRoot, collect, jobInfo, jobDescription, postingText };
})(globalThis);
