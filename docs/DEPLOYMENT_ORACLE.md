# Deploying to Oracle Cloud "Always Free"

Unlike Render/Fly, this is a real, persistent VM you manage yourself - no
managed build pipeline, no automatic HTTPS, no process supervision unless
you set it up. In exchange, Oracle's Always Free tier (as of when this was
written) never bills you at all within its resource caps - not "cheap,"
actually free, indefinitely. The tradeoff is your time: budget a few hours
for a first attempt, and expect to debug some of this live rather than have
every step work on the first try - I don't have an Oracle account to test
this exact guide against, unlike the Render steps earlier in this project.

## Before you start

**Database: SQLite, not Postgres.** The Render guide recommends Postgres
specifically because Render's filesystem is wiped on every deploy/restart.
That doesn't apply here - this VM's disk is genuinely persistent, so SQLite
(the app's default, `DATABASE_URL` unset) just works with zero extra
services to install or manage. Simpler is better for a single-VM demo.

**VM shape: the Ampere A1 (ARM) shape**, not the AMD "always free" micro
shapes - it's the one with real headroom (up to 4 OCPU / 24GB RAM total,
splittable across instances). 1 OCPU / 6GB is plenty for this app; you
don't need to claim the whole allocation for one instance.

**Image: Ubuntu** (22.04 or 24.04 LTS) - the instructions below assume
`apt`. Oracle Linux is the other common default; swap `apt` for `dnf` and
adjust package names if you pick that instead.

## 1. Create the account and the VM instance

1. Sign up at oracle.com/cloud/free - identity verification requires a
   card, but the Always Free resources themselves don't charge it.
2. Console -> **Compute -> Instances -> Create Instance**.
3. **Image**: Ubuntu (latest LTS). **Shape**: click "Change shape" ->
   **Ampere** -> **VM.Standard.A1.Flex** -> 1 OCPU / 6GB is a reasonable
   starting point (you can resize later within the free allocation).
4. Under **Add SSH keys**, either upload a public key you already have
   (`~/.ssh/id_ed25519.pub` or similar) or let Oracle generate a keypair
   and download the private key - you'll need it to SSH in.
5. Create the instance and wait for it to reach "Running." Note its
   **Public IP address** on the instance detail page.

If instance creation fails with an "out of capacity" error for the A1
shape: this is a known, commonly-reported Always Free issue, not something
you did wrong - it means Oracle's free ARM capacity is temporarily
exhausted in that region. Try a different Availability Domain in the same
region (there's usually a dropdown), or try again later.

## 2. Open the firewall - two separate layers

Oracle has two independent firewalls; you need to open port **8000** (what
gunicorn will listen on) in **both**, or the app will be unreachable even
though it's running correctly. This trips up almost everyone the first
time.

**a) Oracle's cloud-level firewall (Security List):**
Console -> your instance -> the **Subnet** link -> **Security Lists** ->
default security list -> **Add Ingress Rules**:
- Source CIDR: `0.0.0.0/0`
- IP Protocol: TCP
- Destination Port Range: `8000`

**b) The VM's own OS firewall (`iptables`/`netfilter`, pre-configured by
Oracle's Ubuntu image):**
After SSHing in (step 3), run:
```
sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT
sudo netfilter-persistent save
```
Skipping either (a) or (b) looks identical from the outside - a connection
that just hangs or refuses - so if the app doesn't load later, this is the
first thing to re-check.

## 3. SSH in and install system dependencies

```
ssh -i /path/to/your/private_key ubuntu@<public-ip>

sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

Confirm the Python version is 3.12+ (`python3 --version`); if Ubuntu's
default is older, install `python3.12` from the `deadsnakes` PPA instead
and use that binary for the venv in step 5.

## 4. Create a dedicated app user and clone the repo

Don't run the app as root or as your login user - a dedicated system user
with no shell keeps its permissions scoped to only what it needs.

```
sudo useradd --system --home /opt/plan-review-copilot --shell /usr/sbin/nologin planreview
sudo mkdir -p /opt/plan-review-copilot
sudo chown planreview:planreview /opt/plan-review-copilot

sudo -u planreview git clone https://github.com/<your-username>/permittingRagProject.git /opt/plan-review-copilot
cd /opt/plan-review-copilot

sudo -u planreview python3 -m venv .venv
sudo -u planreview .venv/bin/pip install -r requirements.txt
```

## 5. Configure environment variables

```
sudo -u planreview tee /opt/plan-review-copilot/.env > /dev/null <<'EOF'
STORAGE_ROOT=/opt/plan-review-copilot/storage
ANTHROPIC_API_KEY=
GROQ_API_KEY=
AUTO_SEED_ON_START=1
EOF
sudo chmod 600 /opt/plan-review-copilot/.env
sudo -u planreview mkdir -p /opt/plan-review-copilot/storage
```

`DATABASE_URL` is deliberately left unset - the app's default
(`sqlite:///./permitting.db`, relative to `WorkingDirectory`) is exactly
right here since the disk is persistent. Fill in whichever API key(s) you
actually want the `groq`/`real` engines to work with; both are optional at
deploy time, same as the Render guide.

## 6. Install and start the systemd service

The unit file is checked into this repo at
`deploy/oracle/plan-review-copilot.service` - copy it in rather than
retyping it:

```
sudo cp /opt/plan-review-copilot/deploy/oracle/plan-review-copilot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plan-review-copilot
sudo systemctl status plan-review-copilot
```

`enable` makes it start automatically on reboot, `--now` also starts it
immediately. `status` should show `active (running)` - if it shows
`failed`, `journalctl -u plan-review-copilot -n 50 --no-pager` shows the
actual error (missing dependency, bad path in the unit file, etc.).

## 7. Verify

Visit `http://<public-ip>:8000` in a browser. `AUTO_SEED_ON_START=1` means
the projects list will be empty for a minute or two after the very first
start while seeding runs in the background (same behavior as the Render
setup) - refresh after a bit. Confirm:
- The projects list loads
- A demo project's page loads and shows its sheets
- `/metrics` loads
- The Diagrams dropdown and Document network graph both render

If nothing loads at all, re-check step 2 (both firewalls) before assuming
it's an application bug - a connection that just times out almost always
means a firewall, not gunicorn.

## Updating the app later

There's no git-push-to-deploy here - pull and restart manually:

```
cd /opt/plan-review-copilot
sudo -u planreview git pull
sudo -u planreview .venv/bin/pip install -r requirements.txt
sudo systemctl restart plan-review-copilot
```

## Optional: a real domain and HTTPS

`http://<ip>:8000` works fine for testing but isn't a URL you'd want to
hand someone, and browsers will flag it as insecure. To get a real
`https://` link:

1. Point a domain (or free subdomain from a service like DuckDNS) at the
   VM's public IP.
2. `sudo apt install -y nginx certbot python3-certbot-nginx`
3. Configure nginx as a reverse proxy from port 80/443 to `127.0.0.1:8000`,
   then `sudo certbot --nginx` to get and auto-renew a Let's Encrypt
   certificate.
4. Open ports 80 and 443 in both firewalls (same two-layer gotcha as
   step 2), and you can close port 8000 to the outside world entirely once
   nginx is the only public entry point.

This is a genuinely separate chunk of work from getting the app running at
all - worth doing once the base setup above is confirmed working, not
before.
