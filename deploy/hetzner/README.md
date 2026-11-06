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
