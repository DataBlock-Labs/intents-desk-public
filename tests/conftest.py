import pytest

from intents_desk import sources


@pytest.fixture(autouse=True)
def no_explorer_wait(monkeypatch):
    """The fake APIs have no rate limit: tests do not wait for the Explorer slot."""
    monkeypatch.setattr(sources, "EXPLORER_GAP", 0.0)
    monkeypatch.setitem(sources._explorer, "last", float("-inf"))
