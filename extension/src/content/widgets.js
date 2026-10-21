/* Reading and setting each kind of question. Every set() returns
 * { ok: true, chosen } or { ok: false, why } and never presses anything
 * that submits or moves the form along. */
(function (root) {
  "use strict";
  const JR = (root.JR = root.JR || {});
  const { sleep, waitFor, visible, textOf, typeInto, clickSeq, key, keyChar, fire, nativeSetter, focusOut } = JR.dom;
  const { pickOption, normQ } = JR.rules;

  const PLACEHOLDER = /^(select|choose|please select|select one|select an option|--+|none selected)\b/i;

  function pickAny(values, texts) {
    for (const v of values) {
      const i = pickOption(v, texts);
      if (i >= 0) return i;
    }
    return -1;
  }

  /* --------------------------------------------------------------- simple */

  function getText(q) { return q.el.value || ""; }

  async function setText(q, values) {
    const v = values[0];
    const max = q.el.maxLength > 0 ? q.el.maxLength : 0;
    if (max && v.length > max) return { ok: false, why: `saved answer is longer than the ${max}-character limit` };
    return typeInto(q.el, v) ? { ok: true, chosen: v } : { ok: false, why: "the page rejected the value" };
  }

  function selectOptions(q) {
    return [...q.el.options].filter((o) => !o.disabled && o.value !== "" && !PLACEHOLDER.test(o.text.trim()));
  }

  function getSelect(q) {
    const o = q.el.selectedOptions && q.el.selectedOptions[0];
    return o && o.value !== "" && !PLACEHOLDER.test(o.text.trim()) ? o.text.trim() : "";
  }

  async function setSelect(q, values) {
    const opts = selectOptions(q);
    const i = pickAny(values, opts.map((o) => o.text));
    if (i < 0) return { ok: false, why: "no option matches your saved answer", options: opts.map((o) => o.text.trim()) };
    nativeSetter(q.el).call(q.el, opts[i].value);
    fire(q.el, "input");
    fire(q.el, "change");
    return { ok: true, chosen: opts[i].text.trim() };
  }

  /* -------------------------------------------------- radios and checkboxes */

  function getChecks(q) {
    if (q.single) return q.el.checked ? "checked" : "";
    return q.controls.map((c, i) => (c.checked ? q.options[i] : "")).filter(Boolean).join(", ");
  }

  async function setChecks(q, values) {
    if (q.single) {
      const want = values[0] === "yes" || values[0] === "checked";
      if (!["yes", "no", "checked", "unchecked"].includes(values[0])) return { ok: false, why: "unclear answer for a checkbox" };
      if (q.el.checked !== want) q.el.click();
      return { ok: q.el.checked === want, chosen: want ? "checked" : "unchecked" };
    }
    const i = pickAny(values, q.options);
    if (i < 0) return { ok: false, why: "no option matches your saved answer", options: q.options };
    const c = q.controls[i];
    if (!c.checked) c.click();
    return c.checked ? { ok: true, chosen: q.options[i] } : { ok: false, why: "the page didn't accept the choice" };
  }

  function getYesNo(q) {
    const b = q.el.querySelector("button[aria-pressed='true'], button[class*='active'], button[class*='selected']");
    return b ? textOf(b) : "";
  }

  async function setYesNo(q, values) {
    const buttons = [...q.el.querySelectorAll("button")];
    const i = pickAny(values, buttons.map((b) => b.getAttribute("data-option") || textOf(b)));
    if (i < 0) return { ok: false, why: "no option matches your saved answer" };
    clickSeq(buttons[i]);
    await sleep(60);
    return { ok: true, chosen: textOf(buttons[i]) };
  }

  /* ------------------------------------------------------------ dropdowns */

  function optionNodes(list, sel = "[role='option']") {
    return list ? [...list.querySelectorAll(sel)].filter((o) => visible(o) && o.getAttribute("aria-disabled") !== "true") : [];
  }

  function busy(list) {
    return !!list && /^(loading|searching)/i.test(list.textContent.trim());
  }

  /** The open listbox that belongs to this question. */
  function listboxOf(q) {
    const doc = q.el.ownerDocument;
    const ctl = q.el.getAttribute("aria-controls") || q.el.getAttribute("aria-owns");
    if (ctl) { const l = doc.getElementById(ctl); if (l && visible(l)) return l; }
    if (q.el.id) { const l = doc.getElementById("react-select-" + q.el.id + "-listbox"); if (l) return l; }
    const scope = q.container || q.el.parentElement;
    const inner = scope && [...scope.querySelectorAll("[role='listbox'], [class*='menu']")].find((l) => visible(l) && l.querySelector("[role='option']"));
    if (inner) return inner;
    return null;
  }

  /** The newest visible listbox anywhere: Workday renders its menus in a popup at the end of the body. */
  function popupListbox(doc) {
    const all = [...doc.querySelectorAll("[role='listbox']")].filter((l) => visible(l) && l.querySelector("[role='option']"));
    return all[all.length - 1] || null;
  }

  function closeMenu(q) {
    key(q.el, "Escape");
  }

  function getRSelect(q) {
    const box = q.container || q.el.closest(".select-shell, [class*='container']") || q.el.parentElement;
    const vals = [...box.querySelectorAll(".select__single-value, .select__multi-value__label, [class*='singleValue'], [class*='multiValue'] > div:first-child")];
    return vals.map(textOf).filter(Boolean).join(", ");
  }

  async function chooseFrom(q, values, getList, { search, allowFirst, optSel, wait = 1500 } = {}) {
    const ready = () => { const l = getList(); return l && optionNodes(l, optSel).length && !busy(l) ? l : null; };
    let list = await waitFor(ready, wait);
    let opts = optionNodes(list, optSel);
    let i = pickAny(values, opts.map(textOf));
    if (i < 0 && search) {
      typeInto(q.el, search, { blur: false });
      keyChar(q.el, search.slice(-1));
      await sleep(250);
      list = await waitFor(ready, 4000);
      opts = optionNodes(list, optSel);
      i = pickAny(values, opts.map(textOf));
    }
    if (i < 0 && allowFirst && opts.length) i = 0;
    if (i < 0) return { ok: false, why: opts.length ? "no option matches your saved answer" : "the list didn't open", options: opts.map(textOf).slice(0, 30) };
    const chosen = textOf(opts[i]);
    opts[i].scrollIntoView({ block: "nearest" });
    clickSeq(opts[i]);
    await sleep(120);
    return { ok: true, chosen };
  }

  async function setRSelect(q, values, cls) {
    const ctl = q.el.closest(".select__control") || (q.container && q.container.querySelector("[class*='control']")) || q.el;
    clickSeq(ctl);
    // A place search's top hit for "Boston, MA" is the right Boston.
    const r = await chooseFrom(q, values, () => listboxOf(q), { search: searchTerm(values[0]), allowFirst: !!(cls && cls.key === "location" && !cls.transform) });
    await sleep(80);
    if (listboxOf(q)) closeMenu(q);
    if (!r.ok) {
      if (q.el.value) typeInto(q.el, "", { blur: false });
      closeMenu(q);
      return r;
    }
    const now = getRSelect(q);
    return now ? { ok: true, chosen: now } : { ok: false, why: "the page didn't accept the choice" };
  }

  function searchTerm(v) {
    if (!v || v === "yes" || v === "no" || v === JR.rules.DECLINE) return "";
    return String(v).slice(0, 40);
  }

  async function setCombo(q, values, cls) {
    const v = values[0];
    if (!v || v === JR.rules.DECLINE) return { ok: false, why: "no saved answer" };
    const doc = q.el.ownerDocument;
    clickSeq(q.el);
    typeInto(q.el, v, { blur: false });
    keyChar(q.el, v.slice(-1));
    const isPlace = cls && cls.key === "location";
    const getList = q.suggest
      ? () => doc.querySelector(q.suggest.list)
      : () => listboxOf(q) || popupListbox(doc);
    // Suggestions come from the network, so give them longer to arrive.
    const r = await chooseFrom(q, values, getList, { allowFirst: isPlace, optSel: q.suggest && q.suggest.option, wait: 4000 });
    if (!r.ok) {
      closeMenu(q);
      // The typed text stays; some location fields accept free text.
      return { ok: false, why: "pick your answer from the list", partial: true };
    }
    focusOut(q.el);
    await sleep(100);
    return q.el.value ? { ok: true, chosen: q.el.value } : { ok: false, why: "pick your answer from the list" };
  }

  /* -------------------------------------------------------------- Workday */

  function getListButton(q) {
    const t = textOf(q.el);
    return t && !PLACEHOLDER.test(t) ? t : "";
  }

  async function setListButton(q, values) {
    clickSeq(q.el);
    const doc = q.el.ownerDocument;
    const r = await chooseFrom(q, values, () => listboxOf(q) || popupListbox(doc));
    if (!r.ok) { key(doc.activeElement || q.el, "Escape"); return r; }
    await sleep(150);
    return { ok: true, chosen: getListButton(q) || r.chosen };
  }

  function getPrompt(q) {
    const box = q.box || q.el.parentElement;
    return [...box.querySelectorAll("[data-automation-id='selectedItem'], [data-automation-id='promptSelectionLabel']")].map(textOf).join(", ");
  }

  async function setPrompt(q, values) {
    const doc = q.el.ownerDocument;
    const term = searchTerm(values[0]);
    if (!term) return { ok: false, why: "no saved answer" };
    typeInto(q.el, term, { blur: false });
    key(q.el, "Enter");
    const opts = await waitFor(() => {
      const o = [...doc.querySelectorAll("[data-automation-id='promptOption'], [data-automation-id='promptLeafNode'] [role='option']")].filter(visible);
      return o.length ? o : null;
    }, 4000);
    if (!opts) { key(q.el, "Escape"); return { ok: false, why: "no option matches your saved answer" }; }
    const i = pickAny(values, opts.map(textOf));
    if (i < 0) { key(q.el, "Escape"); return { ok: false, why: "no option matches your saved answer", options: opts.map(textOf).slice(0, 30) }; }
    const chosen = textOf(opts[i]);
    clickSeq(opts[i]);
    await sleep(200);
    key(q.el, "Escape");
    return { ok: true, chosen };
  }

  /* ------------------------------------------------------------------ file */

  function getFile(q) {
    if (q.el.files && q.el.files.length) return q.el.files[0].name;
    // Greenhouse and Ashby swap the input for a filename chip once attached.
    const box = q.container || q.el.parentElement;
    const chip = box && box.querySelector(".file-upload__filename, [class*='filename'], [data-automation-id='file-upload-item']");
    return chip ? textOf(chip) : "";
  }

  function b64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  async function setFile(q, values, cls, ctx) {
    const r = ctx && ctx.resume;
    if (!r || !r.data) return { ok: false, why: "choose your resume in the extension options" };
    const accept = (q.el.accept || "").toLowerCase();
    const ext = (r.name.match(/\.[a-z0-9]+$/i) || [""])[0].toLowerCase();
    if (accept && ext && !accept.split(",").some((a) => a.trim() === ext || a.trim() === r.type || a.trim() === "*/*" || (a.trim().endsWith("/*") && r.type.startsWith(a.trim().slice(0, -1))))) {
      return { ok: false, why: `this form doesn't take ${ext} files` };
    }
    const file = new File([b64ToBytes(r.data)], r.name, { type: r.type || "application/pdf", lastModified: r.updated || Date.now() });
    const dt = new DataTransfer();
    dt.items.add(file);
    q.el.files = dt.files;
    fire(q.el, "input");
    fire(q.el, "change");
    await sleep(100);
    return { ok: true, chosen: r.name };
  }

  /* -------------------------------------------------------------- dispatch */

  const KINDS = {
    text: [getText, setText], email: [getText, setText], tel: [getText, setText], url: [getText, setText],
    number: [getText, setText], search: [getText, setText], date: [getText, setText], textarea: [getText, setText],
    select: [getSelect, setSelect], radio: [getChecks, setChecks], checkbox: [getChecks, setChecks],
    yesno: [getYesNo, setYesNo], rselect: [getRSelect, setRSelect], combo: [getText, setCombo],
    listbutton: [getListButton, setListButton], prompt: [getPrompt, setPrompt], file: [getFile, setFile],
  };

  function get(q) {
    const k = KINDS[q.kind];
    try { return k ? String(k[0](q) || "").trim() : ""; } catch (e) { return ""; }
  }

  /** The choices a question offers, opening and closing its menu if it has one. [] for free text. */
  async function options(q) {
    try {
      if (q.kind === "radio" || q.kind === "checkbox") return q.single ? [] : q.options.slice();
      if (q.kind === "select") return selectOptions(q).map((o) => o.text.trim());
      if (q.kind === "yesno") return [...q.el.querySelectorAll("button")].map(textOf);
      if (q.kind === "rselect" || q.kind === "listbutton") {
        const doc = q.el.ownerDocument;
        const opener = q.kind === "rselect" ? (q.el.closest(".select__control") || q.el) : q.el;
        clickSeq(opener);
        const get = q.kind === "rselect" ? () => listboxOf(q) : () => listboxOf(q) || popupListbox(doc);
        const list = await waitFor(() => { const l = get(); return l && optionNodes(l).length && !busy(l) ? l : null; }, 1500);
        const texts = optionNodes(list).map(textOf);
        key(q.kind === "rselect" ? q.el : (doc.activeElement || q.el), "Escape");
        if (q.kind === "rselect" && get()) clickSeq(opener);   // Greenhouse toggles its menu on mouseup
        await sleep(80);
        return texts;
      }
    } catch (e) {}
    return [];
  }

  /** values: the saved answer first, then acceptable alternatives. */
  async function set(q, values, cls, ctx) {
    const k = KINDS[q.kind];
    if (!k) return { ok: false, why: "unsupported field" };
    try { return await k[1](q, values, cls, ctx); } catch (e) { return { ok: false, why: "couldn't fill: " + e.message }; }
  }

  JR.widgets = { get, set, options, normQ };
})(globalThis);
