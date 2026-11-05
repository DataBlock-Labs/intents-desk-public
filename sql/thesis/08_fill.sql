-- Fill time (1Click updatedAt - Explorer createdAt) for the sampled SUCCESS swaps.
SELECT
    count(*) AS sampled,
    round(quantile_cont(fill_s, 0.5)) AS p50_s,
    round(quantile_cont(fill_s, 0.95)) AS p95_s,
    round(max(fill_s)) AS max_s
FROM gold.fill_sample WHERE fill_s >= 0;
