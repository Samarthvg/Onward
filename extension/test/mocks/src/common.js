// Shared mock plumbing: the test reads React's own state (not the DOM) to
// prove the page noticed each value, and counts every submit or navigation.
export function exposeState(S) {
  window.__mockState = () => JSON.parse(JSON.stringify(S));
  window.__submitted = 0;
  window.__navClicks = 0;
  document.addEventListener("submit", () => { window.__submitted++; }, true);
}
