/* Runs on the Job Radar dashboard (http://127.0.0.1:8765) so the options
 * page can import the Apply kit details saved there. It only answers this
 * extension's own messages and only reads; it changes nothing on the page. */
(function () {
  "use strict";
  // Firefox match patterns can't name a port, so the manifest matches every
  // local port and this script stays quiet anywhere but the dashboard's.
  if (location.port !== "8765") return;
  const api = globalThis.browser || globalThis.chrome;
  api.runtime.onMessage.addListener((msg, sender, respond) => {
    if (!msg || msg.type !== "jr:exportProfile" || sender.id !== api.runtime.id) return false;
    let profile = null;
    try { profile = JSON.parse(localStorage.getItem("jr:profile") || "null"); } catch (e) {}
    respond({ profile: profile && typeof profile === "object" ? profile : null });
    return false;
  });
})();
