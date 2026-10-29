/* Reading and setting each kind of question. Every set() returns
 * { ok: true, chosen } or { ok: false, why } and never presses anything
 * that submits or moves the form along. */
(function (root) {
  "use strict";
  const JR = (root.JR = root.JR || {});
  const { sleep, waitFor, visible, textOf, typeInto, clickSeq, key, keyChar, fire, nativeSetter, focusIn, focusOut } = JR.dom;
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

  function getText(q) {
    if (q.el.value || q.el.getAttribute("role") !== "spinbutton") return q.el.value || "";
    // Workday's date spinbuttons keep the number in aria-valuenow and show it beside the box.
    const now = q.el.getAttribute("aria-valuenow") || q.el.getAttribute("aria-valuetext") || "";
    if (/^\d+$/.test(now) && now !== "0") return now;
    const shown = q.el.parentElement && q.el.parentElement.querySelector("[data-automation-id$='-display']");
    const t = shown ? textOf(shown) : "";
    return /^\d+$/.test(t) ? t : "";
  }

  /* Workday's month, day and year boxes are spinbuttons driven by key presses.
   * Live Workday shows what's typed in a display beside the box and keeps the
   * number in aria-valuenow, so the input itself can read empty afterwards. */
  async function setSpin(q, v) {
    const el = q.el;
    const took = () => parseInt(getText(q), 10) === parseInt(v, 10);
    focusIn(el);
    for (const ch of String(v)) keyChar(el, ch);
    // A box that ignores key presses takes the value the usual way.
    if (!(await waitFor(took, 300))) typeInto(el, String(v), { blur: false });
    focusOut(el);
    return (await waitFor(took, 800)) ? { ok: true, chosen: v } : { ok: false, why: "the page rejected the value" };
  }

  async function setText(q, values) {
    const v = values[0];
    if (q.el.getAttribute("role") === "spinbutton") return setSpin(q, v);
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
    const inner = scope && [...scope.querySelectorAll("[role='listbox'], [class*='menu']")].find((l) => visible(l) && !isPills(l) && l.querySelector("[role='option']"));
    if (inner) return inner;
    return null;
  }

  /** Workday shows a search box's picks as a listbox of pills; that is an answer, not a menu. */
  function isPills(l) {
    return l.matches("[data-automation-id='selectedItemList']") || !!l.closest("[data-automation-id='multiselectInputContainer']");
  }

  /** The newest visible listbox anywhere: Workday renders its menus in a popup at the end of the body. */
  function popupListbox(doc) {
    const all = [...doc.querySelectorAll("[role='listbox']")].filter((l) => visible(l) && !isPills(l) && l.querySelector("[role='option']"));
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

  /* Workday's search-and-pick box ("prompt"): an input in a multiselectInputContainer
   * whose choices open in a popup list (activeListContainer) outside the form.
   * Enter searches; a search with exactly one match is picked at once, with no
   * list shown. Picks show as selectedItem pills inside the box. A choice can be
   * a category that opens a list of its own ("Social Network" > "LinkedIn"). */
  const NOT_A_CHOICE = /^(no items|no results|no matches|loading|searching)\b/i;

  function promptBox(q) {
    return q.box || q.el.closest("[data-automation-id='multiselectInputContainer'], [data-automation-id='multiSelectContainer']") || q.el.parentElement;
  }

  function promptChips(q) {
    return [...promptBox(q).querySelectorAll("[data-automation-id='selectedItem']")].map(textOf).filter(Boolean);
  }

  function getPrompt(q) {
    const chips = promptChips(q);
    if (chips.length) return chips.join(", ");
    return [...promptBox(q).querySelectorAll("[data-automation-id='promptSelectionLabel']")].map(textOf).filter(Boolean).join(", ");
  }

  function promptList(doc) {
    const all = [...doc.querySelectorAll("[data-automation-id='activeListContainer']")].filter(visible);
    return all[all.length - 1] || null;
  }

  /** The open popup's choices, without placeholders ("No Items.") or another field's picked pills. */
  function promptResults(doc) {
    const list = promptList(doc);
    if (!list) return [];
    const nodes = [...list.querySelectorAll("[role='option']")];
    const items = (nodes.length ? nodes : [...list.querySelectorAll("[data-automation-id='promptOption']")])
      .filter((o) => visible(o) && !o.closest("[data-automation-id='selectedItemList']"));
    return items.map((el) => {
      const opt = el.matches("[data-automation-id='promptOption']") ? el : el.querySelector("[data-automation-id='promptOption']");
      return { el, text: textOf(opt || el) };
    }).filter((o) => o.text && !NOT_A_CHOICE.test(o.text));
  }

  /** Closes an open popup. Workday ignores a scripted Escape, so click outside it, or press the mobile dialog's close button. */
  async function closePrompt(q) {
    const doc = q.el.ownerDocument;
    if (!promptList(doc)) return;
    key(q.el, "Escape");
    await sleep(60);
    if (!promptList(doc)) return;
    const dialog = [...doc.querySelectorAll("[data-automation-id='popUpDialog'], [role='dialog']")].find((d) => visible(d) && d.contains(promptList(doc)));
    const x = dialog && [...dialog.querySelectorAll("button")].find((b) => visible(b) && /^(close|done|cancel)$/i.test(b.getAttribute("aria-label") || textOf(b)));
    clickSeq(x || doc.body);
    focusOut(q.el);
    await waitFor(() => !promptList(doc), 800);
  }

  /** A choice already ticked in a multi-select list; clicking it again would untick it. Workday
   * marks the tick on the leaf ("Checked") and in the label ("Java checked"); its aria-selected
   * only marks the highlighted row. */
  function isTicked(el) {
    const leaf = el.matches("[data-automation-checked]") ? el : el.querySelector("[data-automation-checked]");
    if (leaf) return leaf.getAttribute("data-automation-checked") === "Checked";
    const label = el.getAttribute("aria-label") || "";
    return / checked$/i.test(label) && !/not checked$/i.test(label);
  }

  /** Searches for `term` and picks the choice matching one of `values`, or the first that `match(texts)`
   * points to. With exact, only an exact match counts. */
  async function pickPrompt(q, values, term, { exact = false, wait = 5000, match = null } = {}) {
    const doc = q.el.ownerDocument;
    const before = promptChips(q);
    const matchIn = (texts) => {
      if (match) return match(texts);
      const k = texts.findIndex((t) => values.some((v) => normQ(v) === normQ(t)));
      if (k >= 0 || exact) return k;
      const i = pickAny(values, texts);
      if (i >= 0) return i;
      // "Computer Science (General)" is Computer Science.
      const bare = (t) => normQ(String(t).replace(/\s*\([^)]*\)\s*/g, " "));
      return texts.findIndex((t) => values.some((v) => bare(v) === bare(t)));
    };
    const added = () => promptChips(q).filter((c) => !before.includes(c));
    clickSeq(q.el);
    typeInto(q.el, term, { blur: false });
    key(q.el, "Enter");
    const end = Date.now() + wait;
    let seen = [], opened = 0, sig = "", since = 0;
    while (Date.now() < end) {
      await sleep(150);
      const got = added();
      if (got.length) {
        // Workday picks a search's only match by itself.
        if (matchIn(got) >= 0) { await closePrompt(q); return { ok: true, chosen: got.join(", ") }; }
        break;
      }
      const res = promptResults(doc);
      if (!res.length) continue;
      seen = res.map((o) => o.text);
      const i = matchIn(seen);
      if (i < 0) {
        // Results may still be loading; once they hold still without a match, stop looking.
        const now = seen.join("|");
        if (now !== sig) { sig = now; since = Date.now(); } else if (Date.now() - since > 1200) break;
        continue;
      }
      if (isTicked(res[i].el)) { await closePrompt(q); return { ok: true, chosen: res[i].text, already: true }; }
      const row = res[i].el;
      const moved = () => added().length || isTicked(row)
        || (promptList(doc) && promptResults(doc).map((o) => o.text).join("|") !== seen.join("|")) ? true : null;
      // Workday listens on a part inside the row: the tick box, the leaf, or the label.
      // Click the innermost first, then outward, stopping at the first that takes.
      const targets = [row.querySelector("input[type='checkbox']"), row.querySelector("[data-automation-id='promptOption']"),
        row.querySelector("[data-automation-id='promptLeafNode']"), row].filter((t, k, all) => t && all.indexOf(t) === k);
      let changed = null;
      for (const t of targets) {
        if (!t.isConnected || moved()) { changed = moved(); break; }   // a slow tick from the last click: don't click it off
        t.scrollIntoView({ block: "nearest" });
        clickSeq(t);
        changed = await waitFor(moved, t === row ? 2500 : 1200);
        if (changed) break;
      }
      if (!changed && row.isConnected) {
        // Last resort, the keyboard: Workday's list takes Enter on the highlighted row.
        row.focus && row.focus();
        key(row, "Enter");
        changed = await waitFor(moved, 900);
      }
      if (added().length || (res[i].el.isConnected && isTicked(res[i].el))) {
        // A multi-select may show the pill a moment after the tick.
        await waitFor(() => added().length, 1000);
        await closePrompt(q);
        return { ok: true, chosen: added().join(", ") || res[i].text };
      }
      // A category opened its own list: look for the answer in there.
      if (!changed || ++opened > 3) break;
    }
    await closePrompt(q);
    if (typeof q.el.value === "string" && q.el.value) typeInto(q.el, "", { blur: false });
    return { ok: false, why: seen.length ? "no option matches your saved answer" : "the search found nothing", options: seen.slice(0, 30) };
  }

  async function setPrompt(q, values) {
    const term = searchTerm(values[0]);
    if (!term) return { ok: false, why: "no saved answer" };
    const have = promptChips(q);
    if (have.length && pickAny(values, have) >= 0) return { ok: true, chosen: have.join(", ") };
    const r = await pickPrompt(q, values, term);
    if (r.ok || !/\(/.test(term)) return r;
    // "United States of America (+1)" is found by its name alone.
    return pickPrompt(q, values, term.replace(/\s*\(.*$/, ""));
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
    if (!r || !r.data) return { ok: false, why: "add your master resume in Onward" };
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

  JR.widgets = { get, set, options, normQ, pickPrompt, promptChips, closePrompt };
})(globalThis);
