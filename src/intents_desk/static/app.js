import { NearConnector } from "/static/near-connect.js";

const $ = (id) => document.getElementById(id);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; };
const fmt = (n) => Number(n).toLocaleString("en-US");
const usd = (n) => n == null || n === "" ? "–" : "$" + Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 });
const ago = (s) => { const m = Math.round((Date.now() - new Date(s)) / 60000); return m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };
const chain = (a) => { if (!a) return "–"; if (a.startsWith("1cs_v1:")) return a.split(":")[1]; const r = a.split(":").slice(1).join(":");
  if (r.endsWith(".omft.near")) return r.split("-")[0].split(".")[0]; if (r.includes("omni.hot.tg")) return "hot-omni"; if (r.endsWith(".near")) return "near"; return r.split(".")[0]; };
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
  out.replaceChildren();
  if (!ok || !d.status) { out.append(el("p", d.error || d.message || "1Click does not know this address.", "dim")); return; }
  const q = d.quoteResponse?.quote ?? {}, r = d.quoteResponse?.quoteRequest ?? {}, s = d.swapDetails ?? {};
  const dl = el("dl");
  [["Status", d.status], ["Route", `${chain(r.originAsset)} → ${chain(r.destinationAsset)}`],
   ["Sent", q.amountInFormatted ? `${q.amountInFormatted} (${usd(q.amountInUsd)})` : "–"],
   ["Received", s.amountOutFormatted && s.amountOutFormatted !== "0" ? `${s.amountOutFormatted} (${usd(s.amountOutUsd)})` : "–"],
   ["Refunded", s.refundedAmountFormatted && s.refundedAmountFormatted !== "0" ? `${s.refundedAmountFormatted} (${usd(s.refundedAmountUsd)})` : "–"],
   ["Reason", s.refundReason || "–"], ["Last update", d.updatedAt ? new Date(d.updatedAt).toLocaleString() : "–"],
  ].forEach(([k, v]) => { const dd = el("dd"); k === "Status" ? statusCell(dd, v) : (dd.textContent = v); dl.append(el("dt", k), dd); });
  out.append(dl, el("p", NEXT[d.status] || "", "next"));
  if (me) out.append(watchButton(address));
  if (d.status === "REFUNDED" && r.depositType === "ORIGIN_CHAIN") out.append(swapAgain(address));
});

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
  table($("partners"), [["Partner"], ["Swaps", 1], ["Refunded", 1], ["Lost $", 1]], (parts || []).map((p) => [p, p[1], p[2], p[3]]), (td, v, i, row) => {
    if (i === 0) { const p = row[0]; if (p[7]) td.append(el("span", "ALERT", "alert"), " "); td.append(el("span", p[0].length > 34 ? p[0].slice(0, 32) + "…" : p[0], "mono small")); td.title = p[0]; }
    else td.textContent = i === 1 ? fmt(v) : i === 2 ? v + "%" : usd(v);
  });
  const [, hours] = await getJSON("/api/hourly");
  $("bars").replaceChildren(...(hours || []).map(([h, n, p]) => { const b = el("span"); b.style.height = Math.max(p, 2) + "%"; b.title = `${h}: ${p}% of ${fmt(n)} swaps refunded`; return b; }));
  if (hours?.length) { const f = hours[0], l = hours[hours.length - 1];
    $("bars-axis").replaceChildren(el("span", `${f[0].slice(11, 16)} UTC: ${f[2]}%`), el("span", `bar height = % refunded (0–100)`), el("span", `${l[0].slice(11, 16)} UTC: ${l[2]}%`)); }
  table($("hourly"), [["Hour (UTC)"], ["Swaps", 1], ["Refunded", 1]], (hours || []).map(([h, n, p]) => [h, fmt(n), p + "%"]));
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
const nc = new NearConnector({ network: "mainnet", features: { signMessage: true } });

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
  out.append(el("h2", "Watched swaps"));
  if (!d.watched.length) out.append(el("p", "None yet. Check a swap, then press Watch.", "dim small"));
  for (const w of d.watched) { const p = el("p", null, "mono small"); p.append(el("strong", w.address.slice(0, 12) + "… "), w.line); p.append(" ", watchButton(w.address, false)); out.append(p); }
  out.append(el("h2", "Swaps sent or refunded to this account (last data window)"));
  if (!d.swaps.length) { out.append(el("p", "No swaps found for this account in the stored data.", "dim small")); return; }
  const t = el("table"), wrap = el("div", null, "scroll"); wrap.append(t); out.append(wrap);
  table(t, [["When"], ["Status"], ["Route"], ["In $", 1], ["Reason"]], d.swaps.map((s) => [new Date(s[1]).toLocaleString(), s[2], `${chain(s[3])} → ${chain(s[4])}`, usd(s[5]), s[6] || ""]), (td, v, i) => i === 1 ? statusCell(td, v) : (td.textContent = v));
}

function watchButton(address, on = true) {
  const b = el("button", on ? "Watch this swap" : "Stop watching", on ? "" : "ghost"); b.type = "button"; b.style.marginTop = "var(--s2)";
  b.addEventListener("click", async () => { b.disabled = true; const [ok, d] = await post("/api/me/watch", { address, on }); b.disabled = false; b.textContent = ok ? (on ? "Watching" : "Removed") : d.error; loadMine(); });
  return b;
}

$("connect").addEventListener("click", async () => {
  const btn = $("connect"); btn.disabled = true; btn.textContent = "Waiting for wallet…";
  try {
    const [, ch] = await getJSON("/api/auth/challenge");
    const params = { message: ch.message, recipient: ch.recipient, nonce: Uint8Array.from(atob(ch.nonce), (c) => c.charCodeAt(0)) };
    const signedEvent = new Promise((res) => nc.on("wallet:signInAndSignMessage", (e) => res(e.accounts?.[0]?.signedMessage)));
    const wallet = await nc.connect({ signMessageParams: params });
    let signed = await Promise.race([signedEvent, new Promise((r) => setTimeout(() => r(null), 1500))]);
    if (!signed) signed = await wallet.signMessage(params);
    const [ok, d] = await post("/api/auth/verify", { signed, nonce: ch.nonce });
    if (!ok) throw new Error(d.error || "Sign-in failed.");
    await loadMine();
  } catch (err) {
    $("mine-out").replaceChildren(el("p", "Sign-in did not finish: " + (err?.message || err), "dim"));
  } finally { btn.disabled = false; btn.textContent = "Connect wallet"; }
});

$("signout").addEventListener("click", async () => {
  await post("/api/auth/logout", {});
  try { await nc.disconnect(); } catch { /* no wallet session */ }
  showAccount(null);
  $("mine-out").replaceChildren(el("p", "Signed out.", "dim"));
});

loadHealth().catch((e) => { $("pill").textContent = "Could not load: " + e; });
loadFeed();
loadMine();
setInterval(loadHealth, 60000);
