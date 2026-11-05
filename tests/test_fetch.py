import json
from argparse import Namespace
from pathlib import Path

import pytest

from intents_desk import cli, sources
from intents_desk.store import connect

BLOCK = json.loads(
    (Path(__file__).parent / "fixtures" / "block_intents.json").read_text()
)


def fake_block(height):
    b = json.loads(json.dumps(BLOCK))
    b["block"]["header"]["height"] = height
    return b


def args(db, blocks):
    return Namespace(db=str(db), end=1000, blocks=blocks, sleep=0.0)


def test_next_gap_backs_off_on_429_and_recovers():
    assert cli.next_gap(0.35, 0.35, True) == pytest.approx(0.525)
    assert cli.next_gap(10, 0.35, True) == 5.0  # capped
    assert cli.next_gap(1.0, 0.35, False) == pytest.approx(0.98)
    assert cli.next_gap(0.35, 0.35, False) == 0.35  # never faster than the limit


def test_fetch_prints_progress_and_resumes_without_duplicates(
    tmp_path, monkeypatch, capsys
):
    calls = []
    monkeypatch.setattr(
        sources, "get_block", lambda h: calls.append(h) or fake_block(h)
    )
    cli.cmd_fetch(args(tmp_path / "t.duckdb", 150))
    out = capsys.readouterr().out
    assert "100/150 blocks" in out and "fetched 150" in out
    cli.cmd_fetch(args(tmp_path / "t.duckdb", 200))  # 150 stored, only 50 new
    assert len(calls) == 200 and len(set(calls)) == 200
    con = connect(tmp_path / "t.duckdb")
    assert con.execute("SELECT count(*) FROM bronze.block_seen").fetchone()[0] == 200


def test_fetch_error_stops_and_keeps_what_was_stored(tmp_path, monkeypatch):
    def flaky(h):
        if h == 995:
            raise OSError("neardata down")
        return fake_block(h)

    monkeypatch.setattr(sources, "get_block", flaky)
    with pytest.raises(OSError, match="neardata down"):
        cli.cmd_fetch(args(tmp_path / "t.duckdb", 10))
    con = connect(tmp_path / "t.duckdb")
    assert con.execute("SELECT count(*) FROM bronze.block_seen").fetchone()[0] == 5


def test_default_pace_respects_neardata_free_limit(monkeypatch):
    """neardata free tier: 30 blocks/minute. A faster default causes 429 storms."""
    seen = {}
    monkeypatch.setattr(cli, "cmd_fetch", lambda a: seen.update(sleep=a.sleep))
    monkeypatch.setattr("sys.argv", ["intents-desk", "fetch"])
    cli.main()
    assert 60 / seen["sleep"] <= 30
