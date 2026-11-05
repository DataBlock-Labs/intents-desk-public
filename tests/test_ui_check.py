import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ui_check

BAD = (
    "<style>:root{--a:#fff}.x{color:#f00;border-radius:8px;font-family:Arial}"
    "button{background:var(--bad)}</style><img src=x>"
    '<button onclick="x()"> </button><a href="https://evil.example/x">e</a>'
)


def test_every_rule_catches_its_violation():
    found = ui_check.check(BAD, "el.innerHTML = x")
    assert all(len(hits) == 1 for hits in found.values()), found


def test_the_real_page_passes():
    found = ui_check.check(ui_check.PAGE.read_text(), ui_check.JS.read_text())
    assert sum(len(v) for v in found.values()) == 0, found
