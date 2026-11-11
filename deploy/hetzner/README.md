# Deploy on Hetzner

The live desk runs on one Hetzner Cloud server: `intents-desk` (cax11, nbg1, Ubuntu 24.04).
The Hetzner project token is in `~/.config/cloud-lab/hetzner.env`.

## Lock-down

- Hetzner firewall `intents-desk`: SSH (22) only from the ZBOX public IP. 80 and 443 open.
- SSH: key only (`~/.ssh/id_ed25519_intents`). No passwords. Unattended security upgrades.
- The app port (8420) is not published. Only Caddy reaches it, on the Compose network.
- The app container is read-only, drops all capabilities, and has `no-new-privileges`.
- The image sets `MASK_PARTNERS=1` and `BROWSER_ONLY=1`. The env file sets `SWAP_LIVE=0`.

## Files on the server (`/opt/intents-desk`)

| File | What |
|---|---|
| `compose.yaml`, `Caddyfile` | Copies of this folder |
| `src/` | `git archive` of the commit to deploy |
| `desk.env` | Keys and settings, mode 600. Never in git |

## Deploy a new commit

```sh
H=root@<server ip>; K=~/.ssh/id_ed25519_intents
git archive main | ssh -i $K $H 'rm -rf /opt/intents-desk/src && mkdir /opt/intents-desk/src && tar -x -C /opt/intents-desk/src && chmod -R go=rX /opt/intents-desk/src'
ssh -i $K $H 'cd /opt/intents-desk && docker compose build -q app && docker compose up -d app'
```

If your public IP changes, update the SSH rule in the Hetzner console (Firewalls → intents-desk).

## Monitoring and alerts

| What | Where | Who is told |
|---|---|---|
| A background job (sync, feed, autowatch, tickets, alerts, backup…) fails 3 times in a row | the app, in `_loop` | the owner's Telegram, once; then once when it works again |
| No good swap sync for 30 min | the app (`stale` job) | the owner's Telegram |
| A job has had no good run for 3 of its intervals (also a dead job thread) | `GET /healthz` gives 503 | the uptime check below |
| The site or the server is down | `.github/workflows/uptime.yml` on the public repo, every 30 min | GitHub emails the account that last pushed the workflow |

Read the logs (each job writes one line per run: `[job] N rows` or `[job] failed: Error: …`):

```sh
H=root@<server ip>; K=~/.ssh/id_ed25519_intents
ssh -i $K $H 'cd /opt/intents-desk && docker compose logs --since 1h app | grep "\[sync\]"'
ssh -i $K $H 'cd /opt/intents-desk && docker compose logs --since 24h app | grep -o "\[[a-z_]*\] failed: [A-Za-z]*" | sort | uniq -c'
curl -s https://intents.datablock.pro/healthz
```

## Posting to X

Posting needs five lines in `desk.env`: `X_POST=1` and the four OAuth 1.0a keys of the X app
(Read and Write). Copy them from `~/.config/xurl/oauth1.env` without printing them:

```sh
H=root@<server ip>; K=~/.ssh/id_ed25519_intents
{ echo X_POST=1; sed -n 's/^\(CONSUMER_KEY\|CONSUMER_SECRET\|ACCESS_TOKEN\|TOKEN_SECRET\)=/X_\1=/p' ~/.config/xurl/oauth1.env; } \
  | ssh -i $K $H 'cat >> /opt/intents-desk/desk.env && cd /opt/intents-desk && docker compose up -d app'
```
