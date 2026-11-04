.PHONY: setup test fetch stats demo

BLOCKS ?= 200

setup:
	uv sync

test: setup
	uv run pytest -q

fetch: setup
	uv run intents-desk fetch --blocks $(BLOCKS)

stats:
	uv run intents-desk stats

demo: test fetch stats
