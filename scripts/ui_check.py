"""Check the page against the DataBlock design rules. Exit 1 if any rule is broken.

    uv run python scripts/ui_check.py [path/to/index.html]

Rules: colours only through variables (raw hex only in :root), radius 0, fonts only
through variables, one action colour, alt text on images, no inline event handlers,
every button has a text label, no external origin outside the allow-list.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "src" / "intents_desk" / "index.html"
JS = ROOT / "src" / "intents_desk" / "static" / "app.js"
ALLOWED_ORIGINS = {"https://github.com"}  # links out from the footer
ACTION_VARS = {"--act"}


def check(html: str, js: str) -> dict[str, list[str]]:
    style = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.DOTALL))
    root = "\n".join(re.findall(r":root\s*{(.*?)}", style, re.DOTALL))
    rest = style.replace(root, "")
    body = re.sub(r"<style>.*?</style>", "", html, flags=re.DOTALL)
    out: dict[str, list[str]] = {
        "hex outside :root": re.findall(r"#[0-9a-fA-F]{3,8}\b", rest),
        "radius not 0": [
            v
            for v in re.findall(r"border-radius:\s*([^;}]+)", rest)
            if v.strip() not in ("0", "50%")
        ],
        "font-family not a variable": [
            v
            for v in re.findall(r"font(?:-family)?:\s*([^;}]+)", rest)
            if "var(--" not in v and v.strip() not in ("inherit",)
        ],
        "action colours": sorted(
            set(re.findall(r"button[^{]*{[^}]*background:\s*var\((--[a-z-]+)\)", rest))
            - ACTION_VARS
        ),
        "img without alt": [
            m for m in re.findall(r"<img\b[^>]*>", body) if "alt=" not in m
        ],
        "inline handler": re.findall(r"\son[a-z]+\s*=", body),
        "button without text": [
            m
            for m in re.findall(r"<button\b[^>]*>(.*?)</button>", body, re.DOTALL)
            if not re.sub(r"<[^>]+>", "", m).strip()
        ],
        "external origin": sorted(
            {o for o in re.findall(r'(?:src|href)="(https?://[^/"]+)', body)}
            - ALLOWED_ORIGINS
        ),
        "innerHTML in js": re.findall(r"\.innerHTML\s*=|insertAdjacentHTML", js),
    }
    return out


def main() -> int:
    page = Path(sys.argv[1]) if len(sys.argv) > 1 else PAGE
    found = check(page.read_text(), JS.read_text())
    total = sum(len(v) for v in found.values())
    for rule, hits in found.items():
        print(f"{len(hits):3}  {rule}" + (f"  {hits[:5]}" if hits else ""))
    print(f"violations: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
