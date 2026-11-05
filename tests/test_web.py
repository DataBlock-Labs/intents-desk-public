from intents_desk.web import ADDRESS, chain_of, summarize


def tx(status, origin, dest, usd="10", reason=None):
    return {
        "status": status,
        "originAsset": origin,
        "destinationAsset": dest,
        "amountInUsd": usd,
        "refundReason": reason,
        "createdAt": "2026-11-04T14:00:00Z",
    }


TRON = "nep141:tron-d28a265909efecdcee7c5028585214ea0b96f015.omft.near"
BASE = "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near"


def test_chain_of():
    assert chain_of(TRON) == "tron"
    assert chain_of("nep141:wrap.near") == "near"
    assert chain_of("nep245:v2_1.omni.hot.tg:4444119_wyixUKCL") == "hot-omni"
    assert chain_of("1cs_v1:btc:native:coin") == "btc"


def test_summarize_counts_refunds_per_route():
    d = summarize(
        [
            tx("SUCCESS", TRON, BASE),
            tx("REFUNDED", TRON, BASE, reason="QUOTE_EXPIRED"),
            tx("SUCCESS", BASE, TRON, usd=None),
        ]
    )
    assert d["swaps"] == 3 and d["usd_in"] == 20
    assert d["refund_reasons"] == [("QUOTE_EXPIRED", 1)]
    assert d["routes"][0] == ["tron → base", 2, 50.0]


def test_address_check_blocks_query_injection():
    assert ADDRESS.match("TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq")
    assert not ADDRESS.match("abc&depositMemo=x")


def test_swap_again_only_for_refunded_origin_chain_swaps(monkeypatch):
    from intents_desk import sources, web

    status = {"status": "SUCCESS", "quoteResponse": {"quoteRequest": {}}}
    monkeypatch.setattr(sources, "get_swap_status", lambda a, **_k: status)
    addr = {"address": ["TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq"]}
    assert web.swap_again(addr)[0] == 400
    status["status"] = "REFUNDED"
    status["quoteResponse"]["quoteRequest"]["depositType"] = "INTENTS"
    assert web.swap_again(addr)[0] == 400
    assert web.swap_again({"address": ["x&y"]})[0] == 400
