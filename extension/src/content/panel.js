/* The in-page panel: what was filled, what still needs the user, and
 * buttons for saved answers and drafts. Lives in a shadow root so the
 * page's styles can't reach it and the form scanner never sees it. */
(function (root) {
  "use strict";
  const JR = (root.JR = root.JR || {});

  const CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; }
    .wrap { position: fixed; right: 16px; bottom: 16px; z-index: 2147483646; font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; color: #1d1d1b; }
    .wrap.top { top: 16px; bottom: auto; }
    .launch { border: 0; border-radius: 999px; padding: 9px 14px; background: #2f6b3a; color: #fff; font: 600 13px system-ui, sans-serif; cursor: pointer; box-shadow: 0 4px 14px rgba(0,0,0,.22); }
    .launch:hover { background: #255830; }
    .panel { width: min(380px, calc(100vw - 32px)); max-height: min(70vh, 640px); display: flex; flex-direction: column; background: #fbfaf7; border: 1px solid #d9d6cc; border-radius: 10px; box-shadow: 0 10px 30px rgba(0,0,0,.18); overflow: hidden; }
    header { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-bottom: 1px solid #e6e3da; background: #f3f1ea; }
    header b { flex: 1; font-size: 13px; }
    button { font: inherit; cursor: pointer; border-radius: 6px; border: 1px solid #c9c5b9; background: #fff; color: #1d1d1b; padding: 4px 9px; }
    button:hover { background: #f0eee7; }
    button.primary { background: #2f6b3a; border-color: #2f6b3a; color: #fff; font-weight: 600; }
    button.primary:hover { background: #255830; }
    button[disabled] { opacity: .55; cursor: default; }
    .x { border: 0; background: none; font-size: 16px; line-height: 1; padding: 2px 6px; color: #6b675c; }
    .body { overflow: auto; padding: 8px 12px 10px; }
    .status { margin: 2px 0 8px; color: #4a473f; }
    h3 { font-size: 11px; letter-spacing: .04em; text-transform: uppercase; color: #6b675c; margin: 10px 0 4px; font-weight: 600; }
    ul { list-style: none; margin: 0; padding: 0; }
    li { padding: 6px 0; border-top: 1px solid #ece9e0; }
    li:first-child { border-top: 0; }
    .q { display: block; color: #1d1d1b; cursor: pointer; overflow-wrap: anywhere; }
    .q:hover { text-decoration: underline; }
    .v { color: #4a473f; overflow-wrap: anywhere; }
    .why { color: #8a5a00; }
    .tag { display: inline-block; font-size: 11px; padding: 0 6px; border-radius: 999px; background: #e7efe3; color: #2f5a2a; margin-left: 4px; }
    .tag.amber { background: #fbefd5; color: #7a4b00; }
    .row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
    .note { color: #6b675c; font-size: 12px; margin-top: 10px; }
    textarea { width: 100%; min-height: 90px; font: 12px/1.4 ui-monospace, monospace; margin-top: 4px; }
    details > summary { cursor: pointer; color: #4a473f; margin-top: 10px; }
    a { color: #2f5a8a; }
    .prep { margin: 0 0 8px; padding: 6px 8px; border-radius: 6px; background: #eef3ec; color: #2f5a2a; }
    .sec { margin: 0 0 8px; padding: 6px 8px; border-radius: 6px; background: #fbefd5; color: #5e3b00; }
    .next { margin-top: 8px; padding: 8px 10px; border: 1px solid #d9d6cc; border-radius: 8px; display: grid; gap: 6px; }
    a.btn { display: inline-block; text-decoration: none; border-radius: 6px; padding: 5px 10px; background: #2f6b3a; color: #fff; font-weight: 600; width: fit-content; }
    a.btn:hover { background: #255830; }
    @media (prefers-color-scheme: dark) {
      .wrap { color: #ecebe6; }
      .panel { background: #22221f; border-color: #44423b; }
      header { background: #2a2a26; border-color: #3a3933; }
      button { background: #2f2f2b; color: #ecebe6; border-color: #4d4b43; }
      button:hover { background: #3a3a35; }
      .x { background: none; color: #b5b2a7; }
      .q { color: #ecebe6; } .v, .status, details > summary { color: #c4c1b6; } .note, h3 { color: #a29f94; }
      li { border-color: #3a3933; } .why { color: #f0b85a; }
      .tag { background: #2f4429; color: #cfe6c6; } .tag.amber { background: #4a3a17; color: #f5d797; }
      textarea { background: #1b1b19; color: #ecebe6; border-color: #4d4b43; }
      a { color: #8fb6e8; }
      .prep { background: #2a3626; color: #cfe6c6; } .sec { background: #4a3a17; color: #f5d797; } .next { border-color: #44423b; }
      a.btn { color: #fff; }
    }
  `;

  let host = null, shadow = null, handlers = null;
  const view = { open: false, busy: false, status: "", items: [], step: "", prepared: null, done: null };

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k === "text") n.textContent = v;
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k != null && k !== false) n.append(k);
    return n;
  }

  const remounts = [];

  function mount(h) {
    handlers = h;
    if (host && host.isConnected) return true;
    // A page that keeps removing the panel gets left alone rather than
    // fought with; the toolbar button still works there.
    const now = Date.now();
    while (remounts.length && now - remounts[0] > 30000) remounts.shift();
    if (remounts.length >= 5) return false;
    remounts.push(now);
    if (!host) {
      host = document.createElement("div");
      host.id = "jobradar-autofill";
      host.setAttribute("data-jr-ignore", "");
      shadow = host.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      style.textContent = CSS;
      shadow.append(style, el("div", { class: "wrap" }));
    }
    document.documentElement.append(host);
    render();
    return true;
  }

  function short(s, n = 90) {
    s = String(s || "");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function sentence(s) {
    s = String(s).trim();
    return s.charAt(0).toUpperCase() + s.slice(1) + (/[.!?…)]$/.test(s) ? "" : ".");
  }

  function item(it) {
    const kids = [el("span", { class: "q", title: "Show this field", onclick: () => handlers.focus(it) }, short(it.label, 110))];
    if (it.status === "filled") {
      kids.push(el("div", { class: "v" }, short(it.value, 120),
        it.source === "tailored" ? el("span", { class: "tag" }, "tailored for this job") : null,
        it.source === "remembered" ? el("span", { class: "tag amber" }, "saved answer") : null,
        it.source === "draft" ? el("span", { class: "tag amber" }, "draft, edit it") : null,
        it.source === "ai" ? el("span", { class: "tag amber" }, "AI answer, check it") : null));
    } else if (it.differs) {
      kids.push(el("div", { class: "v" }, "On the form: " + short(it.value, 100)),
        el("div", { class: "v" }, "Your resume: " + short(it.resume, 100)));
    } else {
      if (it.why) kids.push(el("div", { class: "why" }, sentence(it.why)));
      const row = [];
      if (it.offer) {
        kids.push(el("div", { class: "v" }, "Saved answer" + (it.offer.company ? " (from " + it.offer.company + ")" : "") + ": " + short(it.offer.a, 140)));
        row.push(el("button", { onclick: () => handlers.useSaved(it) }, "Use saved answer"));
      }
      if (it.canDraft) row.push(el("button", { disabled: it.drafting, onclick: () => handlers.draft(it) }, it.drafting ? "Drafting…" : "Draft an answer"));
      if (row.length) kids.push(el("div", { class: "row" }, row));
      if (it.prompt) {
        kids.push(el("div", { class: "v" }, "Paste this into ChatGPT or Claude, then paste the answer into the field:"),
          el("textarea", { readonly: true }, it.prompt),
          el("div", { class: "row" }, el("button", { onclick: (e) => handlers.copy(it.prompt, e.target) }, "Copy prompt")));
      }
    }
    return el("li", null, kids);
  }

  /** A Work Experience / Education section the page filled differently from the resume. */
  function sectionBox(sec) {
    const n = sec.diffs.filter((d) => d.canReplace).length;
    const text = sec.undo
      ? `${sec.label}: replaced ${sec.undo.length} field${sec.undo.length === 1 ? "" : "s"} with your resume.`
      : `${sec.label}: ${n} field${n === 1 ? "" : "s"} already filled differently from your resume.`;
    return el("div", { class: "sec" }, el("div", null, text),
      el("div", { class: "row" }, sec.undo
        ? el("button", { disabled: sec.busy, onclick: () => handlers.undoSection(sec) }, "Undo")
        : el("button", { disabled: sec.busy, onclick: () => handlers.replaceSection(sec) }, sec.busy ? "Replacing…" : "Replace with my resume")));
  }

  function render() {
    if (!shadow) return;
    const wrap = shadow.querySelector(".wrap");
    wrap.replaceChildren();
    // Inside an embedded form the frame is as tall as the form, so the
    // bottom of its viewport may be far below the screen.
    wrap.classList.toggle("top", !!view.frame);
    if (!view.open) {
      wrap.append(el("button", { class: "launch", title: "Fill this application with your saved details (Alt+Shift+F)", onclick: () => handlers.fill() }, "Autofill"));
      return;
    }
    if (view.done) return renderDone(wrap);
    const needs = view.items.filter((i) => i.status === "needs");
    const filled = view.items.filter((i) => i.status === "filled");
    const had = view.items.filter((i) => i.status === "had");
    const body = el("div", { class: "body" },
      el("div", { class: "status" }, view.status),
      view.prepared ? el("div", { class: "prep" }, view.prepared) : null,
      (view.sections || []).map(sectionBox),
      needs.length ? [el("h3", null, `Needs you (${needs.length})`), el("ul", null, needs.map(item))] : null,
      filled.length ? [el("h3", null, `Filled (${filled.length})`), el("ul", null, filled.map(item))] : null,
      had.length ? el("details", null, el("summary", null, `Already answered (${had.length})`), el("ul", null, had.map(item))) : null,
      el("div", { class: "note" }, "Nothing is submitted. Check every answer, then press Submit yourself. ",
        el("a", { href: "#", onclick: (e) => { e.preventDefault(); handlers.options(); } }, "Edit saved answers")));
    wrap.append(el("div", { class: "panel", role: "dialog", "aria-label": "Onward Autofill" },
      el("header", null, el("b", null, "Onward Autofill"),
        el("button", { class: "primary", disabled: view.busy, onclick: () => handlers.fill() }, view.busy ? "Filling…" : view.step ? "Fill this step" : "Fill again"),
        el("button", { class: "x", title: "Close", "aria-label": "Close", onclick: () => { view.open = false; render(); } }, "×")),
      body));
  }

  /** After the ATS confirmed the application: what was recorded, and the next job in the queue. */
  function renderDone(wrap) {
    const d = view.done;
    const n = d.next;
    const body = el("div", { class: "body" },
      el("div", { class: "status" }, d.error ? d.error : `Marked as applied${d.title ? `: ${d.title} at ${d.company}` : ""}.`),
      n ? el("div", { class: "next" },
        el("div", null, el("b", null, "Next in your apply queue"), el("br"), `${n.title} at ${n.company} · ${n.score}% match`),
        el("a", { class: "btn", href: n.apply_url, rel: "noopener" }, "Open next application"))
        : el("div", { class: "note" }, "Nothing else is ready in your apply queue. ",
          el("a", { href: "http://127.0.0.1:8765/#queue", target: "_blank", rel: "noopener" }, "Open the queue")));
    wrap.append(el("div", { class: "panel", role: "dialog", "aria-label": "Onward" },
      el("header", null, el("b", null, "Onward"),
        el("button", { class: "x", title: "Close", "aria-label": "Close", onclick: () => { view.open = false; view.done = null; unmount(); } }, "×")),
      body));
  }

  function update(patch) {
    Object.assign(view, patch);
    render();
  }

  function unmount() {
    if (host) host.remove();
    host = shadow = null;
  }

  JR.panel = { mount, update, render, view, unmount };
})(globalThis);
