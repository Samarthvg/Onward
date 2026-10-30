/* Job sites' sign-in and create-account pages (Workday has one per
 * company): fills the email and the password saved in Onward's Apply kit.
 * It never presses Sign In or Create Account, never ticks an agreement,
 * and leaves a CAPTCHA or an email check to the user.
 */
(function (root) {
  "use strict";
  const JR = root.JR = root.JR || {};
  const { typeInto } = JR.dom;

  const shown = (el) => !el.disabled && !el.readOnly && el.getClientRects().length > 0;

  /** The page's account form: its email box and password boxes (two when
   *  creating an account: the password and its confirmation), or null. */
  function find(doc) {
    const pw = [...doc.querySelectorAll("input[type='password']")].filter(shown);
    if (!pw.length || pw.length > 2) return null;
    // The email box in the same form, or the nearest wrapper holding both.
    let scope = pw[0].form || pw[0].parentElement;
    while (scope && scope !== doc.body && !emailIn(scope)) scope = scope.parentElement;
    const email = scope ? emailIn(scope) : null;
    if (!email) return null;
    return { email, password: pw[0], verify: pw[1] || null, kind: pw.length === 2 ? "create" : "signin" };
  }

  function emailIn(scope) {
    return [...scope.querySelectorAll("input[type='email'], input[type='text'], input:not([type])")].filter(shown)
      .find((i) => i.type === "email" || /e-?mail|user ?name/i.test([i.name, i.id, i.autocomplete,
        i.getAttribute("data-automation-id"), i.getAttribute("aria-label")].join(" "))) || null;
  }

  /** Types what's missing; boxes the user already typed in stay as they are.
   *  Returns which boxes were filled. */
  function fill(form, acct) {
    const done = [];
    const put = (el, v, what) => { if (el && !el.value && typeInto(el, v)) done.push(what); };
    put(form.email, acct.email, "email");
    put(form.password, acct.password, "password");
    put(form.verify, acct.password, "verify");
    return done;
  }

  JR.account = { find, fill };
})(globalThis);
