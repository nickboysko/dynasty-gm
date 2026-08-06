# Deploying Dynasty GM to Render

One-time setup to get the app on a public URL, reachable from any device,
with data updates that don't depend on your PC being on. Everything below
that's a "click this in a browser" step has to be done by you -- there's no
GitHub/Render CLI available to script it end to end.

## 1. Push the code to a private GitHub repo

If this repo isn't already on GitHub:
1. On github.com: **New repository** → name it (e.g. `dynasty-gm`) → **Private** → Create.
2. Locally:
   ```bash
   git remote add origin https://github.com/<you>/dynasty-gm.git
   git push -u origin main
   ```

## 2. Create a second, private "data" repo

This one holds nothing but a `dynasty.db` snapshot -- it's how the app
survives Render wiping its disk on every idle restart. **Important: never
connect this repo to Render.** It must stay a plain, inert git remote --
pushing a snapshot here must never trigger a redeploy.

On github.com: **New repository** → name it (e.g. `dynasty-gm-data`) →
**Private** → Create. Empty or with an auto-added README, doesn't matter --
the app's backup step overwrites `main` regardless.

## 3. Generate a GitHub token scoped to only the data repo

GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate
new token:
- Repository access: **Only select repositories** → pick `dynasty-gm-data` only.
- Repository permissions → **Contents: Read and write**.
- Generate, and copy the token immediately -- it's shown once.

(Scoping it to just the data repo means if it ever leaks, the blast radius
is "someone can overwrite a database snapshot," not "someone can touch your
code repo.")

## 4. Sign up for Render and connect the code repo

1. render.com → sign up (free, no credit card required).
2. **New** → **Blueprint** → connect your GitHub account → select the
   `dynasty-gm` (code) repo. Render will read `render.yaml` automatically.

## 5. Set the environment variables

Render will prompt for the blueprint's `sync: false` vars during setup (or
set them later under the service's **Environment** tab):

| Key | Value |
|---|---|
| `APP_PASSWORD` | pick a password -- this gates the whole app |
| `SECRET_KEY` | run `python -c "import secrets; print(secrets.token_hex(32))"` and paste the output |
| `GITHUB_TOKEN` | the fine-grained token from step 3 |
| `GITHUB_DATA_REPO_OWNER` | your GitHub username |
| `GITHUB_DATA_REPO_NAME` | `dynasty-gm-data` (or whatever you named it) |

Don't set `PORT` -- Render provides that automatically.

## 6. Deploy and verify

- Watch the build log through the first boot. Cold start is ~30-60s, then
  the first-ever background ingest takes ~90-120s (the data repo starts
  empty, so there's nothing to restore yet) before real data shows up.
- Visit the Render URL, log in with `APP_PASSWORD`.
- Confirm the "Updating..." indicator appears then clears, and all four
  tabs (Trade Builder, Report, Free Agents, Playoff Odds) load data.
- Open the URL from your phone on cellular data to confirm "any device" access.

## What to expect afterward

Every time the app has been idle for ~15 minutes, the *next* visit repeats
the cold-start-plus-background-ingest sequence above (30-60s to wake up,
then a minute or two before fully fresh data shows). This is the accepted
tradeoff of the free tier -- there's no persistent "always warm" server
without a paid plan. Value-trend history and transaction history persist
across these restarts via the data-repo backup; the Sleeper player cache
does not (it just re-fetches, a few extra seconds, not worth backing up).

## Rotating the password or token later

Change the value in Render's Environment tab and it takes effect on the
next deploy/restart -- no code changes needed.
