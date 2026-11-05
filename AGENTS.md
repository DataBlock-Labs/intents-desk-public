# AGENTS.md: Intents Desk

- Before push: `scripts/premerge fast`. Before merge to main: `scripts/premerge full` and `uat`.
  The pre-push hook runs them (`git config core.hooksPath .githooks`).
- Never commit keys. API keys come from environment variables (`INTENTS_EXPLORER_JWT`,
  `ONECLICK_JWT`, `X_BEARER_TOKEN`); the session key lives outside the repo.
- Never edit `src/intents_desk/static/near-logo.svg` (NEAR's official file; a test checks its
  hash), `static/tokens.css` (DataBlock design tokens) or `static/near-connect.js` (vendored).
- The page must pass `make ui-check` (DataBlock design rules).
- Keep partner names out of public files until the partner has been told (see `.gitignore`).
