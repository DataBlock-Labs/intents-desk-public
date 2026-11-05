-- USD in during the last 24 hours of data (for the claim check).
SELECT round(sum(amount_in_usd)) AS usd_in_24h, count(*) AS swaps_24h
FROM silver.swap
WHERE created_at > (SELECT max(created_at) FROM silver.swap) - INTERVAL 24 HOUR;
