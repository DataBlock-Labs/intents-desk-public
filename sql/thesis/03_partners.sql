-- Partners (1Click referral) by swaps, with USD and refund rate. Top 10.
SELECT
    coalesce(nullif(referral, ''), '(none)') AS partner,
    count(*) AS swaps,
    round(100 * count(*) / sum(count(*)) OVER (), 1) AS swap_share_pct,
    round(sum(amount_in_usd)) AS usd_in,
    round(100 * avg((status = 'REFUNDED')::INT), 1) AS refund_pct
FROM silver.swap
GROUP BY 1 ORDER BY swaps DESC LIMIT 10;
