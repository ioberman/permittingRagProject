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

## Known issue as of this doc: "Check" endpoint hangs and times out

Symptom: triggering the conflict-detection "check" in the UI spins/loads
indefinitely in the browser and eventually times out, even though the
service itself stays "active (running)" in `systemctl status`.

Not yet root-caused, but worth checking, roughly in order of suspicion:

1. **nginx's 60s default timeout vs. gunicorn's 120s `--timeout`.** If a
   check legitimately takes longer than 60s (looping LLM calls per clause
   via `app/llm.py` / `app/llm_groq.py`), nginx will return a 504 and drop
   the connection well before gunicorn/the app actually finishes or times
   out - this would look exactly like "loads forever then times out."
   Likely fix: add `proxy_read_timeout 120s;` and `proxy_send_timeout 120s;`
   inside the `location /` block, then `sudo systemctl restart nginx`.
2. **Single sync worker.** Only one request is served at a time. If the
   check endpoint makes many sequential LLM calls (one per clause /
   candidate set, per `app/conflict_detection.py` and
   `app/cross_discipline_detection.py`), the whole thing runs serially
   inside that one worker with no concurrency - a check over many clauses
   could legitimately take minutes. Worth timing how long a check takes
   directly against gunicorn (bypass nginx, curl `127.0.0.1:8000` with the
   actual check route) to isolate proxy-layer timeout vs. genuine slowness.
3. **This app was moved from a lightweight Render deploy (which used
   `sentence-transformers`/torch and hit memory limits) to this heavier
   Oracle box specifically to allow real compute** - that decision was
   deliberate, not a bug, so "it's using torch/full transformers" is
   expected here, not something to revert.
4. Check `sudo journalctl -u permitting -f` while triggering a check from
   the browser - this shows exactly what the app is doing (or stuck on) in
   real time, including any exception it may eventually throw once
   gunicorn's own 120s timeout is hit.

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
