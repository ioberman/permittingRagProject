# Deploying to Render

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

**2. Instance size.** `sentence-transformers` pulls in `torch`, which needs
more RAM than a 512MB instance to reliably load the embedding model
alongside Flask, PyMuPDF, etc. - confirmed in practice: a deploy on a 512MB
plan was OOM-killed before it could even bind a port. Use the build command
below (forces the CPU-only torch build, meaningfully smaller than the
default GPU one) regardless of plan, and pick a plan with more than 512MB -
check Render's current plan/pricing page for exact RAM per tier rather than
trusting a tier name here, since those change.

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
- **Build command**: `pip install torch --index-url https://download.pytorch.org/whl/cpu && pip install -r requirements.txt`
- **Start command**: `gunicorn app.web:app`
- **Plan**: more than 512MB RAM (see sizing note above)

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
locally). To get jurisdiction code data and the demo projects loaded, use
Render's **Shell** tab on the web service (or `render ssh` via the CLI) to
run the same seed scripts you use locally:

```
python scripts/seed_jurisdictions.py
python scripts/seed_demo_data.py
python scripts/seed_large_demo.py   # optional - a bigger 12-sheet, 8-discipline demo project
```

These read from `seed_data/` in the repo, so they work identically on
Render as they do locally - no manual re-upload needed.

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
