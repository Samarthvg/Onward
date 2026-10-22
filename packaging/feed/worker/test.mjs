// The trigger Worker against a fake GitHub API. Run: node packaging/feed/worker/test.mjs
import assert from "node:assert/strict";
import worker, { dispatch } from "./worker.js";

const env = { GITHUB_TOKEN: "github_pat_TEST", REPO: "Samarthvg/Onward", WORKFLOW: "feed.yml", REF: "main" };
let passed = 0;
const check = async (name, fn) => { await fn(); passed++; console.log("ok", name); };

await check("starts the workflow with the token, on the branch", async () => {
  const calls = [];
  await dispatch(env, async (url, init) => { calls.push({ url, init }); return new Response(null, { status: 204 }); });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.github.com/repos/Samarthvg/Onward/actions/workflows/feed.yml/dispatches");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.Authorization, "Bearer github_pat_TEST");
  assert.deepEqual(JSON.parse(calls[0].init.body), { ref: "main" });
});

await check("a refusal from GitHub fails the run, with GitHub's reason", async () => {
  await assert.rejects(dispatch(env, async () => new Response('{"message":"Bad credentials"}', { status: 401 })),
    /GitHub answered 401: .*Bad credentials/);
});

await check("missing or odd settings fail before any request", async () => {
  const never = async () => { throw new Error("no request expected"); };
  await assert.rejects(dispatch({ ...env, GITHUB_TOKEN: "" }, never), /GITHUB_TOKEN isn't set/);
  await assert.rejects(dispatch({ ...env, REPO: "../../x" }, never), /owner\/name/);
  await assert.rejects(dispatch({ ...env, WORKFLOW: "feed.sh" }, never), /\.yml/);
});

await check("the cron handler runs the dispatch in the background", async () => {
  let waited = null;
  const real = globalThis.fetch;
  globalThis.fetch = async () => new Response(null, { status: 204 });
  try {
    await worker.scheduled({}, env, { waitUntil: (p) => { waited = p; } });
    await waited;
  } finally {
    globalThis.fetch = real;
  }
  assert.ok(waited instanceof Promise);
});

console.log(`${passed} checks passed`);
