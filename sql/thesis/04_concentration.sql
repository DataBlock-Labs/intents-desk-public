-- Partner concentration by USD in: top-5 share and HHI (0-10,000).
WITH p AS (
    SELECT coalesce(nullif(referral, ''), '(none)') AS partner,
        sum(amount_in_usd) AS usd
    FROM silver.swap GROUP BY 1
), s AS (SELECT partner, 100 * usd / sum(usd) OVER () AS share FROM p)
SELECT
    round(sum(share) FILTER (WHERE rn <= 5), 1) AS top5_usd_share_pct,
    round(sum(share * share)) AS hhi_usd,
    count(*) AS partners
FROM (SELECT *, row_number() OVER (ORDER BY share DESC) AS rn FROM s);
