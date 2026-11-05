-- The ':pda' incident, one row per referral id.
SELECT
    referral,
    strftime(min(created_at) AT TIME ZONE 'UTC', '%Y-%m-%d %H:%M UTC') AS first_seen,
    strftime(max(created_at) AT TIME ZONE 'UTC', '%Y-%m-%d %H:%M UTC') AS last_seen,
    count(*) AS swaps,
    round(100 * avg((status = 'REFUNDED')::INT), 1) AS refund_pct,
    round(sum(amount_in_usd) FILTER (WHERE status = 'REFUNDED')) AS refunded_usd,
    count(DISTINCT refund_to) AS senders,
    mode(refund_reason) AS top_reason
FROM silver.swap WHERE incident(referral)
GROUP BY 1 ORDER BY swaps DESC;
