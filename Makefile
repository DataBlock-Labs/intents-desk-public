.PHONY: setup test fetch stats demo serve sync partners bench thesis fill-sample partner-report ui-check

BLOCKS ?= 200
HOURS ?= 24
DB ?= data/desk.duckdb
REFERRAL ?= %:pda
PORT ?= 8420
KEYS ?= $(HOME)/.config/intents-desk/oneclick.env
XENV ?= $(HOME)/.config/last30days/.env

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
	set -a; [ -f "$(KEYS)" ] && . "$(KEYS)"; set +a; \
	X_BEARER_TOKEN="$$(grep -s '^X_BEARER_TOKEN=' "$(XENV)" | cut -d= -f2- | tr -d '"')" \
	uv run intents-desk serve --port $(PORT)

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
