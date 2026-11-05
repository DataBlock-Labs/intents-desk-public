-- Short chain name from a 1Click asset id (same rules as web.chain_of).
CREATE OR REPLACE TEMP MACRO chain(a) AS (
    CASE
        WHEN a LIKE '1cs_v1:%' THEN split_part(a, ':', 2)
        WHEN regexp_replace(a, '^[^:]*:', '') LIKE '%.omft.near'
            THEN split_part(split_part(regexp_replace(a, '^[^:]*:', ''), '-', 1), '.', 1)
        WHEN a LIKE '%omni.hot.tg%' THEN 'hot-omni'
        ELSE 'near'  -- any other token id lives on NEAR
    END
);
-- Incident partners: wallet-widget referral ids of one partner that end in ':pda'.
CREATE OR REPLACE TEMP MACRO incident(r) AS (coalesce(r, '') LIKE '%:pda');
