-- Refund reasons, with and without the incident partners.
SELECT
    coalesce(refund_reason, 'not given') AS reason,
    count(*) AS refunds,
    count(*) FILTER (WHERE NOT incident(referral)) AS refunds_without_incident,
    round(sum(amount_in_usd)) AS usd
FROM silver.swap WHERE status = 'REFUNDED'
GROUP BY 1 ORDER BY refunds DESC;
