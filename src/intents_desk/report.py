"""Thesis and partner reports: markdown with SVG charts, every number from a named SQL file.

Charts follow the dataviz rules: one axis, 2px lines, a legend plus direct labels for
two series, text in ink colors (never the series color), a <title> on every mark for
hover, and a dark-mode style chosen inside the SVG. Colors were checked with the
dataviz validator (blue #2a78d6 / orange #eb6834 light; #3987e5 / #d95926 dark).
"""

from datetime import UTC, datetime
from html import escape
from pathlib import Path

SQL = Path(__file__).resolve().parents[2] / "sql" / "thesis"
STYLE = """<style>
.bg{fill:#fcfcfb}.ink{fill:#0b0b0b}.muted{fill:#52514e}.grid{stroke:#e4e3df}
.s1{stroke:#2a78d6;fill:#2a78d6}.s2{stroke:#eb6834;fill:#eb6834}
@media (prefers-color-scheme:dark){.bg{fill:#1a1a19}.ink{fill:#f0efec}
.muted{fill:#c3c2b7}.grid{stroke:#383835}.s1{stroke:#3987e5;fill:#3987e5}
.s2{stroke:#d95926;fill:#d95926}}
.ln{fill:none!important}text{font:12px system-ui,sans-serif}.t{font-size:14px;font-weight:600}
</style>"""


def run(con, name: str) -> tuple[list[str], list[tuple]]:
    """Run sql/thesis/<name>.sql and return (column names, rows)."""
    con.execute((SQL / "00_macros.sql").read_text())
    cur = con.execute((SQL / f"{name}.sql").read_text())
    return [d[0] for d in cur.description], cur.fetchall()


def md_table(cols: list[str], rows: list[tuple]) -> str:
    def cell(v):  # referral ids come from anyone who makes a swap: escape them
        text = f"{v:,}" if isinstance(v, int | float) else str(v)
        return escape(text).replace("|", "\\|").replace("`", "'")

    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def svg(w: int, h: int, title: str, source: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}"'
        f' height="{h}" role="img" aria-label="{escape(title)}">{STYLE}'
        f'<rect class="bg" width="{w}" height="{h}" rx="6"/>'
        f'<text class="ink t" x="16" y="24">{escape(title)}</text>{body}'
        f'<text class="muted" x="16" y="{h - 10}">Source: {escape(source)}</text></svg>'
    )


def line_chart(labels: list[str], series: list[tuple], title: str, source: str) -> str:
    """Lines of percentages (0-100) over hourly labels. series: (name, values, css)."""
    w, h, left, top, right, bottom = 760, 320, 48, 44, 190, 56
    pw, ph = w - left - right, h - top - bottom
    n = max(len(labels) - 1, 1)

    def x(i):
        return left + pw * i / n

    def y(v):
        return top + ph * (1 - v / 100)

    body = []
    for v in (0, 25, 50, 75, 100):
        body.append(
            f'<line class="grid" x1="{left}" x2="{left + pw}" y1="{y(v)}" y2="{y(v)}"/>'
            f'<text class="muted" x="{left - 8}" y="{y(v) + 4}" text-anchor="end">{v}%'
            "</text>"
        )
    for i in range(0, len(labels), max(len(labels) // 6, 1)):
        body.append(
            f'<text class="muted" x="{x(i)}" y="{top + ph + 18}" text-anchor="middle">'
            f"{escape(labels[i])}</text>"
        )
    body.append(f'<text class="muted" x="{left}" y="{top + ph + 36}">Hour (UTC)</text>')
    for k, (name, values, css) in enumerate(series):
        pts = [(x(i), y(v)) for i, v in enumerate(values) if v is not None]
        path = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        body.append(f'<polyline class="{css} ln" stroke-width="2" points="{path}"/>')
        for (px, py), (lab, v) in zip(
            pts,
            [(lab, v) for lab, v in zip(labels, values, strict=True) if v is not None],
            strict=True,
        ):
            body.append(
                f'<circle class="{css}" cx="{px:.1f}" cy="{py:.1f}" r="5" opacity="0">'
                f"<title>{escape(lab)} UTC: {escape(name)} {v}%</title></circle>"
            )
        lx, ly = pts[-1]
        body.append(  # direct label at the line end, in ink color
            f'<rect class="{css}" x="{lx + 8}" y="{ly - 5}" width="10" height="3"/>'
            f'<text class="ink" x="{lx + 22}" y="{ly + 4}">{escape(name)} {values[-1]}%'
            "</text>"
        )
        body.append(  # legend
            f'<rect class="{css}" x="{w - 300 + 150 * k}" y="16" width="14" height="3"/>'
            f'<text class="muted" x="{w - 280 + 150 * k}" y="21">{escape(name)}</text>'
        )
    return svg(w, h, title, source, "".join(body))


def bar_chart(rows: list[tuple], title: str, source: str, unit: str = "%") -> str:
    """Horizontal bars, one series. rows: (label, value)."""
    w, row_h, left, top = 760, 26, 260, 44
    h = top + row_h * len(rows) + 40
    top_v = max(v for _, v in rows) or 1
    body = []
    for i, (label, v) in enumerate(rows):
        y0 = top + i * row_h
        bw = (w - left - 90) * v / top_v
        short = label if len(label) <= 36 else label[:34] + "…"
        body.append(
            f'<text class="ink" x="{left - 8}" y="{y0 + 16}" text-anchor="end">'
            f"{escape(short)}</text>"
            f'<rect class="s1" x="{left}" y="{y0 + 4}" width="{bw:.1f}" height="{row_h - 8}"'
            f' rx="4"><title>{escape(label)}: {v}{unit}</title></rect>'
            f'<text class="ink" x="{left + bw + 6}" y="{y0 + 16}">{v}{unit}</text>'
        )
    return svg(w, h, title, source, "".join(body))


def histogram(values: list[float], marks: dict, title: str, source: str) -> str:
    """Counts of values in 20 bins (seconds), with labelled marker lines."""
    w, h, left, top, bottom = 760, 310, 48, 62, 60
    pw, ph = w - left - 24, h - top - bottom
    hi = max(marks.values()) * 1.25
    bins = [0] * 20
    for v in values:
        bins[min(int(v / hi * 20), 19)] += 1
    peak = max(bins) or 1
    bw = pw / 20
    body = []
    for i, c in enumerate(bins):
        bh = ph * c / peak
        lo_s, hi_s = round(hi * i / 20), round(hi * (i + 1) / 20)
        label = f"{lo_s}-{hi_s} s" if i < 19 else f"{lo_s}+ s"
        body.append(
            f'<rect class="s1" x="{left + i * bw + 1:.1f}" y="{top + ph - bh:.1f}"'
            f' width="{bw - 2:.1f}" height="{bh:.1f}" rx="2">'
            f"<title>{label}: {c} swaps</title></rect>"
        )
    for i in range(0, 21, 5):
        body.append(
            f'<text class="muted" x="{left + i * bw}" y="{top + ph + 18}"'
            f' text-anchor="middle">{round(hi * i / 20)} s</text>'
        )
    for name, v in marks.items():
        mx = left + pw * min(v / hi, 1)
        body.append(
            f'<line class="s2" x1="{mx:.1f}" x2="{mx:.1f}" y1="{top}" y2="{top + ph}"'
            f' stroke-width="2"/><text class="ink" x="{mx:.1f}" y="{top - 6}"'
            f' text-anchor="middle">'
            f"{name} {round(v)} s</text>"
        )
    body.append(
        f'<text class="muted" x="{left}" y="{top + ph + 36}">'
        "Seconds from swap created to SUCCESS; bar height = swaps</text>"
    )
    return svg(w, h, title, source, "".join(body))


def write_thesis(con, out: str = "reports") -> str:
    """Write reports/thesis-<date>.md and its charts. Return the report path."""
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    folder = Path(out)
    (folder / "charts").mkdir(parents=True, exist_ok=True)

    _, [win] = run(con, "01_window")
    swaps, usd_in, first, last, hours = win
    chain_cols, chains = run(con, "02_by_chain")
    part_cols, partners = run(con, "03_partners")
    conc_cols, [conc] = run(con, "04_concentration")
    _, hourly = run(con, "05_hourly")
    reason_cols, reasons = run(con, "06_reasons")
    inc_cols, incident = run(con, "07_incident")
    _, [fill] = run(con, "08_fill")
    _, [last24] = run(con, "09_last24h")
    fills = [
        r[0] for r in con.execute("SELECT fill_s FROM gold.fill_sample").fetchall()
    ]

    charts = {
        "refund-rate": line_chart(
            [r[0] for r in hourly],
            [
                ("All swaps", [r[2] for r in hourly], "s1"),
                ("Without incident", [r[3] for r in hourly], "s2"),
            ],
            "Refunded share of 1Click swaps per hour",
            "sql/thesis/05_hourly.sql",
        ),
        "partners": bar_chart(
            [(r[0], r[2]) for r in partners],
            "Share of swaps by partner (top 10)",
            "sql/thesis/03_partners.sql",
        ),
    }
    if fills:
        charts["fill-time"] = histogram(
            fills,
            {"p50": fill[1], "p95": fill[2]},
            f"Fill time of {fill[0]} sampled successful swaps",
            "sql/thesis/08_fill.sql",
        )
    for name, body in charts.items():
        (folder / "charts" / f"{name}-{day}.svg").write_text(body)

    before = [r for r in hourly if r[3] is not None]
    inc_swaps = sum(r[3] for r in incident)
    inc_usd = sum(r[5] or 0 for r in incident)
    inc_refunded = sum(round(r[3] * r[4] / 100) for r in incident)
    inc_pct = round(100 * inc_refunded / inc_swaps, 1) if inc_swaps else 0
    clean = sorted(r[3] for r in before)
    clean_median = clean[len(clean) // 2] if clean else 0
    text = f"""# NEAR Intents (1Click): volume, reliability and one incident

Report date: {day}. Data window: {first} to {last} ({hours} h), {swaps:,} swaps,
${usd_in:,.0f} in. Built by Intents Desk (DataBlock) for NEAR HACKATHON. Every number
comes from a SQL file in `sql/thesis/`; run `make thesis` to rebuild this report.

## Summary

- **Volume:** ${last24[0]:,.0f} came in through 1Click in the last 24 hours of data
  ({last24[1]:,} swaps). Source: `09_last24h.sql`.
- **Reliability without the incident is good:** the median hour refunds {clean_median}% of
  swaps when the incident partners are left out. Source: `05_hourly.sql`.
- **One partner type failed at scale:** {len(incident)} wallet-widget referral ids of one partner that end in
  `:pda` sent {inc_swaps:,} swaps; {inc_pct}% were refunded (${inc_usd:,.0f} counted
  across retries, so the same funds appear many times). Source:
  `07_incident.sql`.
- **Fill time:** the median successful swap settles in {fill[1]:,.0f} s; 95% settle within
  {fill[2]:,.0f} s (sample of {fill[0]}). Source: `08_fill.sql`.
- **Concentration:** the top 5 partners carry {conc[0]}% of USD volume (HHI {conc[1]:,.0f}
  of 10,000). Source: `04_concentration.sql`.

## 1. Reliability

![Refunded share per hour](charts/refund-rate-{day}.svg)

The blue line is all swaps. The orange line removes the incident partners. The gap
between them is the incident. Hours with data: {len(before)}.

{md_table(reason_cols, reasons)}

Source: `06_reasons.sql`.

## 2. The `:pda` incident

{md_table(inc_cols, incident)}

Source: `07_incident.sql`. Few sender addresses and a very high swap count suggest an
integration or a bot that retries. Most refunds are `INTENT_SUBMIT_FAILED`: in this window,
{reasons[0][1] - reasons[0][2] if reasons and reasons[0][0] == "INTENT_SUBMIT_FAILED" else "n/a"}
of the {reasons[0][1] if reasons else 0:,} refunds with that reason came from the incident partners.
Because the same senders retry, the refunded USD counts the same funds many times; it
is not money lost. Refunds return the funds; the cost is failed swaps, fees and time.

## 3. Volume and partners

![Share of swaps by partner](charts/partners-{day}.svg)

{md_table(part_cols, partners)}

Source: `03_partners.sql`.

{md_table(chain_cols, chains)}

Source: `02_by_chain.sql`.

{md_table(conc_cols, [conc])}

Source: `04_concentration.sql`.

## 4. Fill time

{"![Fill time](charts/fill-time-" + day + ".svg)" if fills else "No sample yet: run `make fill-sample`."}

| sampled | p50 (s) | p95 (s) | max (s) |
|---|---|---|---|
| {fill[0]} | {fill[1]} | {fill[2]} | {fill[3]} |

Method: a fixed pseudo-random sample of SUCCESS swaps older than 1 hour. Fill time =
1Click status `updatedAt` minus Explorer `createdAt`. `updatedAt` is the last status
change, so this is an upper bound on the real settlement time.

## 5. Claim check

Public figures say more than $30B cumulative and about $4B a month. Our window shows
${last24[0]:,.0f} in 24 hours, about ${last24[0] * 30 / 1e9:,.1f}B a month at that rate.

What this window can show: 1Click swaps only, for {hours} hours. What it cannot show:
volume outside 1Click (direct `intents.near` use by solvers and apps), earlier months, or
private-shard swaps. A gap between the two figures is expected; it is not evidence
that the public figure is wrong.

## Method and limits

- Source: NEAR Intents Explorer API (every 1Click swap, with status and USD values),
  1Click status API (for fill times). Raw responses are kept in `bronze.explorer_tx`.
- A swap is counted once (keyed by deposit address and memo); a status change replaces
  the row.
- The window is short ({hours} h). Hourly rates in hours with few swaps are noisy.
- Not financial advice.
"""
    path = folder / f"thesis-{day}.md"
    path.write_text(text)
    return str(path)


def write_partner_report(con, referral: str, out: str = "reports") -> str:
    """Write a short report for the partners whose referral matches `referral`."""
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    Path(out).mkdir(parents=True, exist_ok=True)
    cur = con.execute(
        """
        SELECT strftime(date_trunc('hour', created_at) AT TIME ZONE 'UTC', '%m-%d %H:00')
                AS hour_utc,
            count(*) AS swaps,
            round(100 * avg((status = 'REFUNDED')::INT), 1) AS refund_pct,
            round(sum(amount_in_usd) FILTER (WHERE status = 'REFUNDED')) AS refunded_usd
        FROM silver.swap WHERE referral LIKE ?
        GROUP BY date_trunc('hour', created_at) ORDER BY date_trunc('hour', created_at)
        """,
        [referral],
    )
    cols, hourly = [d[0] for d in cur.description], cur.fetchall()
    cur = con.execute(
        """
        SELECT referral, count(*) AS swaps,
            round(100 * avg((status = 'REFUNDED')::INT), 1) AS refund_pct,
            count(DISTINCT refund_to) AS senders, mode(refund_reason) AS top_reason,
            strftime(min(created_at) AT TIME ZONE 'UTC', '%Y-%m-%d %H:%M UTC') AS first_seen
        FROM silver.swap WHERE referral LIKE ? GROUP BY 1 ORDER BY 2 DESC
        """,
        [referral],
    )
    id_cols, ids = [d[0] for d in cur.description], cur.fetchall()
    [(others,)] = con.execute(
        "SELECT round(100 * avg((status = 'REFUNDED')::INT), 1) FROM silver.swap"
        " WHERE coalesce(referral, '') NOT LIKE ?",
        [referral],
    ).fetchall()
    text = f"""# Partner report: referral `{referral}` ({day})

Prepared by Intents Desk (DataBlock) from the public NEAR Intents Explorer API.
Check it yourself: Explorer API `GET /api/v0/transactions?referral=<id>`.

## Referral ids

{md_table(id_cols, ids)}

All other swaps in the same window: {others}% refunded.

## Per hour

{md_table(cols, hourly)}

Sender addresses are counted, not listed. Not financial advice.
"""
    slug = referral.strip("%").replace(":", "") or "all"
    path = Path(out) / f"partner-{slug}-{day}.md"
    path.write_text(text)
    return str(path)
