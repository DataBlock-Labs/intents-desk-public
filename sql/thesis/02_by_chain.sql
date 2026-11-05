-- Volume by origin chain, top 10 by USD in.
SELECT
    chain(origin_asset) AS origin_chain,
    count(*) AS swaps,
    round(sum(amount_in_usd)) AS usd_in,
    round(100 * sum(amount_in_usd) / sum(sum(amount_in_usd)) OVER (), 1) AS usd_share_pct
FROM silver.swap
GROUP BY 1 ORDER BY usd_in DESC NULLS LAST LIMIT 10;
