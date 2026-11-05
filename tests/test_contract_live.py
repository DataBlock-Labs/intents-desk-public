"""Contract tests against the real public APIs. Opt-in: `pytest -m live` (premerge full).

They check only the fields and behaviour Intents Desk relies on, so an upstream change
fails here first instead of in production.
"""

import os

import pytest

from intents_desk import sources

pytestmark = pytest.mark.live
KNOWN_SWAP = (
    "TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq"  # a refunded 1Click swap (Nov 4, 2026)
)


def test_neardata_final_block_redirect_and_block_shape():
    height = sources.get_final_height()  # follows neardata's 302
    assert height > 223_000_000
    block = sources.get_block(height - 5)
    assert block["block"]["header"]["timestamp"]
    shard = block["shards"][0]
    assert "receipt_execution_outcomes" in shard


def test_oneclick_status_fields_we_read():
    s = sources.get_swap_status(KNOWN_SWAP, retries=2)
    assert s["status"] == "REFUNDED"
    req = s["quoteResponse"]["quoteRequest"]
    for field in (
        "originAsset",
        "destinationAsset",
        "depositType",
        "amount",
        "refundTo",
    ):
        assert field in req
    for field in ("refundReason", "refundedAmountFormatted", "amountOutFormatted"):
        assert field in s["swapDetails"]


@pytest.mark.skipif(
    not os.environ.get("INTENTS_EXPLORER_JWT"), reason="needs the Explorer key"
)
def test_explorer_fields_we_store():
    [tx] = sources.get_explorer_txs(numberOfTransactions=1)
    for field in (
        "depositAddress",
        "createdAt",
        "status",
        "originAsset",
        "destinationAsset",
        "amountInUsd",
        "referral",
        "refundTo",
    ):
        assert field in tx


def test_rpc_view_account_and_access_key_shapes():
    acct = sources.get_account("near")
    assert int(acct["amount"]) > 0
    unknown = "ed25519:6E8sCci9badyRkXb3JoRpBj5p8C6Tw41ELDZoiihKEtp"  # valid form, not a key of "near"
    assert sources.get_access_key("near", unknown) is None
    assert sources.get_account("no-such-account-xyz-123.near") is None
