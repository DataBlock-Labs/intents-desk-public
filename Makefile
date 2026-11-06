.PHONY: serve-tailnet setup test fetch stats demo serve sync partners bench thesis fill-sample partner-report ui-check audit backup

BLOCKS ?= 200
HOURS ?= 24
DB ?= data/desk.duckdb
REFERRAL ?= %:pda
PORT ?= 8420
KEYS ?= $(HOME)/.config/intents-desk/oneclick.env
XENV ?= $(HOME)/.config/last30days/.env
# Telegram alerts (optional): TELEGRAM_BOT_TOKEN=... from @BotFather. Never commit it.
TGENV ?= $(HOME)/.config/intents-desk/telegram.env

setup:
	uv sync

test: setup
	uv run pytest -q

fetch: setup
	uv run intents-desk fetch --blocks $(BLOCKS)

stats:
	uv run intents-desk stats

demo: test fetch stats

# Loads API keys from $(KEYS) if the file exists. The swap box works without keys.
# The issue feed also reads X when the X_BEARER_TOKEN line exists in $(XENV).
serve: setup
	set -a; [ -f "$(KEYS)" ] && . "$(KEYS)"; [ -f "$(TGENV)" ] && . "$(TGENV)"; set +a; \
	X_BEARER_TOKEN="$$(grep -s '^X_BEARER_TOKEN=' "$(XENV)" | cut -d= -f2- | tr -d '"')" \
	uv run intents-desk serve --port $(PORT)

# Serve on the tailnet over HTTPS (wallet sign-in needs HTTPS and a matching host).
# Open https://<this machine>.ts.net:18420 from any tailnet device.
TS_PORT ?= 18420
serve-tailnet:
	tailscale serve --bg --https=$(TS_PORT) http://127.0.0.1:$(PORT)
	PUBLIC_HOST="$$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'):$(TS_PORT)" \
	$(MAKE) serve

# Needs INTENTS_EXPLORER_JWT in $(KEYS). About 1 page of 1,000 swaps every 5.5 s.
sync: setup
	set -a; [ -f "$(KEYS)" ] && . "$(KEYS)"; set +a; uv run intents-desk sync --hours $(HOURS)

partners: setup
	uv run intents-desk partners --hours $(HOURS)

# Measure time, space and response size. See scripts/bench.py.
bench: setup
	set -a; [ -f "$(KEYS)" ] && . "$(KEYS)"; set +a; uv run python scripts/bench.py measure

# Reports read $(DB). Stop the server first, or point DB at a snapshot copy.
fill-sample: setup
	uv run intents-desk --db $(DB) fill-sample --n 300

thesis: setup
	uv run intents-desk --db $(DB) thesis

partner-report: setup
	uv run intents-desk --db $(DB) partner-report --referral '$(REFERRAL)'

# DataBlock design rules on the page (scripts/ui_check.py). Exit 1 on any violation.
ui-check:
	uv run python scripts/ui_check.py

# Operations audit (src/intents_desk/ops.py): recent job runs, and a verified Parquet backup
# of the run log, hourly rollups and public chain data (never raw 1Click data).
BACKUP_DIR ?= data/backups

audit: setup
	uv run intents-desk --db $(DB) audit

backup: setup
	uv run intents-desk --db $(DB) backup --out $(BACKUP_DIR)
