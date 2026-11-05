-- Refund rate per UTC hour: all swaps, and all swaps except the incident partners.
SELECT
    strftime(date_trunc('hour', created_at) AT TIME ZONE 'UTC', '%m-%d %H:00') AS hour,
    count(*) AS swaps,
    round(100 * avg((status = 'REFUNDED')::INT), 1) AS refund_pct,
    round(100 * avg((status = 'REFUNDED')::INT) FILTER (WHERE NOT incident(referral)), 1)
        AS refund_pct_without_incident
FROM silver.swap
GROUP BY date_trunc('hour', created_at) ORDER BY date_trunc('hour', created_at);
