-- The data window: swaps, USD in, first and last swap, hours covered.
SELECT
    count(*) AS swaps,
    round(sum(amount_in_usd)) AS usd_in,
    strftime(min(created_at) AT TIME ZONE 'UTC', '%Y-%m-%d %H:%M UTC') AS first_swap,
    strftime(max(created_at) AT TIME ZONE 'UTC', '%Y-%m-%d %H:%M UTC') AS last_swap,
    round(epoch(max(created_at) - min(created_at)) / 3600, 1) AS hours
FROM silver.swap;
