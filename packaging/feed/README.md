# Hosting the shared job feed for free

The feed's search runs on GitHub Actions (free for a public repository), a
Cloudflare Worker starts it every 30 minutes, and the files live in
Cloudflare R2. How the feed works is in [feed.py](../../feed.py) and the
"Shared job feed" section of the main README.

| Piece | File | What it does |
| --- | --- | --- |
| Search settings | `feed-settings.json` | A broad role (software, internship to mid level, US): each reader filters by its own roles |
| One run | `run_feed.py` | Restores the search's database from R2, runs `radar.py --feed-out`, uploads new files, saves the database, uploads `index.json` last |
| Workflow | `.github/workflows/feed.yml` | Runs `run_feed.py`, one run at a time; does nothing until the secrets below exist |
| Trigger | `worker/` | A Worker on a Cron Trigger at :07 and :37 that starts the workflow through GitHub's API |

Try a run on your own computer first, with folders standing in for the buckets:

```
python packaging/feed/run_feed.py --store some/folder
```

`some/folder/feed` is then what a static host would serve; set Onward's feed address to its `file:` URL to read it.

## Setting it up

Everything below is on free plans. Cloudflare asks for a payment method before it turns R2 on, even
though this stays inside the free tier (at the time of writing: 10 GB stored, a million writes and ten
million reads a month, no charge for downloads). Check Cloudflare's pricing page before you start.

1. **R2 buckets.** In the Cloudflare dashboard, under R2, create two buckets:
   - `onward-feed`, the feed. Under its settings, Public access, allow the **Public Development URL**
     (`https://pub-<id>.r2.dev`). Cloudflare limits how fast it serves and says it isn't for production;
     it's fine for a first few hundred users. Later, connect a domain of your own to the bucket instead.
   - `onward-feed-state`, the search's database. Leave it private.
2. **An R2 API token.** Under R2, Manage API tokens, create one with "Object Read & Write" for those two
   buckets only. Note the Access Key ID, the Secret Access Key and your account ID.
3. **GitHub secrets.** In the repository's Settings, Secrets and variables, Actions:
   - secrets `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`;
   - variables `FEED_BUCKET` = `onward-feed` and `STATE_BUCKET` = `onward-feed-state`.

   Then, under Actions, run "Job feed" once by hand. The first run starts from an empty database, so it
   reads only the boards in `companies.json`; discovery adds a few hundred boards each run after that.
4. **The trigger Worker.** Create a fine-grained GitHub token for this repository only, with
   "Actions: Read and write" and nothing else. Then, in `packaging/feed/worker`:

   ```
   npx wrangler login
   npx wrangler secret put GITHUB_TOKEN
   npx wrangler deploy
   ```

   The first `wrangler` command on an account may ask for a `workers.dev` subdomain: any free name will do,
   since this Worker has no public address (`workers_dev = false`). The Worker's logs, in the Cloudflare
   dashboard, show each start or GitHub's reason for refusing one.
5. **Point Onward at it.** In Settings, under Search (advanced settings), set the feed address to the
   bucket's `r2.dev` address, e.g. `https://pub-<id>.r2.dev/`.

## Keeping an eye on it

- GitHub emails you when a run fails.
- `index.json` says when the search last ran (`checked`). A copy of Onward that finds it more than
  3 hours old searches every board itself until the feed is back, so a stopped feed slows readers down
  but never leaves them without jobs.
- The hourly schedule in the workflow keeps the feed going, more slowly, if the Worker stops.

## Moving it later

The same `run_feed.py` runs anywhere with Python and the R2 secrets in its environment. On a small server
with a disk, `radar.py --feed-out` plus an upload is enough, and the database never has to leave it.
