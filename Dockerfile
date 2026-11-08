# Intents Desk server. The DuckDB file and the session key live on a volume at /data.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY sql ./sql
RUN uv sync --locked --no-dev \
    && useradd --create-home --uid 1000 desk \
    && mkdir -p /data && chown desk /data
USER desk
ENV PATH="/app/.venv/bin:$PATH" HOST=0.0.0.0 TRUST_PROXY=1 SESSION_KEY_FILE=/data/session.key \
    INCIDENT_REPORTS_DIR=/data/reports/incidents
# Public deploy: partner aliases, and /api/swap and /api/quote only from our page.
ENV MASK_PARTNERS=1 BROWSER_ONLY=1
EXPOSE 8420
CMD ["intents-desk", "--db", "/data/desk.duckdb", "serve", "--port", "8420"]
