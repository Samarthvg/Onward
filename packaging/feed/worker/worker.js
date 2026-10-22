// Starts the "Job feed" GitHub workflow on a Cron Trigger (wrangler.toml), since
// GitHub's own schedule can run late or be dropped under load. A workflow
// started through the API runs at once.
//
// Secrets and variables (set with wrangler, see README.md):
//   GITHUB_TOKEN  a fine-grained token for this repository only, with
//                 "Actions: read and write" and nothing else
//   REPO          owner/name, e.g. Samarthvg/Onward
//   WORKFLOW      the workflow file, feed.yml
//   REF           the branch it runs on, main

export async function dispatch(env, fetchFn = fetch) {
  for (const k of ["GITHUB_TOKEN", "REPO", "WORKFLOW", "REF"]) {
    if (!env[k]) throw new Error(`${k} isn't set`);
  }
  if (!/^[\w.-]+\/[\w.-]+$/.test(env.REPO) || !/^[\w.-]+\.ya?ml$/.test(env.WORKFLOW)) {
    throw new Error("REPO must be owner/name and WORKFLOW a .yml file name");
  }
  const res = await fetchFn(`https://api.github.com/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "onward-feed-trigger",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.REF }),
  });
  // 204: started. Anything else is a failed run the Worker's logs show.
  if (res.status !== 204) throw new Error(`GitHub answered ${res.status}: ${(await res.text()).slice(0, 300)}`);
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },
};
