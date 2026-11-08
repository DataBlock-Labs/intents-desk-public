"""Integration: run the real CLI and server as processes against the fake APIs."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import duckdb
import pytest

from tests import fakes

pytestmark = pytest.mark.integration


@pytest.fixture
def fake():
    f = fakes.Fake(rate_limit_every=7)
    base = f.start()
    yield f, base
    f.stop()


def run_cli(base, tmp_path, *args, timeout=120):
    env = {**os.environ, **fakes.env(base), "SESSION_KEY_FILE": str(tmp_path / "s.key")}
    return subprocess.run(
        [sys.executable, "-m", "intents_desk.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_fetch_follows_redirect_survives_429s_and_stores_every_block(fake, tmp_path):
    f, base = fake
    db = tmp_path / "chain.duckdb"
    out = run_cli(
        base, tmp_path, "--db", str(db), "fetch", "--blocks", "20", "--sleep", "0"
    )
    assert out.returncode == 0, out.stderr
    assert "fetched 20" in out.stdout
    assert f.block_requests > 20  # some answers were 429 and were retried
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT count(*) FROM bronze.block_seen").fetchone()[0] == 20


def test_sync_keeps_only_the_last_24_hours(fake, tmp_path):
    _, base = fake
    db = tmp_path / "desk.duckdb"
    out = run_cli(base, tmp_path, "--db", str(db), "sync", "--hours", "48")
    assert out.returncode == 0, out.stderr
    assert "purged 1 swaps older than 24 h" in out.stdout
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT count(*) FROM silver.swap").fetchone()[0] == 120
    oldest_h = con.execute(
        "SELECT epoch(now() - min(created_at)) / 3600 FROM silver.swap"
    ).fetchone()[0]
    assert oldest_h < 24


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        body = r.read()
        return (
            json.loads(body)
            if r.headers["Content-Type"].startswith("application/json")
            else body
        )


def test_server_uses_db_flag_syncs_and_serves_every_panel(fake, tmp_path):
    _, base = fake
    db, port = tmp_path / "served.duckdb", free_port()
    env = {**os.environ, **fakes.env(base), "SESSION_KEY_FILE": str(tmp_path / "s.key")}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "intents_desk.cli",
            "--db",
            str(db),
            "serve",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    app = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):  # wait until the sync thread has finished its first run
            try:
                if get(f"{app}/api/summary").get("synced_at"):
                    break
            except OSError:
                pass
            time.sleep(0.2)
        partners = get(f"{app}/api/partners")
        assert db.exists()  # the --db flag is honoured
        alert = {p[0]: p[-1] for p in partners}
        assert alert["widget-x:pda"] is True and alert["partner-good"] is False
        assert get(f"{app}/api/summary")["swaps"] == 120
        line = get(f"{app}/api/swap?address={fakes.REFUNDED}&format=agent").decode()
        assert line.startswith("status=REFUNDED route=tron→eth")
        for _ in range(50):
            posts = get(f"{app}/api/feed")["posts"]
            if posts:
                break
            time.sleep(0.2)
        assert posts[0]["text"].startswith("4 days. My funds are still stuck")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
