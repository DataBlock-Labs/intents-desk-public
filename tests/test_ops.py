import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from intents_desk import ops
from intents_desk.store import connect, load_block, load_explorer_txs
from tests.fakes import explorer_swaps

BLOCK = json.loads(
    (Path(__file__).parent / "fixtures" / "block_intents.json").read_text()
)


def test_audited_logs_ok_and_error_runs(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    with ops.audited(con, "intents-desk", "sync") as run:
        run["rows"] = 7
    with pytest.raises(RuntimeError), ops.audited(con, "intents-desk", "fetch") as run:
        run["rows"] = 3
        raise RuntimeError("neardata down")
    rows = con.execute(
        "SELECT job, status, rows_out, error, finished_at IS NOT NULL, code_version"
        " FROM ops.run_log ORDER BY started_at"
    ).fetchall()
    assert rows[0][:3] == ("sync", "ok", 7) and rows[0][4]
    assert rows[1][:4] == ("fetch", "error", 3, "RuntimeError: neardata down")
    assert rows[1][5]  # a git sha or "unknown"


T0 = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)


def swaps_between(first_h: float, last_h: float, every_min: int = 10, partner="p"):
    """One swap every `every_min` minutes from T0 + first_h to T0 + last_h."""
    out, t = [], T0 + timedelta(hours=first_h)
    while t <= T0 + timedelta(hours=last_h):
        i = len(out)
        out.append(
            {
                "depositAddress": f"{partner}{t.timestamp():.0f}x",
                "createdAt": t.isoformat(),
                "status": "REFUNDED" if i % 2 else "SUCCESS",
                "referral": partner,
                "refundReason": "QUOTE_EXPIRED" if i % 2 else None,
                "amountInUsd": "10",
                "originAsset": "nep141:wrap.near",
                "destinationAsset": "nep141:wrap.near",
            }
        )
        t += timedelta(minutes=every_min)
    return out


def hours(con):
    return {
        (h, n)
        for h, n in con.execute(
            "SELECT hour(hour AT TIME ZONE 'UTC'), sum(swaps)::INT"
            " FROM ops.hourly_swap_summary GROUP BY 1"
        ).fetchall()
    }


def test_only_complete_settled_hours_are_rolled_up(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    load_explorer_txs(con, swaps_between(0.5, 6))  # 00:30 .. 06:00
    ops.rollup_swaps(con)
    # 00:00 is incomplete (data starts 00:30); an hour is settled when its end + 2 h
    # <= the newest swap (06:00), so 03:00 is the last one
    assert hours(con) == {(1, 6), (2, 6), (3, 6)}


def test_a_later_partial_source_never_shrinks_a_rolled_up_hour(tmp_path):
    full = connect(tmp_path / "full.duckdb")
    load_explorer_txs(full, swaps_between(0.5, 8))
    ops.rollup_swaps(full)
    before = hours(full)
    full.execute(
        "DELETE FROM bronze.explorer_tx WHERE created_at < ?",
        [T0 + timedelta(hours=2, minutes=30)],
    )
    ops.rollup_swaps(full)  # the purge left 02:00 half-empty: it must not be replaced
    assert {h for h in hours(full) if h[0] <= 4} == {h for h in before if h[0] <= 4}
    assert (2, 6) in hours(full)


def test_rollup_reads_another_database_without_copying_raw_rows(tmp_path):
    src = connect(tmp_path / "old.duckdb")
    load_explorer_txs(src, swaps_between(0.5, 6))
    src.close()
    import duckdb

    con = connect(tmp_path / "new.duckdb")
    old = duckdb.connect(str(tmp_path / "old.duckdb"), read_only=True)
    assert ops.rollup_swaps(con, old, label="old.duckdb") > 0
    assert con.execute("SELECT count(*) FROM silver.swap").fetchone()[0] == 0
    assert con.execute(
        "SELECT DISTINCT source FROM ops.hourly_swap_summary"
    ).fetchall() == [("old.duckdb",)]
    day = con.execute(
        "SELECT day, sum(swaps)::INT FROM ops.daily_swap_summary GROUP BY day"
    ).fetchall()
    assert day == [(T0.date(), 18)]


def test_backup_has_no_api_data_and_detects_tampering(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    load_block(con, BLOCK)
    load_explorer_txs(con, explorer_swaps(datetime.now(UTC)))
    ops.rollup_swaps(con)
    with ops.audited(con, "intents-desk", "sync") as run:
        run["rows"] = 1
    folder = ops.backup(con, "intents-desk", tmp_path / "backups")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert set(manifest["tables"]) == {
        "ops.run_log",
        "ops.hourly_swap_summary",
        "bronze.receipt",
        "silver.event",
        "silver.token_diff",
    }
    assert not any("explorer" in p.name for p in folder.iterdir())
    assert ops.verify_backup(folder) == []
    (folder / "bronze.receipt.parquet").write_bytes(b"tampered")
    problems = ops.verify_backup(folder)
    assert any("bronze.receipt: hash differs" in p for p in problems)


def test_jobs_on_two_threads_do_not_conflict(tmp_path):
    import threading

    con = connect(tmp_path / "t.duckdb")
    errors = []

    def job(name):
        try:
            for _ in range(20):
                with ops.audited(con.cursor(), "intents-desk", name) as run:
                    run["rows"] = 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=job, args=(n,)) for n in ("sync", "feed")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert (
        con.execute("SELECT count(*) FROM ops.run_log WHERE status = 'ok'").fetchone()[
            0
        ]
        == 40
    )


def fake_remote(monkeypatch, root: Path, corrupt: str | None = None):
    """Stand-in for ssh/rsync: 'remote' paths live under `root` on this machine."""
    import hashlib
    import subprocess

    real_run = subprocess.run

    def run(cmd, **kw):
        if cmd[0] == "ssh" and "mkdir" in cmd:
            (root / cmd[-1].lstrip("/")).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "tar":  # local side: really make the tar stream
            return real_run(cmd, capture_output=True, check=True)
        if cmd[0] == "ssh" and cmd[-1].startswith("tar -C"):
            import io
            import tarfile

            dest = root / cmd[-1].split("'")[1].lstrip("/")
            with tarfile.open(fileobj=io.BytesIO(kw["input"])) as t:
                t.extractall(dest, filter="data")
            if corrupt:
                (dest / corrupt).write_bytes(b"bit rot")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "ssh" and "sha256sum" in cmd:
            d = root / cmd[cmd.index("cd") + 1].lstrip("/")
            out = "".join(
                f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
                for p in sorted(d.glob("*.parquet"))
            )
            return subprocess.CompletedProcess(cmd, 0, out, "")
        raise AssertionError(cmd)

    monkeypatch.setattr(ops.subprocess, "run", run)


def test_push_copies_and_checks_hashes_on_the_remote(tmp_path, monkeypatch):
    con = connect(tmp_path / "t.duckdb")
    load_block(con, BLOCK)
    folder = ops.backup(con, "intents-desk", tmp_path / "local")
    fake_remote(monkeypatch, tmp_path / "nas")
    assert ops.push(folder, "nas:/volume1/backups/datablock") == []
    copied = tmp_path / "nas" / "volume1/backups/datablock/intents-desk" / folder.name
    assert (copied / "manifest.json").exists()


def test_push_reports_a_file_corrupted_on_the_way(tmp_path, monkeypatch):
    con = connect(tmp_path / "t.duckdb")
    load_block(con, BLOCK)
    folder = ops.backup(con, "intents-desk", tmp_path / "local")
    fake_remote(monkeypatch, tmp_path / "nas", corrupt="silver.event.parquet")
    assert ops.push(folder, "nas:/b") == [
        "silver.event: remote hash differs or missing"
    ]


def test_push_needs_an_absolute_remote_path(tmp_path):
    with pytest.raises(ValueError):
        ops.push(tmp_path, "nas:relative/path")


def test_a_tampered_manifest_cannot_name_other_tables_or_files(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    load_block(con, BLOCK)
    folder = ops.backup(con, "intents-desk", tmp_path / "b")
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["tables"]["x') ; DROP TABLE y; --"] = {"rows": 0, "sha256": "0"}
    manifest["tables"]["../../etc/passwd"] = {"rows": 0, "sha256": "0"}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    problems = ops.verify_backup(folder)
    assert sum("not a known backup table" in p for p in problems) == 2
    assert len(problems) == 2  # the real tables still check out


def test_backup_folder_with_a_quote_is_refused(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    with pytest.raises(ValueError, match="quote"):
        ops.backup(con, "intents-desk", tmp_path / "it's")
    assert not (tmp_path / "it's").exists()  # refused before anything is created
