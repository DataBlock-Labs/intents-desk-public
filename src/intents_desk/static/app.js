
const $ = (id) => document.getElementById(id);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; };
const fmt = (n) => Number(n).toLocaleString("en-US");
const usd = (n) => n == null || n === "" ? "–" : "$" + Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 });
const ago = (s) => { const m = Math.round((Date.now() - new Date(s)) / 60000); return m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };
const chain = (a) => { if (!a) return "–"; if (a.startsWith("1cs_v1:")) return a.split(":")[1]; const r = a.split(":").slice(1).join(":");
  if (r.endsWith(".omft.near")) return r.split("-")[0].split(".")[0]; if (r.includes("omni.hot.tg")) return "hot-omni"; return "near"; };
const statusCell = (td, s) => { td.textContent = s; if (/^[A-Z_]+$/.test(s)) td.classList.add("s-" + s); };
const getJSON = async (url, opts) => { const r = await fetch(url, opts); const d = await r.json().catch(() => ({})); return [r.ok, d]; };
const post = (url, body) => getJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function table(t, head, rows, fill) {
  t.replaceChildren();
  const tr = t.insertRow();
  head.forEach(([h, num]) => { const th = el("th", h, num ? "n" : ""); th.scope = "col"; tr.append(th); });
  rows.forEach((r) => { const row = t.insertRow(); r.forEach((v, i) => { const td = row.insertCell(); if (head[i][1]) td.className = "n"; fill ? fill(td, v, i, r) : (td.textContent = v); }); });
}

const NEXT = {
  SUCCESS: "The swap is complete.",
  PROCESSING: "The swap is in progress. Check again in 10 minutes.",
  PENDING_DEPOSIT: "1Click is waiting for your deposit. Send it before the quote deadline.",
  INCOMPLETE_DEPOSIT: "Your deposit was less than the quote. The rest is refunded to your refund address.",
  REFUNDED: "Your funds went back to your refund address. You can get a new quote below.",
  FAILED: "The swap failed. Contact NEAR Intents support with this deposit address.",
};

// ---------- Swap lookup ----------
$("lookup").addEventListener("submit", async (e) => {
  e.preventDefault();
  const address = $("addr").value.trim(), out = $("swap");
  out.textContent = "Checking…";
  const [ok, d] = await getJSON("/api/swap?address=" + encodeURIComponent(address));
  if (!ok || !d.status) { out.replaceChildren(el("p", d.error || d.message || "1Click does not know this address.", "dim")); return; }
  renderSwap(d, address);
});

// Show one swap. `address` is null for swaps opened from the public list (no watch/quote).
function renderSwap(d, address) {
  const out = $("swap"); out.replaceChildren();
  const q = d.quoteResponse?.quote ?? {}, r = d.quoteResponse?.quoteRequest ?? {}, s = d.swapDetails ?? {};
  const dl = el("dl");
  [["Status", d.status], ["Route", `${chain(r.originAsset)} → ${chain(r.destinationAsset)}`],
   ["Sent", q.amountInFormatted ? `${q.amountInFormatted} (${usd(q.amountInUsd)})` : "–"],
   ["Received", s.amountOutFormatted && s.amountOutFormatted !== "0" ? `${s.amountOutFormatted} (${usd(s.amountOutUsd)})` : "–"],
   ["Refunded", s.refundedAmountFormatted && s.refundedAmountFormatted !== "0" ? `${s.refundedAmountFormatted} (${usd(s.refundedAmountUsd)})` : "–"],
   ["Reason", s.refundReason || "–"], ["Last update", d.updatedAt ? new Date(d.updatedAt).toLocaleString() : "–"],
  ].forEach(([k, v]) => { const dd = el("dd"); k === "Status" ? statusCell(dd, v) : (dd.textContent = v); dl.append(el("dt", k), dd); });
  out.append(dl, el("p", NEXT[d.status] || "", "next"));
  if (me && address) out.append(watchButton(address));
  const mine = me && address && d.status === "REFUNDED" && r.refundTo === me && String(r.originAsset).startsWith("nep141:");
  if (mine) out.append(recoverBox(address));
  else if (address && d.status === "REFUNDED" && r.depositType === "ORIGIN_CHAIN") out.append(swapAgain(address));
}

function swapAgain(address) {
  const box = el("div", null, "card"); box.style.marginTop = "var(--s3)";
  const note = el("p", "New quote through NEAR Intents 1Click, with a DataBlock fee shared 50/50 with 1Click. You send from your own wallet. DataBlock never holds your funds. Not financial advice.", "dim small");
  const btn = el("button", "Preview a new quote"); btn.type = "button";
  const res = el("div"); res.setAttribute("aria-live", "polite");
  btn.addEventListener("click", async () => {
    btn.disabled = true; res.textContent = "Quoting…";
    const [ok, q] = await getJSON(`/api/quote?address=${encodeURIComponent(address)}&dry=1`);
    btn.disabled = false; res.replaceChildren();
    if (!ok || !q.quote) { res.textContent = q.error || q.message || "No quote."; return; }
    const dl = el("dl");
    [["You send", `${q.quote.amountInFormatted} (${usd(q.quote.amountInUsd)})`],
     ["You get (est.)", `${q.quote.amountOutFormatted} (${usd(q.quote.amountOutUsd)})`],
     ["Minimum you get", (Number(q.quote.amountOutFormatted) * Number(q.quote.minAmountOut) / Number(q.quote.amountOut)).toLocaleString("en-US", { maximumFractionDigits: 6 })],
     ["Time", q.quote.timeEstimate + " s"], ["Fee", `${q.fee.bps / 100}% total, shared with 1Click`],
    ].forEach(([k, v]) => dl.append(el("dt", k), el("dd", v)));
    res.append(dl, el("p", "Preview only. Sending funds from this page comes in a later version.", "dim small"));
  });
  box.append(el("h2", "Swap again"), note, btn, res);
  return box;
}

// ---------- Network health ----------
async function loadHealth() {
  const [ok, d] = await getJSON("/api/summary");
  if (!ok) { $("pill").textContent = d.error || "Network data unavailable"; return; }
  if (!d.swaps) { $("pill").textContent = "No swap data yet"; $("window").textContent = "Network health needs the Explorer API key (see README)."; return; }
  const refunded = d.status.find(([s]) => s === "REFUNDED")?.[2] ?? 0;
  $("pill").textContent = `${fmt(d.swaps)} newest swaps · ${refunded}% refunded`;
  $("window").textContent = `${new Date(d.first).toLocaleTimeString()}–${new Date(d.last).toLocaleTimeString()} · ${usd(d.usd_in)} in` + (d.synced_at ? ` · synced ${ago(d.synced_at)}` : "");
  table($("status"), [["Status"], ["Swaps", 1], ["Share", 1]], d.status.map(([s, c, p]) => [s, fmt(c), p + "%"]), (td, v, i) => i === 0 ? statusCell(td, v) : (td.textContent = v));
  table($("reasons"), [["Reason"], ["Refunds", 1]], d.refund_reasons.map(([r, c]) => [r, fmt(c)]));
  table($("routes"), [["From → to"], ["Swaps", 1], ["Refunded", 1]], d.routes.map(([r, c, p]) => [r, fmt(c), p + "%"]));
  const [, parts] = await getJSON("/api/partners");
  table($("partners"), [["Partner"], ["Swaps", 1], ["Refunded", 1], ["Refunded $", 1]], (parts || []).map((p) => [p, p[1], p[2], p[3]]), (td, v, i, row) => {
    if (i === 0) { const p = row[0]; if (p[7]) td.append(el("span", "ALERT", "alert"), " ");
      const a = el("a", p[0].length > 34 ? p[0].slice(0, 32) + "…" : p[0], "small"); a.href = "#partner=" + encodeURIComponent(p[0]); a.title = "Open the partner view for " + p[0]; td.append(a); }
    else td.textContent = i === 1 ? fmt(v) : i === 2 ? v + "%" : usd(v);
  });
  const [, hours] = await getJSON("/api/hourly");
  $("bars").replaceChildren(...(hours || []).map(([h, n, p]) => { const b = el("span"); b.style.height = Math.max(p, 2) + "%"; b.title = `${h}: ${p}% of ${fmt(n)} swaps refunded`; return b; }));
  if (hours?.length) { const f = hours[0], l = hours[hours.length - 1];
    $("bars-axis").replaceChildren(el("span", `${f[0].slice(11, 16)} UTC: ${f[2]}%`), el("span", `bar height = % refunded (0–100)`), el("span", `${l[0].slice(11, 16)} UTC: ${l[2]}%`)); }
  table($("hourly"), [["Hour (UTC)"], ["Swaps", 1], ["Refunded", 1]], (hours || []).map(([h, n, p]) => [h, fmt(n), p + "%"]));
  fillSelect($("f-partner"), (parts || []).map((p) => p[0]));
  fillSelect($("f-route"), d.routes.map(([r]) => r));
}

function fillSelect(sel, values) {
  const keep = sel.value;
  sel.replaceChildren(el("option", "All"), ...values.map((v) => el("option", v)));
  sel.options[0].value = "";
  if (values.includes(keep)) sel.value = keep;
}

// ---------- Partner view (#partner=<id>) ----------
async function showPartner() {
  const m = location.hash.match(/^#partner=(.+)$/), box = $("partner-view");
  if (!m) { box.classList.add("hidden"); return; }
  const id = decodeURIComponent(m[1]);
  const [ok, d] = await getJSON("/api/partner?id=" + encodeURIComponent(id));
  box.classList.remove("hidden");
  $("partner-name").textContent = id;
  if (!ok) { $("partner-stats").textContent = d.error || "Partner not found."; return; }
  $("partner-stats").textContent = d.swaps_24h
    ? `${fmt(d.swaps_24h)} swaps in the last 24 h · ${d.refund_pct_24h}% refunded · ${usd(d.refunded_usd_24h)} refunded (counted across retries)`
    : "No swaps from this partner in the last 24 hours.";
  $("partner-bars").replaceChildren(...d.hourly.map(([h, n, p]) => { const b = el("span"); b.style.height = Math.max(p, 2) + "%"; b.title = `${h}: ${p}% of ${fmt(n)} swaps refunded`; return b; }));
  table($("partner-routes"), [["From → to"], ["Swaps", 1], ["Refunded", 1]], d.routes.map(([r, n, p]) => [r, fmt(n), p + "%"]));
  table($("partner-reasons"), [["Reason"], ["Refunds", 1]], d.reasons.map(([r, n]) => [r, fmt(n)]));
  table($("partner-statuses"), [["Status"], ["Swaps", 1]], d.statuses.map(([s, n]) => [s, fmt(n)]), (td, v, i) => i === 0 ? statusCell(td, v) : (td.textContent = v));
  table($("partner-daily"), [["Day (UTC)"], ["Swaps", 1], ["Refunded", 1], ["Refunded $", 1]], d.daily.map(([day, n, p, u]) => [day, fmt(n), (p ?? 0) + "%", usd(u)]));
  $("f-partner").value = id; loadSwaps();
  box.scrollIntoView({ block: "start" });
}
window.addEventListener("hashchange", showPartner);
$("partner-close").addEventListener("click", (e) => { e.preventDefault(); history.replaceState(null, "", location.pathname); showPartner(); });

// ---------- Recent swaps ----------
let swapLimit = 25;  // a short first table on phones; "Show 100" for more
async function loadSwaps() {
  const q = new URLSearchParams({ limit: String(swapLimit) });
  for (const [k, id] of [["status", "f-status"], ["partner", "f-partner"], ["route", "f-route"]]) if ($(id).value) q.set(k, $(id).value);
  const [ok, rows] = await getJSON("/api/swaps?" + q);
  if (!ok) { $("swaps-note").textContent = rows.error || "Swaps unavailable"; return; }
  $("swaps-note").textContent = `${rows.length} newest` + (q.size > 1 ? " (filtered)" : "");
  table($("swaps"), [["When"], ["Status"], ["Route"], ["In $", 1], ["Partner"], ["Reason"], ["Deposit address"]],
    rows.map((s) => [new Date(s.created_at).toLocaleTimeString(), s.status, s.route, usd(s.usd_in), s.partner.length > 24 ? s.partner.slice(0, 22) + "…" : s.partner, s.reason || "", s]),
    (td, v, i) => {
      if (i === 1) statusCell(td, v);
      else if (i === 6) { const b = el("button", v.short, "addr"); b.type = "button"; b.title = "Check this swap";
        b.setAttribute("aria-label", "Check swap " + v.short);
        b.addEventListener("click", () => lookupRef(v.ref, v.short)); td.append(b); }
      else td.textContent = v;
    });
}
for (const id of ["f-status", "f-partner", "f-route"]) $(id).addEventListener("change", loadSwaps);
$("swaps-more").addEventListener("click", () => { swapLimit = 100; $("swaps-more").classList.add("hidden"); loadSwaps(); });

// A swap from the public list: the page never gets its full address, only a ref.
async function lookupRef(ref, short) {
  $("addr").value = ""; $("lookup-h").scrollIntoView();
  const out = $("swap"); out.replaceChildren(el("p", `Checking ${short}…`, "dim"));
  const [ok, d] = await getJSON("/api/swap?ref=" + encodeURIComponent(ref));
  if (!ok || !d.status) { out.replaceChildren(el("p", d.error || d.message || `1Click does not know swap ${short}.`, "dim")); return; }
  renderSwap(d, null);
}

// ---------- Feed ----------
async function loadFeed(tag = "") {
  const [ok, d] = await getJSON("/api/feed" + (tag ? "?tag=" + tag : ""));
  const ul = $("feed"); ul.replaceChildren();
  if (!ok) { ul.append(el("li", d.error || "Feed unavailable.", "dim")); return; }
  $("feed-note").textContent = (d.feed_sources?.length ? "From " + d.feed_sources.join(" and ") : "") + (d.feed_at ? ` · updated ${ago(d.feed_at)}` : " · loading…");
  if (!d.posts.length) ul.append(el("li", "No posts yet. The feed updates every hour.", "dim"));
  for (const p of d.posts) {
    const li = el("li"), meta = el("div", null, "small dim");
    const a = el("a", p.source === "x" ? "@" + p.author : "u/" + p.author); a.href = p.url; a.target = "_blank"; a.rel = "noopener noreferrer";
    meta.append(p.source === "x" ? "X · " : "Reddit · ", a, ` · ${ago(p.created_at)}` + (p.tags.length ? " · " + p.tags.join(", ") : ""));
    li.append(meta, el("p", p.text.length > 400 ? p.text.slice(0, 400) + "…" : p.text));
    ul.append(li);
  }
}
document.querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-pressed", String(c === b)));
  loadFeed(b.dataset.tag);
}));

// ---------- Wallet sign-in (NEP-413 signed message; no transaction) ----------
let me = null;
// The wallet library (61 KB, plus its wallet list and frames) loads after the page shows data.
let ncPromise = null;
const connector = () => (ncPromise ??= import("/static/near-connect.js").then(
  (m) => new m.NearConnector({ network: "mainnet", features: { signMessage: true },
    cspNonce: document.querySelector("script[nonce]")?.nonce })));  // wallet code runs inline

function showAccount(account) {
  me = account;
  $("connect").classList.toggle("hidden", !!account);
  $("who").classList.toggle("hidden", !account); $("who").textContent = account || "";
  $("signout").classList.toggle("hidden", !account);
}

async function loadMine() {
  const [ok, d] = await getJSON("/api/me");
  if (!ok || !d.account) { showAccount(null); return; }
  showAccount(d.account);
  const out = $("mine-out"); out.replaceChildren();
  const dl = el("dl"); dl.append(el("dt", "Account"), el("dd", d.account), el("dt", "Balance"), el("dd", `${d.balance_near} NEAR`)); out.append(dl);
  out.append(telegramRow(d.telegram));
  out.append(swapForm(d));
  out.append(el("h2", "Watched swaps"));
  if (!d.watched.length) out.append(el("p", "None yet. Check a swap, then press Watch.", "dim small"));
  for (const w of d.watched) { const p = el("p", null, "mono small"); p.append(el("strong", w.address.slice(0, 12) + "… "), w.line); p.append(" ", watchButton(w.address, false)); out.append(p); }
  out.append(el("h2", "Swaps sent or refunded to this account (last data window)"));
  if (!d.swaps.length) { out.append(el("p", "No swaps found for this account in the stored data.", "dim small")); return; }
  const t = el("table"), wrap = el("div", null, "scroll"); wrap.append(t); out.append(wrap);
  table(t, [["When"], ["Status"], ["Route"], ["In $", 1], ["Reason"]], d.swaps.map((s) => [new Date(s[1]).toLocaleString(), s[2], `${chain(s[3])} → ${chain(s[4])}`, usd(s[5]), s[6] || ""]), (td, v, i) => i === 1 ? statusCell(td, v) : (td.textContent = v));
}

// ---------- Send the deposit from the connected wallet (the wallet asks you to approve) ----------
let swapLive = false;
async function walletFor(account) {
  const nc = await connector();
  const w = (await nc.wallet().catch(() => null)) || (await nc.connect());
  const on = (await w.getAccounts())?.[0]?.accountId;
  if (on !== account) throw new Error(`Your wallet is on ${on || "no account"}. Sign in to it as ${account}.`);
  return w;
}

// NEAR: a plain transfer. Other NEAR tokens: register the deposit account, then ft_transfer.
function depositTx(q, origin) {
  if (origin === "nep141:wrap.near") return { receiverId: q.depositAddress, actions: [{ type: "Transfer", params: { deposit: q.amountIn } }] };
  const call = (methodName, args, deposit) => ({ type: "FunctionCall", params: { methodName, args, gas: "30000000000000", deposit } });
  return { receiverId: origin.slice("nep141:".length), actions: [
    call("storage_deposit", { account_id: q.depositAddress, registration_only: true }, "1250000000000000000000"),
    call("ft_transfer", { receiver_id: q.depositAddress, amount: q.amountIn }, "1"),
  ] };
}

function sendButton(q, origin, label, status) {
  const b = el("button", `Send ${label} from your wallet`); b.type = "button";
  b.addEventListener("click", async () => {
    b.disabled = true; status.textContent = "Approve the transfer in your wallet…";
    try {
      const out = await (await walletFor(me)).signAndSendTransaction(depositTx(q, origin));
      const hash = out?.transaction?.hash ?? out?.transaction_outcome?.id;
      if (hash) await post("/api/me/swap/sent", { address: q.depositAddress, txHash: hash });
      b.textContent = "Sent"; pollStatus(q.depositAddress, status);
    } catch (err) { b.disabled = false; status.textContent = "Not sent: " + (err?.message || err); }
  });
  return b;
}

async function pollStatus(address, line) {
  for (let i = 0; i < 120; i++) {  // every 10 s, for up to 20 minutes
    const r = await fetch(`/api/swap?address=${encodeURIComponent(address)}&format=agent`);
    const text = await r.text(); line.textContent = text;
    if (/SUCCESS|REFUNDED|FAILED/.test(text)) { loadMine(); return; }
    await new Promise((ok) => setTimeout(ok, 10000));
  }
}

// ---------- Recover a refunded swap at today's price (refund came back to this account) ----------
function recoverBox(address) {
  const box = el("div", null, "card"); box.style.marginTop = "var(--s3)";
  const res = el("div"); res.setAttribute("aria-live", "polite");
  const ask = async (dry) => {
    res.replaceChildren(el("p", dry ? "Pricing it today…" : "Getting a deposit address…", "dim"));
    const [ok, r] = await post("/api/me/recover", { address, dry });
    if (!ok) { res.replaceChildren(el("p", r.error || "Could not quote.", "dim")); return; }
    const q = r.quote, dl = el("dl");
    const then = Number(r.then?.amountOutUsd), now = Number(q.amountOutUsd);
    const diff = then && now ? ` (${now >= then ? "+" : ""}${(100 * (now - then) / then).toFixed(1)}% vs then)` : "";
    [["Then you were quoted", `${r.then?.amountOutFormatted ?? "?"} (${usd(r.then?.amountOutUsd)})`],
     ["Today you get (about)", `${q.amountOutFormatted} (${usd(q.amountOutUsd)})${diff}`],
     ["You send again", `${q.amountInFormatted} (${usd(q.amountInUsd)})`],
     ["Fee", `${r.fee.bps / 100}%: half to ${r.fee.recipient}, half to 1Click`]].forEach(([k, v]) => dl.append(el("dt", k), el("dd", v)));
    res.replaceChildren(dl);
    if (dry && swapLive) { const go = el("button", "Recover at today's price"); go.type = "button"; go.addEventListener("click", () => ask(false)); res.append(go); }
    if (!dry && q.depositAddress) { const status = el("p", null, "mono small"); res.append(sendButton(q, r.origin, q.amountInFormatted, status), status); }
  };
  const btn = el("button", "Price it again today"); btn.type = "button"; btn.addEventListener("click", () => ask(true));
  box.append(el("h2", "Recover this refund"), el("p", "The refund came back to your account. Swap it again at today's price, to the same recipient. You approve the transfer in your wallet.", "dim small"), btn, res);
  return box;
}

// ---------- New swap from your own account (fee: 0.40%, half to datablock.near) ----------
function swapForm(d) {
  const box = el("div", null, "newswap");
  box.append(el("h2", "New swap"));
  const sel = (id, val) => { const s = el("select"); s.id = id; for (const t of ["NEAR", "USDC"]) { const o = el("option", t); o.value = t; s.append(o); } s.value = val; return s; };
  const from = sel("ns-from", "NEAR"), to = sel("ns-to", "USDC");
  const amount = el("input"); amount.id = "ns-amount"; amount.inputMode = "decimal"; amount.value = "1"; amount.autocomplete = "off";
  const lab = (text, input) => { const l = el("label", text + " "); l.htmlFor = input.id; l.append(input); return l; };
  from.addEventListener("change", () => { to.value = from.value === "NEAR" ? "USDC" : "NEAR"; });
  to.addEventListener("change", () => { from.value = to.value === "NEAR" ? "USDC" : "NEAR"; });
  const row = el("div", null, "filters"); row.append(lab("From", from), lab("To", to), lab("Amount", amount)); box.append(row);
  const result = el("div"); result.setAttribute("aria-live", "polite");
  const ask = async (dry) => {
    result.replaceChildren(el("p", dry ? "Getting a quote…" : "Getting a deposit address…", "dim"));
    const [ok, r] = await post("/api/me/swap", { from: from.value, to: to.value, amount: amount.value.trim(), dry });
    if (!ok) { result.replaceChildren(el("p", r.error || "Quote failed.", "dim")); return; }
    const q = r.quote, dl = el("dl");
    const item = (k, v, cls) => dl.append(el("dt", k), el("dd", v, cls));
    item("You send", `${q.amountInFormatted ?? amount.value} ${from.value}`);
    item("You get (about)", `${q.amountOutFormatted ?? "?"} ${to.value}`);
    item("Fee", `${r.fee.bps / 100}% of what you send: half to ${r.fee.recipient}, half to 1Click`);
    item("To and refunds", d.account);
    if (q.deadline) item("Quote valid until", new Date(q.deadline).toUTCString());
    result.replaceChildren(dl);
    if (!dry && q.depositAddress) {
      item("Deposit address", q.depositAddress, "mono");
      const copy = el("button", "Copy address", "ghost"); copy.type = "button";
      copy.addEventListener("click", () => navigator.clipboard?.writeText(q.depositAddress).then(() => { copy.textContent = "Copied"; }));
      const status = el("p", null, "mono small"); status.setAttribute("aria-live", "polite");
      result.append(el("p", `Send exactly ${q.amountInFormatted} ${from.value} from ${d.account} before the deadline. Nothing moves until you send.`),
        sendButton(q, r.origin, `${q.amountInFormatted} ${from.value}`, status), " ", copy, status);
    }
  };
  const preview = el("button", "Preview quote"); preview.type = "button"; preview.addEventListener("click", () => ask(true));
  const btns = el("p"); btns.append(preview);
  swapLive = !!d.swap_live;
  if (d.swap_live) { const live = el("button", "Get deposit address", "ghost"); live.type = "button"; live.addEventListener("click", () => ask(false)); btns.append(" ", live); }
  else btns.append(el("span", " Preview only for now: live swaps are off.", "dim small"));
  box.append(btns, result);
  if (d.quotes?.length) {
    box.append(el("h2", "Your swaps from this page"));
    for (const qq of d.quotes) { const p = el("p", null, "mono small"); p.append(el("strong", qq.address.slice(0, 12) + "… "), qq.line); box.append(p); }
  }
  return box;
}

function telegramRow(tg) {
  const p = el("p", null, "small");
  if (!tg?.available) { p.append(el("span", "Telegram alerts are not set up on this server.", "dim")); return p; }
  if (tg.linked) {
    const b = el("button", "Disconnect", "ghost"); b.type = "button";
    b.addEventListener("click", async () => { await post("/api/me/telegram/unlink", {}); loadMine(); });
    p.append("Telegram alerts: on. You get a message when a watched swap changes. ", b);
    return p;
  }
  const b = el("button", "Connect Telegram"); b.type = "button";
  b.addEventListener("click", async () => {
    b.disabled = true; const [ok, r] = await post("/api/me/telegram", {}); b.disabled = false;
    if (!ok) { p.append(" ", el("span", r.error || "Could not make a link.", "dim")); return; }
    const a = el("a", "Open Telegram and press Start"); a.href = r.link; a.target = "_blank"; a.rel = "noopener noreferrer";
    p.replaceChildren(a, el("span", " (link works once, for 10 minutes; then reload this page)", "dim"));
  });
  p.append("Get a Telegram message when a watched swap changes. ", b);
  return p;
}

function watchButton(address, on = true) {
  const b = el("button", on ? "Watch this swap" : "Stop watching", on ? "" : "ghost"); b.type = "button"; b.style.marginTop = "var(--s2)";
  b.addEventListener("click", async () => { b.disabled = true; const [ok, d] = await post("/api/me/watch", { address, on }); b.disabled = false; b.textContent = ok ? (on ? "Watching" : "Removed") : d.error; loadMine(); });
  return b;
}

// Show page errors on screen: phones have no console to read.
for (const ev of ["error", "unhandledrejection"]) window.addEventListener(ev, (e) => {
  const msg = e.reason?.message || e.message || String(e.reason || "unknown error");
  $("mine-out").append(el("p", "Page error: " + msg, "dim"));
});
let signingIn = false;
$("connect").addEventListener("click", async () => {
  const btn = $("connect");
  if (signingIn) { location.reload(); return; }  // second tap cancels a wallet that never answers
  signingIn = true; btn.textContent = "Waiting for wallet… (tap to cancel)";
  let timer;
  try {
    const [, ch] = await getJSON("/api/auth/challenge");
    const params = { message: ch.message, recipient: ch.recipient, nonce: Uint8Array.from(atob(ch.nonce), (c) => c.charCodeAt(0)) };
    const step = (t) => $("mine-out").replaceChildren(el("p", t, "dim"));
    step("Loading the wallet list…");
    const nc = await connector();
    step("Pick a wallet in the list. It opens your wallet to sign one message (no fee).");
    // connect() lists only wallets that sign in and sign the message in one step, and it
    // emits this event before it returns. So one prompt only: no second signMessage popup.
    let signed = null;
    const keep = (e) => { signed = e.accounts?.[0]?.signedMessage ?? null; };
    nc.on("wallet:signInAndSignMessage", keep);
    const noAnswer = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("No answer from the wallet in 2 minutes. Try again, or pick another wallet.")), 120000); });
    try { await Promise.race([nc.connect({ signMessageParams: params }), noAnswer]); } finally { nc.off("wallet:signInAndSignMessage", keep); }
    if (!signed) throw new Error("The wallet did not return a signed message.");
    const [ok, d] = await post("/api/auth/verify", { signed, nonce: ch.nonce });
    if (!ok) throw new Error(d.error || "Sign-in failed.");
    await loadMine();
  } catch (err) {
    $("mine-out").replaceChildren(el("p", "Sign-in did not finish: " + (err?.message || err), "dim"));
  } finally { clearTimeout(timer); signingIn = false; btn.textContent = "Connect wallet"; }
});

$("signout").addEventListener("click", async () => {
  await post("/api/auth/logout", {});
  try { if (ncPromise) await (await ncPromise).disconnect(); } catch { /* no wallet session */ }
  showAccount(null);
  $("mine-out").replaceChildren(el("p", "Signed out.", "dim"));
});

loadHealth().catch((e) => { $("pill").textContent = "Could not load: " + e; });
loadFeed();
loadMine();
loadSwaps();
const whenVisible = (fn) => () => { if (!document.hidden) fn(); };  // no polling in a background tab
setInterval(whenVisible(loadHealth), 60000);
setInterval(whenVisible(loadSwaps), 60000);
document.addEventListener("visibilitychange", whenVisible(() => { loadHealth(); loadSwaps(); }));
(window.requestIdleCallback || ((f) => setTimeout(f, 1500)))(() => connector().catch(() => {}));
showPartner();
