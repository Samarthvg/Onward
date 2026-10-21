/* DOM helpers for the content scripts: visibility, labels, and setting
 * values in a way React-controlled forms notice. */
(function (root) {
  "use strict";
  const JR = (root.JR = root.JR || {});

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function waitFor(fn, timeout = 2000, every = 50) {
    const end = Date.now() + timeout;
    for (;;) {
      const v = fn();
      if (v) return v;
      if (Date.now() > end) return null;
      await sleep(every);
    }
  }

  function visible(el) {
    if (!el || !el.isConnected) return false;
    if (!el.getClientRects().length) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none";
  }

  /** Visible text of a node, without required markers or screen-reader-only extras. */
  function textOf(node) {
    if (!node) return "";
    const c = node.cloneNode(true);
    c.querySelectorAll("svg, style, script, [aria-hidden='true'], .required, .asterisk, abbr[title='required'], input, select, textarea, button")
      .forEach((n) => n.remove());
    return JR.rules.clean(c.textContent);
  }

  function nativeSetter(el) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    return Object.getOwnPropertyDescriptor(proto, "value").set;
  }

  function fire(el, type, init) {
    el.dispatchEvent(new Event(type, Object.assign({ bubbles: true, cancelable: true }, init)));
  }

  /* React keeps its own copy of an input's value; setting it through the
     native setter and firing input is what makes React (Greenhouse, Ashby,
     Workday) see the change. Workday validates when focus leaves a field, and
     a window without OS focus fires no focus events, so stand in for them. */
  function focusIn(el) {
    let got = false;
    const mark = () => { got = true; };
    el.addEventListener("focus", mark, { once: true });
    try { el.focus({ preventScroll: true }); } catch (e) {}
    el.removeEventListener("focus", mark);
    if (!got) {
      el.dispatchEvent(new FocusEvent("focus", { bubbles: false }));
      el.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    }
  }

  function focusOut(el) {
    let left = false;
    const mark = () => { left = true; };
    el.addEventListener("focusout", mark, { once: true });
    if (el.ownerDocument.activeElement === el) el.blur();
    el.removeEventListener("focusout", mark);
    if (!left) {
      el.dispatchEvent(new FocusEvent("blur", { bubbles: false }));
      el.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    }
  }

  function typeInto(el, v, { blur = true } = {}) {
    focusIn(el);
    nativeSetter(el).call(el, v);
    fire(el, "input");
    fire(el, "change");
    if (blur) focusOut(el);
    return el.value === v;
  }

  /** The full sequence a real click produces; some widgets listen to mouseup or pointerdown only. */
  function clickSeq(el) {
    const r = el.getBoundingClientRect();
    const o = { bubbles: true, cancelable: true, composed: true, button: 0, buttons: 1, view: el.ownerDocument.defaultView,
      clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
    const P = typeof PointerEvent === "function" ? PointerEvent : MouseEvent;
    el.dispatchEvent(new P("pointerover", o));
    el.dispatchEvent(new MouseEvent("mouseover", o));
    el.dispatchEvent(new P("pointerdown", Object.assign({ pointerType: "mouse", isPrimary: true }, o)));
    el.dispatchEvent(new MouseEvent("mousedown", o));
    el.dispatchEvent(new P("pointerup", Object.assign({ pointerType: "mouse", isPrimary: true }, o, { buttons: 0 })));
    el.dispatchEvent(new MouseEvent("mouseup", Object.assign({}, o, { buttons: 0 })));
    el.dispatchEvent(new MouseEvent("click", Object.assign({}, o, { buttons: 0 })));
  }

  function key(el, k) {
    const codes = { Enter: 13, Escape: 27, ArrowDown: 40, ArrowUp: 38, Tab: 9 };
    const o = { key: k, code: k, keyCode: codes[k] || 0, which: codes[k] || 0, bubbles: true, cancelable: true };
    el.dispatchEvent(new KeyboardEvent("keydown", o));
    el.dispatchEvent(new KeyboardEvent("keyup", o));
  }

  /** Key events for a typed character: some autocompletes search on keyup, not input. */
  function keyChar(el, ch) {
    const o = { key: ch, bubbles: true, cancelable: true };
    for (const t of ["keydown", "keypress", "keyup"]) el.dispatchEvent(new KeyboardEvent(t, o));
  }

  function labelFor(el) {
    const doc = el.ownerDocument;
    const nodes = new Set();
    if (el.id) for (const l of doc.querySelectorAll('label[for="' + CSS.escape(el.id) + '"]')) nodes.add(l);
    const lb = el.getAttribute("aria-labelledby");
    if (lb) for (const id of lb.split(/\s+/)) { const n = doc.getElementById(id); if (n && !n.contains(el)) nodes.add(n); }
    return [...nodes].map(textOf).filter(Boolean).join(" ");
  }

  function wrappingLabel(el) {
    const l = el.closest("label");
    return l ? textOf(l) : "";
  }

  JR.dom = { sleep, waitFor, visible, textOf, typeInto, clickSeq, key, keyChar, fire, nativeSetter, focusIn, focusOut, labelFor, wrappingLabel };
})(globalThis);
