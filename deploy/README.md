# Running Second Brain on a Linux host

The Mac setup uses launchd agents (`scripts/service.sh`) and the macOS login
keychain. On Linux the same `scripts/run-*.sh` launchers run under systemd,
and secrets live in a 0600 JSON file. This guide assumes an Ubuntu 24.04
host (for example a Proxmox LXC with `nesting=1`) and the checkout at
`/opt/second-brain`.

## 1. Install

```bash
apt install -y git python3.12-venv docker.io docker-compose-v2
git clone https://github.com/yosisrael2/Second-brain.git /opt/second-brain
cd /opt/second-brain
python3 -m venv .venv && .venv/bin/pip install -e '.[gmail,telegram]'
```

## 2. Databases

Keep Postgres and Qdrant on loopback; the compose defaults publish them on
all interfaces with the `brain`/`brain` password. Create
`docker-compose.override.yml`:

```yaml
services:
  db:
    ports: !override ["127.0.0.1:27432:5432"]
  qdrant:
    ports: !override ["127.0.0.1:27333:6333", "127.0.0.1:27334:6334"]
```

Then `docker compose up -d --wait`. To move data from another host, restore a
`pg_dump -Fc` with `pg_restore --no-owner` and upload a Qdrant collection
snapshot to `/collections/brain_items/snapshots/upload?priority=snapshot`.

## 3. Configuration and secrets

Copy the env file from the old host to `/opt/second-brain` with mode 0600.
Check `DATABASE_URL` and `QDRANT_URL` point at localhost.

Secrets that the Mac keeps in the login keychain go to
`~/.config/second-brain/secrets.json` (override with `BRAIN_SECRETS_FILE`).
The backend is picked automatically: keychain on macOS, the file elsewhere;
`BRAIN_SECRET_STORE=file|keychain` forces one. Copy a token from the Mac
without it ever reaching the screen:

```bash
# on the Mac; repeat with second-brain-gmail-labeler for the labeler token
security find-generic-password -s second-brain-gmail -a default -w \
  | ssh <host> '/opt/second-brain/.venv/bin/brain secret-store second-brain-gmail default'
```

### LLM login (claude_sdk)

The default LLM provider runs through a Claude Code login, which a headless
server does not have. On a machine with a browser run `claude setup-token`,
then on the server store the printed token without echoing it:

```bash
install -m 600 /dev/null /root/.config/second-brain/claude.env
read -rs -p "token: " T && echo "CLAUDE_CODE_OAUTH_TOKEN=$T" > /root/.config/second-brain/claude.env && unset T
```

The units load that file with `EnvironmentFile=`. It must be a real
environment variable: the app reads the env file itself, but the Claude Code
subprocess only inherits the process environment. Alternatively set
`LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`.

## 4. Services

```bash
cp deploy/systemd/second-brain-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now second-brain-api second-brain-telegram second-brain-email
journalctl -u second-brain-telegram -f
curl -s localhost:27800/ops/health
```

Stop the old host's agents first: two Telegram pollers on one bot token fight
over updates, and two Gmail pollers process the same mail twice.

The WhatsApp agent is not covered here. It reads a local WhatsApp bridge
(`WHATSAPP_BRIDGE_URL`, `WHATSAPP_BRIDGE_DB`) that has to run on the same host.

## 5. Auto-deploy on merge

`second-brain-deploy.timer` runs `scripts/auto-deploy.sh` every 2 minutes.
When `origin/main` moved it backs up Postgres to `/var/backups/second-brain`
(14 days kept), fast-forwards the checkout, reinstalls, runs `alembic upgrade
head`, refreshes changed units, restarts the services and waits for
`/ops/health` (api ok). Every outcome is sent to the Telegram allowlist.

- Health check fails and no migration changed: the code rolls back to the
  previous sha.
- Health check fails after a schema change: no rollback (never `alembic
  downgrade`); you get an alert with the backup path.
- A failed sha is not retried on every tick; a newer commit, or
  `rm /opt/second-brain/.dev/deploy-failed-sha`, tries again.
- A checkout with local commits is left alone and reported.

```bash
cp deploy/systemd/second-brain-deploy.* /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now second-brain-deploy.timer
systemctl list-timers second-brain-deploy.timer
journalctl -u second-brain-deploy -n 30        # last run
systemctl start second-brain-deploy            # deploy now
```

## 6. Remote access (Tailscale)

The API keeps listening on 127.0.0.1 only. Tailscale publishes it to your own
devices, nothing opens to the internet. An unprivileged Proxmox LXC needs the
TUN device first; on the Proxmox host:

```bash
cat >> /etc/pve/lxc/102.conf <<'CONF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
CONF
pct reboot 102
```

Then inside the container:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up                       # open the printed login link
tailscale serve --bg 27800         # https://brain.<tailnet>.ts.net -> 127.0.0.1:27800
tailscale serve status
```
