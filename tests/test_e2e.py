"""End-to-end UAT in a real browser (Playwright): `pytest -m e2e` (premerge uat).

The app runs as a process against the fake APIs (tests/fakes.py). Wallet sign-in uses a
test wallet: the page's wallet library is swapped for a stand-in that makes a throwaway
ed25519 key and signs the real NEP-413 message with WebCrypto, so the full sign-in path
(page -> challenge -> signature -> server check -> session) runs with no real wallet.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from intents_desk import support
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
  constructor() { this.handlers = {}; window.__walletReady = true; }
  on(name, fn) { (this.handlers[name] ||= []).push(fn); }
  off(name, fn) { this.handlers[name] = (this.handlers[name] || []).filter((f) => f !== fn); }
  async connect({ signMessageParams: p }) {
    // What the page had done when it called the wallet (see WATCH_SIGN_IN_JS).
    (window.__connects ||= []).push({ inTap: window.__inTap === true, sentAtTap: window.__sentAtTap,
      sentAtCall: (window.__challenges || []).length, nonce: btoa(String.fromCharCode(...p.nonce)) });
    const key = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
    const pub = new Uint8Array(await crypto.subtle.exportKey("raw", key.publicKey));
    const payload = cat([u32(2 ** 31 + 413), ...str(p.message), p.nonce, ...str(p.recipient), new Uint8Array([0])]);
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", payload));
    const sig = new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, key.privateKey, digest));
    const signedMessage = { accountId: "test.near", publicKey: "ed25519:" + b58(pub), signature: btoa(String.fromCharCode(...sig)) };
    for (const fn of this.handlers["wallet:signInAndSignMessage"] || []) fn({ accounts: [{ accountId: "test.near", signedMessage }] });
    return this.wallet();
  }
  async wallet() {  // the connected wallet: records what the page asks it to send
    return {
      getAccounts: async () => [{ accountId: "test.near" }],
      signAndSendTransaction: async (tx) => { (window.__sent ||= []).push(tx);
        return { transaction: { hash: "8kY6pnEhGKNrDhX8kCWt8rfm8ZRZx4yNHjDSU7r1vAZt" } }; },
    };
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


def run_app(tmp_path_factory, seed=None, **extra):
    """Start the app as a process against the fake APIs; yield its URL.

    `seed(con)` fills the database before the server opens it."""
    fake = fakes.Fake()
    base = fake.start()
    tmp = tmp_path_factory.mktemp("e2e")
    if seed:
        from intents_desk.store import connect

        con = connect(tmp / "e2e.duckdb")
        seed(con)
        con.close()
    port = free_port()
    env = {
        **os.environ,
        **fakes.env(base),
        "RATE_PER_MIN": "600",  # one browser drives every test from one IP
        "SESSION_KEY_FILE": str(tmp / "s.key"),
        "PUBLIC_HOST": f"127.0.0.1:{port}",
        "INCIDENT_REPORTS_DIR": str(tmp / "reports"),
        "VAULT_INCIDENTS_DIR": "",
        **extra,
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


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    """The app at BASE_URL, or a local process against the fake APIs."""
    if os.environ.get("BASE_URL"):
        yield os.environ["BASE_URL"].rstrip("/"), False
        return
    yield from run_app(tmp_path_factory)


@pytest.fixture(scope="module")
def live_app(tmp_path_factory):
    """The app with live swaps on (SWAP_LIVE=1), against the fake APIs."""
    yield from run_app(tmp_path_factory, SWAP_LIVE="1")


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
    # the support case: answer, why, timeline and chain references
    expect(page.locator("#swap")).to_contain_text("Your funds came back: 15006.128946")
    expect(page.locator("#swap")).to_contain_text(support.REASONS["QUOTE_EXPIRED"][0])
    expect(page.locator("#swap")).to_contain_text(f"{fakes.NEAR_BLOCK:,}")
    tron = page.locator("#swap a", has_text="tronscan.org")
    expect(tron).to_have_attribute("rel", "noopener noreferrer")
    expect(page.get_by_role("button", name="Copy support message")).to_be_visible()
    with page.expect_download() as dl:
        page.get_by_role("button", name="Download .md").click()
    text = Path(dl.value.path()).read_text()
    assert "NEAR Intents swap: support case" in text and fakes.TRON_TX in text
    assert "0xabc" not in text and "TRefund" not in text  # no recipient or refund
    assert re.search(r"#case=[0-9a-f]{20}$", page.url)
    case_url = page.url
    page.get_by_role("button", name="Preview a new quote").click()
    expect(page.locator("#swap")).to_contain_text("14945.25293")
    page.fill("#addr", "bad&addr")
    page.click("#lookup button[type=submit]")
    expect(page.locator("#swap")).to_contain_text("Not a valid deposit address")
    # by the hash of the deposit tx: the same case, but no quote (no address shown)
    page.fill("#addr", fakes.TRON_TX)
    page.click("#lookup button[type=submit]")
    expect(page.locator("#swap")).to_contain_text("Your funds came back")
    expect(page.get_by_role("button", name="Preview a new quote")).to_have_count(0)
    assert fakes.REFUNDED not in page.locator("#swap").inner_text()
    # a case link opens the case on a fresh page, and the lookups are counted
    page.goto("about:blank")
    page.goto(case_url)
    expect(page.locator("#swap")).to_contain_text("Your funds came back")
    expect(page.locator("#asks")).to_contain_text("QUOTE_EXPIRED")


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
    page.click("text=Preview quote")  # new swap: fee shown, preview only
    expect(page.locator("#mine-out")).to_contain_text("half to datablock.near")
    expect(page.locator("#mine-out")).to_contain_text("Preview only for now")
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
    # Pick a web wallet: its code runs inline in a srcdoc iframe under our policy.
    with page.context.expect_page(timeout=20000) as login:
        page.get_by_text("MyNearWallet", exact=True).first.click()
        page.wait_for_timeout(3000)
    assert "mynearwallet" in login.value.url or login.value.url == "about:blank"
    blocked = [e for e in page.errors if "Content Security Policy" in e]
    assert blocked == []


def test_partner_view_and_recent_swaps(app, page):
    url, faked = app
    if not faked:
        pytest.skip("needs the fake partners")
    open_page(page, url)
    page.locator("#partners a", has_text="widget-x:pda").click()
    expect(page.locator("#partner-view")).to_be_visible()
    expect(page.locator("#partner-stats")).to_contain_text(
        "60 swaps in the last 24 h · 100% refunded"
    )
    expect(page.locator("#partner-reasons")).to_contain_text("INTENT_SUBMIT_FAILED")
    expect(page.locator("#swaps")).to_contain_text(
        "widget-x:pda"
    )  # filter follows the view
    page.select_option("#f-status", "SUCCESS")
    page.select_option("#f-partner", "")
    expect(page.locator("#swaps")).not_to_contain_text("REFUNDED")
    page.locator("#swaps button.addr").first.click()
    expect(page.locator("#swap")).to_contain_text("does not know this swap")  # by ref
    assert page.locator("#addr").input_value() == ""  # the page never held the address
    assert "dep0" not in page.locator("#swap").inner_text()  # nor did the error text
    page.click("#partner-close")
    expect(page.locator("#partner-view")).to_be_hidden()
    assert page.evaluate("window.__csp || []") == []


def test_account_lookup_lists_activity_five_per_page(app, page):
    url, faked = app
    if not faked:
        pytest.skip("needs the fake transactions API")
    open_page(page, url)
    page.fill("#addr", "alice.near")
    page.click("#lookup button[type=submit]")
    rows = page.locator("#swap table tr")
    expect(rows).to_have_count(6)  # header + 5 events
    expect(page.locator("#swap")).to_contain_text("Page 1 of 2 · 7 events")
    expect(
        page.locator("#swap td.s-SUCCESS").first
    ).to_be_visible()  # same status names
    page.get_by_role("button", name="Older").click()
    expect(rows).to_have_count(3)  # header + the last 2
    expect(page.get_by_role("button", name="Older")).to_be_disabled()
    page.get_by_role("button", name="Newer").click()
    swap = page.get_by_role("button", name=re.compile("Check swap a1b2c3d4"))
    expect(page.locator("#swap")).to_contain_text("REFUNDED")
    swap.click()
    expect(page.locator("#swap .answer")).to_contain_text("REFUNDED")


def test_report_publicly_opens_a_ticket_in_the_panel(app, page):
    url, faked = app
    if not faked:
        pytest.skip("needs the fake 1Click swap")
    open_page(page, url)
    page.fill("#addr", fakes.NEAR_REFUNDED)
    page.click("#lookup button[type=submit]")
    expect(page.locator("#swap .answer")).to_contain_text("REFUNDED")
    page.get_by_role("button", name="Report publicly").click()
    expect(page.locator("#swap")).to_contain_text(
        re.compile("Ticket opened|Already a ticket")
    )
    panel = page.locator("#tickets")
    expect(panel).to_contain_text("REFUNDED")
    expect(panel).to_contain_text("QUOTE_EXPIRED")
    assert fakes.NEAR_REFUNDED not in panel.inner_text()
    panel.get_by_role("button", name=re.compile("Open ticket")).first.click()
    expect(page.locator("#swap .answer")).to_contain_text("REFUNDED")


def test_page_shows_data_when_the_wallet_library_never_loads(app, page):
    url, faked = app
    page.route("**/static/near-connect.js", lambda r: r.abort())  # blocked or slow CDN
    open_page(page, url)
    expect(page.locator("#pill")).not_to_have_text("Loading network…")
    rows = page.locator("#swaps tr")
    if faked:
        expect(rows.first).to_be_visible()
        assert rows.count() <= 26  # 25 rows + header first; "Show 100" for more
        page.click("#swaps-more")
        expect(page.locator("#swaps-more")).to_be_hidden()


def sign_in_with_test_wallet(page, url):
    page.route(
        "**/static/near-connect.js",
        lambda r: r.fulfill(
            status=200, content_type="text/javascript", body=TEST_WALLET_JS
        ),
    )
    open_page(page, url)
    page.click("#connect")
    expect(page.locator("#who")).to_have_text("test.near")


def test_new_swap_sends_the_deposit_from_the_wallet(live_app, page):
    url, _ = live_app
    sign_in_with_test_wallet(page, url)
    page.fill("#ns-amount", "1")
    page.click("text=Get deposit address")
    page.get_by_role("button", name="Send 1 NEAR from your wallet").click()
    expect(page.get_by_role("button", name="Sent")).to_be_visible()
    sent = page.evaluate("window.__sent")
    assert sent[0]["receiverId"].startswith("dep")  # the deposit address of our quote
    assert sent[0]["actions"] == [
        {"type": "Transfer", "params": {"deposit": str(10**24)}}
    ]
    # USDC from the account: register the deposit account, then ft_transfer to it
    page.select_option("#ns-from", "USDC")
    page.fill("#ns-amount", "2")
    page.click("text=Get deposit address")
    page.get_by_role("button", name=re.compile(r"^Send .* from your wallet")).click()
    page.wait_for_function("(window.__sent || []).length === 2")
    usdc = page.evaluate("window.__sent")[1]
    assert usdc["receiverId"] == fakes.USDC_NEAR.removeprefix("nep141:")
    calls = [a["params"]["methodName"] for a in usdc["actions"]]
    assert calls == ["storage_deposit", "ft_transfer"]
    assert usdc["actions"][1]["params"]["args"]["amount"] == "2000000"
    assert page.errors == []


def test_recover_a_refund_at_todays_price(live_app, page):
    url, _ = live_app
    sign_in_with_test_wallet(page, url)
    page.fill("#addr", fakes.NEAR_REFUNDED)
    page.click("#lookup button[type=submit]")
    page.click("text=Price it again today")
    expect(page.locator("#swap")).to_contain_text("Then you were quoted")
    expect(page.locator("#swap")).to_contain_text("vs then")
    page.click("text=Recover at today's price")
    page.get_by_role("button", name=re.compile(r"^Send .* from your wallet")).click()
    sent = page.evaluate("window.__sent")
    assert sent[-1]["actions"][0]["params"]["deposit"] == str(2 * 10**24)
    assert page.errors == []


# Runs before the page's code. It logs every challenge request (and the nonce in its reply),
# and wraps the Connect tap handler: __inTap is true only until the handler's first await.
WATCH_SIGN_IN_JS = r"""
window.__challenges = []; window.__connects = []; window.__sentAtTap = 0;
const realFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  if (!String(args[0]).includes("/api/auth/challenge")) return realFetch(...args);
  const entry = { done: false, nonce: null }; window.__challenges.push(entry);
  const reply = await realFetch(...args);
  entry.nonce = (await reply.clone().json().catch(() => ({}))).nonce ?? null; entry.done = true;
  return reply;
};
const add = EventTarget.prototype.addEventListener;
EventTarget.prototype.addEventListener = function (type, fn, ...rest) {
  if (type !== "click" || this.id !== "connect") return add.call(this, type, fn, ...rest);
  return add.call(this, type, function (e) {
    window.__inTap = true; window.__sentAtTap = window.__challenges.length;
    try { return fn.call(this, e); } finally { window.__inTap = false; }
  }, ...rest);
};
"""


def watch_sign_in(page, faked):
    """Use the test wallet, and log each challenge request and each call to the wallet."""
    if not faked:
        pytest.skip("the test wallet needs the fake RPC (FullAccess) behind the server")
    page.add_init_script(WATCH_SIGN_IN_JS)
    page.route(
        "**/static/near-connect.js",
        lambda r: r.fulfill(
            status=200, content_type="text/javascript", body=TEST_WALLET_JS
        ),
    )


def wait_until(page, expression, seconds=10):
    """Poll from here: the page's CSP stops the eval that wait_for_function polls with."""
    deadline = time.monotonic() + seconds
    while not page.evaluate(expression):
        assert time.monotonic() < deadline, f"never true: {expression}"
        page.wait_for_timeout(50)


def challenge_in_hand(page, count=1):
    """Wait until the page has the wallet library and the reply to request `count`."""
    wait_until(page, "window.__walletReady === true")
    wait_until(
        page,
        f"window.__challenges.length === {count} && window.__challenges[{count - 1}].done",
    )
    page.wait_for_timeout(200)  # the page stores the reply just after the fetch ends


def sign_in_log(page):
    return page.evaluate("[window.__challenges, window.__connects]")


def test_connect_tap_calls_the_wallet_with_no_request_in_between(app, page):
    url, faked = app
    watch_sign_in(page, faked)
    open_page(page, url)
    challenge_in_hand(page)  # fetched on page load, before any tap
    page.click("#connect")
    expect(page.locator("#who")).to_have_text("test.near")
    challenges, (first,) = sign_in_log(page)
    # the wallet was called before the handler's first await, with no request after the tap
    assert first["inTap"] is True
    assert (first["sentAtTap"], first["sentAtCall"]) == (1, 1)
    assert [c["nonce"] for c in challenges] == [first["nonce"]]
    # signed out again: the used challenge is not kept, a new one is fetched for the next tap
    page.click("#signout")
    expect(page.locator("#connect")).to_be_visible()
    challenge_in_hand(page, 2)
    page.click("#connect")
    expect(page.locator("#who")).to_have_text("test.near")
    challenges, (_, second) = sign_in_log(page)
    assert second["inTap"] is True
    assert (second["sentAtTap"], second["sentAtCall"]) == (2, 2)
    assert [c["nonce"] for c in challenges] == [first["nonce"], second["nonce"]]
    assert second["nonce"] != first["nonce"]
    assert page.errors == []


def test_sign_in_fetches_a_challenge_after_the_tap_when_none_is_in_hand(app, page):
    url, faked = app
    watch_sign_in(page, faked)
    gate = {"open": False}  # the request on page load fails (a phone with no signal)
    page.route(
        "**/api/auth/challenge",
        lambda r: r.continue_() if gate["open"] else r.abort(),
    )
    open_page(page, url)
    wait_until(
        page, "window.__walletReady === true && window.__challenges.length === 1"
    )
    page.wait_for_timeout(200)
    gate["open"] = True
    page.click("#connect")  # the old order: fetch the challenge, then call the wallet
    expect(page.locator("#who")).to_have_text("test.near")
    challenges, (call,) = sign_in_log(page)
    assert call["inTap"] is False
    assert (call["sentAtTap"], call["sentAtCall"]) == (1, 2)
    assert call["nonce"] == challenges[1]["nonce"]


def test_challenge_is_renewed_and_an_old_one_is_never_used(app, page):
    url, faked = app
    watch_sign_in(page, faked)
    gate = {"open": True}
    page.route(
        "**/api/auth/challenge",
        lambda r: r.continue_() if gate["open"] else r.abort(),
    )
    page.clock.install()
    open_page(page, url)
    challenge_in_hand(page)
    page.clock.fast_forward(100_000)  # over 90 s old: the page gets a new one by itself
    challenge_in_hand(page, 2)
    gate["open"] = False  # the phone sleeps past the 150 s limit and the renewal fails
    page.clock.fast_forward(200_000)
    wait_until(page, "window.__challenges.length === 3")
    page.wait_for_timeout(200)
    gate["open"] = True
    page.click("#connect")  # the one in hand is too old: a new one, then the wallet
    expect(page.locator("#who")).to_have_text("test.near")
    challenges, (call,) = sign_in_log(page)
    assert (call["sentAtTap"], call["sentAtCall"]) == (3, 4)
    assert call["nonce"] == challenges[3]["nonce"]
    assert call["nonce"] not in (challenges[0]["nonce"], challenges[1]["nonce"])


def test_challenge_the_server_already_took_is_refused_then_replaced(app, page):
    url, faked = app
    watch_sign_in(page, faked)
    replies = []

    def challenge(route):  # the second request gets the first reply again (a replay)
        if len(replies) == 1:
            replies.append(replies[0])
            route.fulfill(status=200, content_type="application/json", body=replies[0])
        else:
            reply = route.fetch()
            replies.append(reply.text())
            route.fulfill(response=reply)

    page.route("**/api/auth/challenge", challenge)
    open_page(page, url)
    challenge_in_hand(page)
    page.click("#connect")
    expect(page.locator("#who")).to_have_text("test.near")
    page.click("#signout")
    expect(page.locator("#connect")).to_be_visible()
    challenge_in_hand(page, 2)
    page.click("#connect")  # a valid signature over a used nonce: the server says no
    expect(page.locator("#mine-out")).to_contain_text("already used")
    expect(page.locator("#who")).to_be_hidden()
    challenge_in_hand(page, 3)  # the page got a new one for the next tap
    page.click("#connect")
    expect(page.locator("#who")).to_have_text("test.near")
    _, (first, replayed, third) = sign_in_log(page)
    assert replayed["nonce"] == first["nonce"] != third["nonce"]
    assert third["inTap"] is True
    assert (third["sentAtTap"], third["sentAtCall"]) == (3, 3)


def seed_incidents(con):
    """A closed incident 3 days ago and an ongoing one in the last hour."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows = []
    for h in range(-80, 0):
        rows.append((now + timedelta(hours=h), "partner-good", "SUCCESS", 1000, None))
    for h in (-72, -71):
        rows.append(
            (
                now + timedelta(hours=h),
                "old-widget:pda",
                "REFUNDED",
                90,
                "QUOTE_EXPIRED",
            )
        )
        rows.append((now + timedelta(hours=h), "old-widget:pda", "FAILED", 10, None))
    rows.append(
        (
            now - timedelta(hours=1),
            "live-widget:pda",
            "REFUNDED",
            80,
            "INTENT_SUBMIT_FAILED",
        )
    )
    con.executemany(
        "INSERT INTO ops.hourly_swap_summary (hour, partner, origin_chain, destination_chain,"
        " status, swaps, usd_in, refunded_usd, top_refund_reason)"
        " VALUES (?, ?, 'arb', 'base', ?, ?, 0, NULL, ?)",
        rows,
    )
    from intents_desk import incidents

    incidents.detect(con)


@pytest.fixture(scope="module")
def incident_app(tmp_path_factory):
    yield from run_app(tmp_path_factory, seed=seed_incidents)


def test_past_incidents_list_and_detail(incident_app):
    url, _ = incident_app
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_page(page, url)
        live = page.locator("#incidents tr", has_text="live-widget:pda")
        expect(live).to_contain_text("ONGOING")
        old = page.locator("#incidents tr", has_text="old-widget:pda")
        expect(old).not_to_contain_text("ONGOING")
        expect(page.locator("#incidents")).to_contain_text("old-widget:pda")
        expect(page.locator("#incidents")).to_contain_text("QUOTE_EXPIRED")
        page.locator("#incidents a", has_text="old-widget:pda").click()
        expect(page.locator("#incident-view")).to_be_visible()
        expect(page.locator("#incident-stats")).to_contain_text("200 swaps")
        expect(page.locator("#incident-stats")).to_contain_text("20 failed")
        expect(page.locator("#incident-routes")).to_contain_text("arb → base")
        expect(page.locator("#incident-who")).to_contain_text("Solvers")
        expect(page.locator("#incident-repro-text")).to_contain_text(
            "referral=old-widget%3Apda"
        )
        with page.expect_download() as dl:
            page.click("#ev-json")
        assert json.loads(Path(dl.value.path()).read_text())["swaps"] == 200
        page.click("#incident-close")
        expect(page.locator("#incident-view")).to_be_hidden()
        assert errors == []
        browser.close()
