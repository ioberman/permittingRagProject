# Hosting Setup - Plan Review Copilot (permittingRagProject)

This app is hosted on an Oracle Cloud (OCI) Always Free VM. This doc is a
reference for anyone (including a future Claude Code session) picking up
ops/deploy work on it - it is not a from-scratch setup guide. See
[DEPLOYMENT_ORACLE.md](DEPLOYMENT_ORACLE.md) for that; this doc describes
how the real, currently-running instance actually differs from that guide
in a few places (see the note at the top of that file).

## Server

- **Provider**: Oracle Cloud Infrastructure (OCI), Always Free tier
- **Shape**: `VM.Standard.A1.Flex` (Ampere ARM, aarch64)
- **Resources**: 2 OCPUs, 12 GB RAM, ~44 GB boot volume
- **OS**: Ubuntu 24.04 LTS
- **Region**: US West (Phoenix), availability domain AD-3
- **Public IP**: `161.153.54.246`
- **SSH user**: `ubuntu`
- **SSH access**: `ssh -i ~/.ssh/oracle_cloud ubuntu@161.153.54.246`
  (private key lives only on the developer's laptop, not in the repo or on
  the server)

Note: OCI halved the Always Free Ampere A1 allowance in June 2026, from
4 OCPU/24GB to 2 OCPU/12GB, with no formal announcement - if resource
limits ever look wrong in the console, that's why.

## Code location on the server

```
/home/ubuntu/permittingRagProject/       # git clone of the repo, main branch
├── .venv/                               # Python virtualenv (packages installed here)
├── .env                                 # ANTHROPIC_API_KEY, GROQ_API_KEY, AUTO_SEED_ON_START=1
├── permitting.db                        # local SQLite (DATABASE_URL unset -> defaults here)
└── ...
```

`.env`, `*.db`, and `storage/` are gitignored - they exist only on the
server's local disk, not in the repo. `DATABASE_URL` is currently unset, so
the app is running on local SQLite, not Postgres.

## How the app is run

Not run manually / not run via `flask run`. It's wired up as two
systemd-managed services sitting behind each other:

**Internet → nginx (port 80) → gunicorn (127.0.0.1:8000) → Flask app**

### 1. gunicorn, managed by systemd

Unit file: `/etc/systemd/system/permitting.service`

```ini
[Unit]
Description=Permitting RAG Flask App
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/permittingRagProject
Environment="PATH=/home/ubuntu/permittingRagProject/.venv/bin"
ExecStart=/home/ubuntu/permittingRagProject/.venv/bin/gunicorn app.web:app --bind 127.0.0.1:8000 --timeout 120
Restart=always

[Install]
WantedBy=multi-user.target
```

- Single sync worker (gunicorn default - no `--workers` or `--worker-class`
  set), so **only one request is handled at a time**. A second request
  queues behind whatever the first one is doing, and any background
  seeding (`AUTO_SEED_ON_START`) competes for the same worker/core.
- `.env` is loaded by `app/__init__.py` via `load_dotenv()`, not by systemd
  - systemd doesn't need an `EnvironmentFile=` line for this to work.
- Binds only to `127.0.0.1:8000` - not reachable directly from the
  internet, on purpose. Only nginx talks to it.

### 2. nginx, reverse proxy

Config: `/etc/nginx/sites-available/permitting` (symlinked into
`sites-enabled/`; the default site was removed)

```nginx
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

No `proxy_read_timeout` / `proxy_send_timeout` is set here, so nginx uses
its **default of 60 seconds** on those. This is shorter than gunicorn's
`--timeout 120`. See "Known issue" below - this mismatch is the prime
suspect.

No HTTPS/TLS is configured. Site is `http://` only, on port 80.

## Firewall (two independent layers - both must allow a port)

1. **OCI Security List** (`permitting-vcn` - Default Security List) -
   ingress rules currently allow: TCP 22 (SSH), TCP 80 (web), plus default
   ICMP rules. Port 8000 is NOT exposed here (intentionally - only nginx
   should be reachable, not gunicorn directly).
2. **iptables on the instance itself** (Oracle's Ubuntu images ship
   restrictive by default) - currently allows established connections, TCP
   22, and TCP 80, with a REJECT catch-all after. Rules were persisted with
   `iptables-persistent` so they survive reboot.

If adding a new port (e.g. for HTTPS/443, or to temporarily expose 8000 for
debugging), **both** layers need a matching rule or it won't be reachable.

## Resolved: "Check" endpoint hung and 504'd

Symptom was: triggering the conflict-detection "check" in the UI
spun/loaded indefinitely and eventually 504'd, even though the service
itself stayed "active (running)" in `systemctl status`.

**Root cause, found by process of elimination**: it reproduced with the
`mock` engine (zero API calls) on a 2-clause project, which ruled out slow
LLM calls entirely. `find_candidate_jurisdiction_clauses`
(`app/retrieval.py`) runs before any engine-specific logic for every
engine - mock, groq, real - and was re-encoding the **entire jurisdiction
corpus from scratch on every single check**, uncached. Measured directly:
San Diego County's real 614-clause corpus took 4.55s to re-embed on a fast
x86 dev machine, every time, regardless of project size or engine. On this
box's ARM CPU that's very plausibly tens of seconds to minutes - exactly
"spins forever."

**Fix**: `JurisdictionClause` rows are effectively immutable once ingested
(a jurisdiction is only ever added to, not edited in place), so this work
never needed to be redone. The corpus embeddings are now cached per
`jurisdiction_id` in `app/retrieval.py` (`_jurisdiction_corpus_cache`),
invalidated automatically if the jurisdiction's clause set ever actually
changes. Verified: 4.55s -> 0.022s on the second call, and end-to-end
against the exact reported scenario (mock engine, San Diego project) -
first request 1.79s (cold model load), second 0.095s, both clean instead
of hanging.

If a check is still slow after pulling this fix, the two secondary factors
below are still real, just smaller:

1. **Single sync worker** means only one request is served at a time, and
   any real (`groq`/`real`) check still makes one LLM call per clause,
   serially - a project with many clauses can legitimately take a while.
   This is a deliberate tradeoff (see `AUTO_SEED_ON_START`'s race-condition
   reasoning for why more workers isn't a free win) rather than a bug to
   silently work around.
2. nginx's default 60s proxy timeout is still shorter than gunicorn's
   `--timeout 120` - worth adding `proxy_read_timeout 120s;` /
   `proxy_send_timeout 120s;` to the `location /` block as a safety margin
   regardless, now that the dominant cost (uncached retrieval) is gone.

`sudo journalctl -u permitting -f` while triggering a check from the
browser still shows exactly what the app is doing in real time if anything
looks slow again.

## Common ops commands

```bash
# App service
sudo systemctl status permitting
sudo systemctl restart permitting
sudo journalctl -u permitting -f      # live app logs

# nginx
sudo systemctl status nginx
sudo systemctl restart nginx
sudo nginx -t                          # validate config before restarting

# Deploy new code
cd ~/permittingRagProject
git pull
source .venv/bin/activate              # only needed if requirements.txt changed
pip install -r requirements.txt        # only needed if requirements.txt changed
sudo systemctl restart permitting

# System resources
free -h
sudo iptables -L INPUT -n --line-numbers
```
