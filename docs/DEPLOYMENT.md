# Deploying to Render

**This app has since moved off Render** to a self-managed Oracle Cloud
Always Free VM - see [HOSTING.md](HOSTING.md) for the current live setup,
or [DEPLOYMENT_ORACLE.md](DEPLOYMENT_ORACLE.md) for the from-scratch guide.
Render hit real memory/timeout limits on its free tier that motivated the
move (see the retrieval-library history in `app/retrieval.py` and
`CLAUDE.md`). This guide is left here for reference / in case Render (or a
similar PaaS) is ever the right fit again, not because it's the active
deployment.

Render runs a real Python process (unlike GitHub Pages, which is static-file
only and can't run this app at all - see the note in `app/web.py`). This
covers the manual dashboard path (recommended for your first deploy, since
you see and approve every setting) and the optional `render.yaml` blueprint
shortcut for repeat/reproducible deploys.

## Before you start: two decisions

**1. Database: SQLite or Postgres?**

The app defaults to a local SQLite file (`DATABASE_URL` unset ->
`sqlite:///./permitting.db`). That file lives on the container's local disk,
which Render wipes on every deploy and every time the service restarts or
spins down (Render's free/starter web services aren't guaranteed to run on
the same instance continuously). Two ways to get real persistence:

- **Recommended: Render Postgres.** SQLAlchemy is the only thing touching
  the database in this codebase (no raw SQLite-specific SQL anywhere), so
  pointing `DATABASE_URL` at a Postgres connection string just works - no
  code changes. `psycopg2-binary` is already in `requirements.txt` for this.
  Render Postgres gets automatic backups and isn't tied to your web
  service's disk lifecycle.
- **Simpler but weaker: keep SQLite, add a Render Disk.** A persistent Disk
  (see step 5) survives deploys, so `DATABASE_URL` can stay pointed at a
  file on that disk. Downsides: no automatic backups, and Render Disks only
  attach to a single instance, so this doesn't survive a move to multiple
  instances later.

These instructions default to Postgres. If you'd rather start with the
SQLite+Disk path, skip step 2 and set `DATABASE_URL` to
`sqlite:////var/data/permitting.db` (four slashes - absolute path into the
disk mount from step 5) instead.

**2. Instance size.** At the time this guide was written, retrieval
(`app/retrieval.py`) ran on `fastembed` (ONNX Runtime) instead of
`sentence-transformers`/PyTorch, specifically to fit a 512MB free-tier
host - a real deploy at that size was OOM-killed before it could even bind
a port on the torch-based version, even with the CPU-only wheel (torch
alone is 500MB+ installed). **That swap has since been reverted** - the
app moved to Oracle (real RAM, see `docs/HOSTING.md`) and
`app/retrieval.py` is back on `sentence-transformers`, the more mature
library, per `CLAUDE.md`'s architecture notes. If you're reviving this
Render path on a small instance, you'd want to re-apply the fastembed swap
yourself; don't assume the current code fits a 512MB host as-is.

## 1. Push to GitHub

Render deploys from a GitHub (or GitLab) repo. If this project isn't pushed
yet:

```
git remote add origin <your-repo-url>
git push -u origin main
```

`.env`, `*.db`, and `storage/` are already gitignored - nothing secret or
local-only gets pushed.

## 2. Create the Postgres database (skip if using SQLite+Disk)

In the Render dashboard: **New > PostgreSQL**. Name it (e.g.
`plan-review-copilot-db`), pick a region close to you, pick a plan. Once
created, copy its **Internal Database URL** - you'll paste this into the web
service's `DATABASE_URL` in step 4.

## 3. Create the web service

**New > Web Service**, connect the GitHub repo.

- **Runtime**: Python 3
- **Build command**: `pip install -r requirements.txt`
- **Start command**: `gunicorn app.web:app --timeout 120`
- **Plan**: try the smallest tier first and check the deploy log (see sizing note above)

The `--timeout 120` matters more than it looks: gunicorn's 30s default has
been observed killing a real request mid-flight on Render's free-tier CPU -
the document network graph's first hit of the embedding model (fastembed)
landing while `AUTO_SEED_ON_START`'s background thread was still working
through Chicago's 3800+ clauses, both competing for the same single core.
The client sees a truncated response (e.g. "Unexpected end of JSON input"),
not a real error - the request was still in progress, just cut off.

## 4. Set environment variables

On the web service's **Environment** tab:

| Key | Value |
|---|---|
| `DATABASE_URL` | The Postgres Internal Database URL from step 2 (or the SQLite path from the decision above) |
| `STORAGE_ROOT` | `/var/data/storage` (only meaningful once the disk from step 5 exists - uploaded plan files live here) |
| `ANTHROPIC_API_KEY` | Your real key, if you want the `real` engine to work |
| `GROQ_API_KEY` | Your real key, if you want the `groq` engine to work |

Both API keys are optional at deploy time - the app runs fine without them,
those two engine options in the UI just won't work until set (the `mock` and
`preview` options need no key at all).

## 5. Add a persistent disk (for uploaded files either way, or for the DB too if using SQLite)

**Disks** tab on the web service -> **Add Disk**. Mount path `/var/data`,
pick a size (1GB is plenty to start). This is what `STORAGE_ROOT` in step 4
points into - without it, every uploaded plan document disappears on the
next deploy.

## 6. Deploy, then seed reference data

First deploy will build and start the service; it'll come up with an empty
database (`init_db()` creates the schema automatically on startup, same as
locally). Two ways to load jurisdiction code data and the demo projects,
depending on whether your plan has Shell access:

**With Shell access** (paid plans): open the **Shell** tab on the web
service (or `render ssh` via the CLI) and run the same seed scripts you use
locally:

```
python scripts/seed_jurisdictions.py
python scripts/seed_demo_data.py
python scripts/seed_large_demo.py   # optional - a bigger 12-sheet, 8-discipline demo project
```

**Without Shell access** (free tier): set the environment variable
`AUTO_SEED_ON_START=1` on the web service (Environment tab, which free-tier
services do have). The app runs the same three idempotent seed scripts
in-process on every boot when this is set - safe to leave on across every
future deploy/restart, since each script skips anything already ingested.
Don't run the seed scripts from your own machine against the remote
`DATABASE_URL` instead - that writes the seeded files to your local
`STORAGE_ROOT`, not Render's, so the app would have database rows pointing
at files that don't exist on the actual deployed filesystem.

Either way, these read from `seed_data/` in the repo, so they work
identically on Render as they do locally - no manual re-upload needed.

## 7. Verify

Visit the `.onrender.com` URL Render assigns (shown on the service page).
Confirm:
- The projects list loads (`/`)
- A demo project's page loads and shows its sheets
- The Diagrams dropdown opens and both diagrams render
- The "Document network" button on a project page opens and renders a graph
- `/projects/<id>/audit-report` renders (exercises the DB read path end to end)
- `/metrics` loads

## Optional: one-click blueprint

`render.yaml` in the repo root mirrors steps 2-5 as a Render Blueprint - use
**New > Blueprint** and point it at the repo instead of doing steps 2-5 by
hand. Review the plan/size values in that file first; Render's blueprint
spec and pricing tiers change over time, so treat it as a starting point to
verify against Render's current dashboard, not a guarantee it's current.

## What's still local-only after this

- `.env`-based local dev is untouched - these steps only affect the Render
  deployment's own environment variables.
- The `mock`/`preview` engines still work with zero configuration on Render,
  same as locally - useful for a demo where you haven't set API keys yet.
