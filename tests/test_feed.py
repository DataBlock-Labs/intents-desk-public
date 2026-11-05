from intents_desk import sources
from intents_desk.store import connect, read_feed, save_feed, set_watch, watch_list


def test_tags_of_finds_problem_words():
    assert sources.tags_of("My swap is STUCK, support ignores my ticket") == [
        "stuck",
        "support",
    ]
    assert sources.tags_of("NEAR price looks good") == []


def test_reddit_keeps_only_swap_problem_posts(monkeypatch):
    posts = [
        {
            "id": "a1",
            "title": "Swap stuck on NEAR Intents",
            "selftext": "refund?",
            "author": "u1",
            "created_utc": 1793850832,
            "permalink": "/r/nearprotocol/comments/a1/x/",
        },
        {
            "id": "a2",
            "title": "NEAR price support level",
            "selftext": "",
            "author": "u2",
            "created_utc": 1793850832,
            "permalink": "/r/nearprotocol/comments/a2/x/",
        },
    ]
    monkeypatch.setattr(sources, "get_json", lambda url: {"data": posts})
    out = sources.get_reddit_posts()
    assert [p["id"] for p in out] == ["reddit:a1"]
    assert out[0]["url"] == "https://www.reddit.com/r/nearprotocol/comments/a1/x/"
    assert out[0]["tags"] == ["stuck", "refund"]


def test_feed_store_filters_by_tag(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    post = {
        "id": "x:1",
        "source": "x",
        "author": "a",
        "created_at": "2026-11-04T22:39:22Z",
        "text": "<b>4 days</b> stuck",
        "url": "https://x.com/a/status/1",
        "tags": ["stuck"],
    }
    save_feed(con, [post, {**post, "id": "x:2", "tags": ["support"]}])
    save_feed(con, [post])  # saving again does not duplicate
    assert [p["id"] for p in read_feed(con, "stuck")] == ["x:1"]
    assert len(read_feed(con)) == 2
    assert read_feed(con)[0]["text"] == "<b>4 days</b> stuck"  # stored as text


def test_watch_list_add_and_remove(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    set_watch(con, "alice.near", "TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq", True)
    set_watch(con, "alice.near", "TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq", True)
    assert watch_list(con, "alice.near") == ["TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq"]
    set_watch(con, "alice.near", "TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq", False)
    assert watch_list(con, "alice.near") == []
