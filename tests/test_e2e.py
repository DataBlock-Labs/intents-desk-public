"""End-to-end UAT in a real browser (Playwright): `pytest -m e2e` (premerge uat).

The app runs as a process against the fake APIs (tests/fakes.py). Wallet sign-in uses a
test wallet: the page's wallet library is swapped for a stand-in that makes a throwaway
ed25519 key and signs the real NEP-413 message with WebCrypto, so the full sign-in path
(page -> challenge -> signature -> server check -> session) runs with no real wallet.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from playwright.sync_api import expect, sync_playwright

from tests import fakes

pytestmark = pytest.mark.e2e

TEST_WALLET_JS = r"""
const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function b58(bytes) {
  let n = 0n; for (const b of bytes) n = n * 256n + BigInt(b);
  let s = ""; while (n > 0n) { s = B58[Number(n % 58n)] + s; n /= 58n; }
  for (const b of bytes) { if (b !== 0) break; s = "1" + s; } return s;
}
const u32 = (n) => { const a = new Uint8Array(4); new DataView(a.buffer).setUint32(0, n, true); return a; };
const str = (t) => { const d = new TextEncoder().encode(t); return [u32(d.length), d]; };
const cat = (parts) => { const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0)); let i = 0; for (const p of parts) { out.set(p, i); i += p.length; } return out; };
export class NearConnector {
  constructor() { this.handlers = {}; }
  on(name, fn) { (this.handlers[name] ||= []).push(fn); }
  async connect({ signMessageParams: p }) {
    const key = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
    const pub = new Uint8Array(await crypto.subtle.exportKey("raw", key.publicKey));
    const payload = cat([u32(2 ** 31 + 413), ...str(p.message), p.nonce, ...str(p.recipient), new Uint8Array([0])]);
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", payload));
    const sig = new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, key.privateKey, digest));
    const signedMessage = { accountId: "test.near", publicKey: "ed25519:" + b58(pub), signature: btoa(String.fromCharCode(...sig)) };
    for (const fn of this.handlers["wallet:signInAndSignMessage"] || []) fn({ accounts: [{ accountId: "test.near", signedMessage }] });
    return { signMessage: async () => signedMessage };
  }
  async disconnect() {}
}
"""


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    """The app at BASE_URL, or a local process against the fake APIs."""
    if os.environ.get("BASE_URL"):
        yield os.environ["BASE_URL"].rstrip("/"), False
        return
    fake = fakes.Fake()
    base = fake.start()
    tmp = tmp_path_factory.mktemp("e2e")
    port = free_port()
    env = {
        **os.environ,
        **fakes.env(base),
        "SESSION_KEY_FILE": str(tmp / "s.key"),
        "PUBLIC_HOST": f"127.0.0.1:{port}",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "intents_desk.cli",
            "--db",
            str(tmp / "e2e.duckdb"),
            "serve",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(150):  # wait until the first sync and feed are done
        try:
            with urllib.request.urlopen(f"{url}/api/feed", timeout=2) as r:
                if b'"feed_at": "' in r.read():
                    with urllib.request.urlopen(f"{url}/api/summary", timeout=2) as s:
                        if b'"synced_at": "' in s.read():
                            break
        except OSError:
            pass
        time.sleep(0.2)
    yield url, True
    proc.terminate()
    proc.wait(timeout=10)
    fake.stop()


@pytest.fixture
def page(app):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1440, "height": 900})
        pg.errors, pg.csp = [], []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and pg.errors.append(m.text))
        pg.add_init_script(
            "document.addEventListener('securitypolicyviolation', e => "
            "(window.__csp = window.__csp || []).push(e.violatedDirective + ' ' + e.blockedURI))"
        )
        yield pg
        browser.close()


def open_page(page, url):
    page.goto(url)
    page.wait_for_load_state("networkidle")


def test_page_loads_with_every_panel_and_no_errors(app, page):
    url, faked = app
    open_page(page, url)
    expect(page.locator("#pill")).not_to_have_text("Loading network…")
    expect(page.locator("img[alt='NEAR']")).to_be_visible()
    if faked:
        expect(page.locator("#partners")).to_contain_text("widget-x:pda")
        expect(page.locator("#partners .alert")).to_have_count(1)
        expect(page.locator("#feed")).to_contain_text(
            "My funds are still stuck <b>help</b>"
        )
    assert page.evaluate("window.__csp || []") == []
    assert page.errors == []


def test_swap_lookup_and_quote_preview(app, page):
    url, faked = app
    if not faked:
        pytest.skip("needs the fake 1Click swap")
    open_page(page, url)
    page.fill("#addr", fakes.REFUNDED)
    page.click("#lookup button[type=submit]")
    expect(page.locator("#swap")).to_contain_text("REFUNDED")
    expect(page.locator("#swap")).to_contain_text("QUOTE_EXPIRED")
    page.get_by_role("button", name="Preview a new quote").click()
    expect(page.locator("#swap")).to_contain_text("14945.25293")
    page.fill("#addr", "bad&addr")
    page.click("#lookup button[type=submit]")
    expect(page.locator("#swap")).to_contain_text("Not a valid deposit address")


def test_wallet_sign_in_watch_and_sign_out_with_test_wallet(app, page):
    url, faked = app
    if not faked:
        pytest.skip("the test wallet needs the fake RPC (FullAccess) behind the server")
    page.route(
        "**/static/near-connect.js",
        lambda r: r.fulfill(
            status=200, content_type="text/javascript", body=TEST_WALLET_JS
        ),
    )
    open_page(page, url)
    page.click("#connect")
    expect(page.locator("#who")).to_have_text("test.near")
    expect(page.locator("#mine-out")).to_contain_text("Balance5 NEAR")
    expect(page.locator("#mine-out")).to_contain_text("refunded to this account")
    page.fill("#addr", fakes.REFUNDED)
    page.click("#lookup button[type=submit]")
    page.get_by_role("button", name="Watch this swap").click()
    expect(page.locator("#mine-out")).to_contain_text("TTxfvD7a6bxi")
    page.click("#signout")
    expect(page.locator("#connect")).to_be_visible()
    page.reload()
    expect(page.locator("#who")).to_be_hidden()  # the session cookie is gone
    assert page.errors == []


def test_phone_layout_keyboard_and_layout_shift(app, page):
    url, _ = app
    page.set_viewport_size({"width": 375, "height": 812})
    page.add_init_script(
        "window.__cls = 0; new PerformanceObserver(l => { for (const e of l.getEntries())"
        " if (!e.hadRecentInput) window.__cls += e.value }).observe({type: 'layout-shift',"
        " buffered: true});"
    )
    open_page(page, url)
    page.wait_for_timeout(1500)
    assert page.evaluate("document.documentElement.scrollWidth") <= 375
    assert page.evaluate("window.__cls") < 0.1
    for _ in range(6):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.id") == "addr":
            break
    assert page.evaluate("document.activeElement.id") == "addr"
    assert page.evaluate("document.activeElement.matches(':focus-visible')")


@pytest.mark.live
def test_real_wallet_picker_opens(app, page):
    """Needs internet: NEAR Connect loads its wallet list from GitHub/jsDelivr."""
    url, _ = app
    open_page(page, url)
    page.click("#connect")
    expect(page.get_by_text("Select wallet")).to_be_visible(timeout=15000)
    assert page.evaluate("window.__csp || []") == []
