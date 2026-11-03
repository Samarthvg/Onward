/* The in-page panel: what was filled, what still needs the user, and
 * buttons for saved answers and drafts. Lives in a shadow root so the
 * page's styles can't reach it and the form scanner never sees it. */
(function (root) {
  "use strict";
  const JR = (root.JR = root.JR || {});

  const CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; }
    /* The dashboard's two worlds: the ledger in light (ink, the one blue on data),
       the terminal in dark (cards darker than the ground, a white primary). */
    .wrap {
      --bg: #ffffff; --head: #f2f4f7; --text: #0a0a0a; --muted: #5d636e; --line: #e3e6ea; --rule: #eef0f3;
      --control: #d3d7dd; --hover: #f6f7f9; --primary: #141414; --primary-ink: #ffffff; --focus: #1d5fbf;
      --chip: #eceef1; --chip-ink: #3b414b; --link: #1d5fbf;
      --warn: #c4242a; --warn-soft: #ffedea; --caution: #92400e; --caution-soft: #fffbeb; --field: #ffffff;
      --radius-ctl: 6px;
      position: fixed; right: 16px; bottom: 16px; z-index: 2147483646;
      font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--text);
    }
    @media (prefers-color-scheme: dark) {
      .wrap {
        --bg: #131217; --head: #171719; --text: #ffffff; --muted: #8e9096; --line: #252528; --rule: #1d1c21;
        --control: #3a3a3e; --hover: #1c1b20; --primary: #ffffff; --primary-ink: #000000; --focus: #4a8ad6;
        --chip: #212023; --chip-ink: #c4c5c9; --link: #6ea3e4;
        --warn: #ef6b82; --warn-soft: #2c1a20; --caution: #d9a441; --caution-soft: #2a2416; --field: #0f0e12;
        --radius-ctl: 999px;
      }
    }
    .wrap.top { top: 16px; bottom: auto; }
    .launch { border: 0; border-radius: 999px; padding: 9px 14px; background: var(--primary); color: var(--primary-ink); font: 600 13px system-ui, sans-serif; cursor: pointer; box-shadow: 0 4px 14px rgba(0,0,0,.22); }
    .launch:hover { opacity: .88; }
    .panel { width: min(380px, calc(100vw - 32px)); max-height: min(70vh, 640px); display: flex; flex-direction: column; background: var(--bg); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 10px 30px rgba(0,0,0,.18); overflow: hidden; }
    header { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-bottom: 1px solid var(--line); background: var(--head); }
    header b { flex: 1; font-size: 13px; }
    button { font: inherit; cursor: pointer; border-radius: var(--radius-ctl); border: 1px solid var(--control); background: var(--bg); color: var(--text); padding: 4px 10px; }
    button:hover { background: var(--hover); }
    button.primary { background: var(--primary); border-color: var(--primary); color: var(--primary-ink); font-weight: 600; }
    button.primary:hover { opacity: .88; background: var(--primary); }
    button[disabled] { opacity: .55; cursor: default; }
    button:focus-visible, a:focus-visible, .q:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
    .x { border: 0; background: none; font-size: 16px; line-height: 1; padding: 2px 6px; color: var(--muted); }
    .body { overflow: auto; padding: 8px 12px 10px; }
    .status { margin: 2px 0 8px; color: var(--text); }
    h3 { font-size: 12px; color: var(--muted); margin: 10px 0 4px; font-weight: 600; }
    ul { list-style: none; margin: 0; padding: 0; }
    li { padding: 6px 0; border-top: 1px solid var(--rule); }
    li:first-child { border-top: 0; }
    .q { display: block; color: var(--text); cursor: pointer; overflow-wrap: anywhere; }
    .q:hover { text-decoration: underline; text-underline-offset: 2px; }
    .v { color: var(--muted); overflow-wrap: anywhere; }
    .why { color: var(--caution); }
    .tag { display: inline-block; font-size: 11px; padding: 0 6px; border-radius: 999px; background: var(--chip); color: var(--chip-ink); margin-left: 4px; }
    .tag.amber { background: var(--caution-soft); color: var(--caution); }
    .row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
    .note { color: var(--muted); font-size: 12px; margin-top: 10px; }
    textarea { width: 100%; min-height: 90px; font: 12px/1.4 ui-monospace, monospace; margin-top: 4px; background: var(--field); color: var(--text); border: 1px solid var(--control); border-radius: 6px; }
    details > summary { cursor: pointer; color: var(--muted); margin-top: 10px; }
    a { color: var(--link); text-underline-offset: 2px; }
    .prep { margin: 0 0 8px; padding: 6px 8px; border-radius: 6px; background: var(--chip); color: var(--text); }
    .redo { display: flex; gap: 6px; margin-top: 4px; }
    .redo input { flex: 1; min-width: 0; font: inherit; padding: 4px 8px; border: 1px solid var(--control); border-radius: 6px; background: var(--field); color: var(--text); }
    .redo input:focus { outline: 2px solid var(--focus); outline-offset: -1px; }
    .warn { margin-top: 4px; padding: 5px 8px; border-radius: 6px; background: var(--warn-soft); color: var(--warn); }
    .sec { margin: 0 0 8px; padding: 6px 8px; border-radius: 6px; background: var(--caution-soft); color: var(--caution); }
    .next { margin-top: 8px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; display: grid; gap: 6px; }
    a.btn { display: inline-block; text-decoration: none; border-radius: var(--radius-ctl); padding: 5px 12px; background: var(--primary); color: var(--primary-ink); font-weight: 600; width: fit-content; }
    a.btn:hover { opacity: .88; }
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
      host.id = "onward-autofill";
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
      if (it.why) kids.push(el("div", { class: "why" }, sentence(it.why)));
      if (handlers.canRedraft(it)) kids.push(redraftBox(it));
      if (it.prompt) kids.push(el("textarea", { readonly: true }, it.prompt),
        el("div", { class: "row" }, el("button", { onclick: (e) => handlers.copy(it.prompt, e.target) }, "Copy")));
    } else if (it.differs) {
      kids.push(el("div", { class: "v" }, "On the form: " + short(it.value, 100)),
        el("div", { class: "v" }, "Your resume: " + short(it.resume, 100)));
    } else {
      if (it.why) kids.push(el("div", { class: "why" }, sentence(it.why)));
      const row = [];
      if (it.offer && it.offerWarn) {
        // Written for another company and naming it: hidden until asked for, then a second click to use.
        const w = it.offerWarn;
        kids.push(el("div", { class: "warn" }, `Your saved answer was written for ${w.company} and mentions ${w.names.join(", ")}. Don't reuse it here as is.`));
        if (it.revealed) {
          kids.push(el("div", { class: "v" }, short(it.offer.a, 400)));
          row.push(el("button", { onclick: () => handlers.useSaved(it) }, `Use it anyway (edit out ${w.names[0]})`));
        } else row.push(el("button", { class: "ghost", onclick: () => handlers.revealSaved(it) }, "Show it"));
      } else if (it.offer) {
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

  /** "Redraft with a note": the model writes the answer again, as told ("use my Y project, not X"). */
  function redraftBox(it) {
    if (!it.noteOpen) {
      return el("div", { class: "row" }, el("button", { onclick: () => { it.noteOpen = true; render(); focusNote(it); } }, "Redraft with a note"));
    }
    const go = () => { if (!it.drafting && (it.note || "").trim()) handlers.draft(it, it.note.trim()); };
    const input = el("input", { type: "text", maxlength: "500", "aria-label": "What to change in this answer",
      placeholder: "e.g. Use my Onward project instead", disabled: it.drafting,
      oninput: (e) => { it.note = e.target.value; },
      onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } else if (e.key === "Escape") { it.noteOpen = false; render(); } } });
    input.value = it.note || "";
    input.dataset.noteFor = String(view.items.indexOf(it));
    return el("div", { class: "redo" }, input,
      el("button", { disabled: it.drafting, onclick: go }, it.drafting ? "Redrafting…" : "Redraft"));
  }

  function focusNote(it) {
    const n = shadow && shadow.querySelector(`input[data-note-for="${view.items.indexOf(it)}"]`);
    if (n) n.focus();
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
      view.next ? nextBox(view.next, "Go to next job") : null,
      el("div", { class: "note" }, "Nothing is submitted. Check every answer, then press Submit yourself. ",
        el("a", { href: "#", onclick: (e) => { e.preventDefault(); handlers.options(); } }, "Edit saved answers")));
    wrap.append(el("div", { class: "panel", role: "dialog", "aria-label": "Onward Autofill" },
      el("header", null, el("b", null, "Onward Autofill"),
        el("button", { class: "primary", disabled: view.busy, onclick: () => handlers.fill() }, view.busy ? "Filling…" : view.step ? "Fill this step" : "Fill again"),
        el("button", { class: "x", title: "Close", "aria-label": "Close", onclick: () => { view.open = false; render(); } }, "×")),
      body));
  }

  /** After the ATS confirmed the application: what was recorded, and the next job in the queue. */
  /** The next job to apply to: in the list you were working through in Onward, or the apply queue. */
  function nextBox(n, action) {
    const where = n.from === "list" ? "Next in your list" : "Next in your apply queue";
    return el("div", { class: "next" },
      el("div", null, el("b", null, where), el("br"), `${n.title} at ${n.company}` + (n.score != null ? ` · ${n.score}% match` : "")),
      el("a", { class: "btn", href: n.apply_url, rel: "noopener" }, action));
  }

  // After a confirmed application the next one opens by itself, unless
  // cancelled. Not from inside an embedded form, which can't move the page.
  const NEXT_IN = 8;
  function startNext(d) {
    if (d.timer || d.cancelled || view.frame || !d.next || d.error) return;
    d.left = NEXT_IN;
    d.timer = setInterval(() => {
      d.left -= 1;
      if (d.left <= 0) { clearInterval(d.timer); location.href = d.next.apply_url; return; }
      render();
    }, 1000);
  }

  function renderDone(wrap) {
    const d = view.done;
    const n = d.next;
    startNext(d);
    const counting = d.timer && !d.cancelled;
    const body = el("div", { class: "body" },
      el("div", { class: "status" }, d.error ? d.error : `Marked as applied${d.title ? `: ${d.title} at ${d.company}` : ""}.`),
      counting ? el("div", { class: "row" }, el("span", null, `Opening the next one in ${d.left}s.`),
        el("button", { onclick: () => { d.cancelled = true; clearInterval(d.timer); render(); } }, "Stay here")) : null,
      n ? nextBox(n, "Open next application")
        : el("div", { class: "note" }, "Nothing else to apply to in your list or your apply queue. ",
          el("a", { href: "http://127.0.0.1:8765/", target: "_blank", rel: "noopener" }, "Open Onward")));
    wrap.append(el("div", { class: "panel", role: "dialog", "aria-label": "Onward" },
      el("header", null, el("b", null, "Onward"),
        el("button", { class: "x", title: "Close", "aria-label": "Close", onclick: () => { clearInterval(d.timer); view.open = false; view.done = null; unmount(); } }, "×")),
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
