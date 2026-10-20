/* Canonical position snapshot. One row per position; sensitivities stay in sensitivities.sql
   so that a multi-factor instrument cannot have its market value counted twice.

   Reference data is joined with LEFT JOIN on purpose. An INNER JOIN on active_flag would
   silently drop a matured or suspended instrument that is still held, and because the
   control totals are calculated from this same result the loss would reconcile cleanly.
   Unresolved or inactive master data is flagged here and quarantined downstream instead.

   Materialise this as an immutable, date-partitioned staging table before reading controls:
     INSERT INTO risk_stage.position_enriched SELECT * FROM ( <this query> ) s;
*/
WITH latest_position AS (
    SELECT p.*, ROW_NUMBER() OVER (PARTITION BY p.as_of_date,p.fund_id,p.portfolio_id,p.instrument_id ORDER BY p.source_updated_at DESC) rn
    FROM front_office.position_eod p WHERE p.as_of_date = :as_of_date AND p.position_status = 'VALID'
), latest_price AS (
    SELECT px.*, ROW_NUMBER() OVER (PARTITION BY px.price_date,px.instrument_id ORDER BY px.price_timestamp DESC) rn
    FROM market_data.instrument_price px WHERE px.price_date = :as_of_date
), latest_fx AS (
    SELECT fx.*, ROW_NUMBER() OVER (PARTITION BY fx.rate_date,fx.from_currency,fx.to_currency ORDER BY fx.rate_timestamp DESC) rn
    FROM market_data.fx_rate fx WHERE fx.rate_date = :as_of_date AND fx.to_currency = 'USD'
)
SELECT p.as_of_date, p.fund_id,
       COALESCE(f.fund_name, p.fund_id)        AS fund_name,
       p.portfolio_id, p.instrument_id,
       COALESCE(i.asset_class, 'UNCLASSIFIED') AS asset_class,
       p.currency,
       p.quantity * px.clean_price * COALESCE(fx.fx_rate, 1.0) AS market_value_usd,
       CASE WHEN px.clean_price IS NULL THEN 1 ELSE 0 END AS missing_price_flag,
       CASE WHEN fx.fx_rate IS NULL AND p.currency <> 'USD' THEN 1 ELSE 0 END AS missing_fx_flag,
       CASE WHEN f.fund_id IS NULL THEN 1 ELSE 0 END AS missing_fund_master_flag,
       CASE WHEN i.instrument_id IS NULL THEN 1 ELSE 0 END AS missing_instrument_master_flag,
       CASE WHEN COALESCE(f.active_flag, 0) = 0 THEN 1 ELSE 0 END AS inactive_fund_flag,
       CASE WHEN COALESCE(i.active_flag, 0) = 0 THEN 1 ELSE 0 END AS inactive_instrument_flag
FROM latest_position p
LEFT JOIN risk_master.fund f       ON f.fund_id = p.fund_id
LEFT JOIN risk_master.instrument i ON i.instrument_id = p.instrument_id
LEFT JOIN latest_price px          ON px.instrument_id = p.instrument_id AND px.rn = 1
LEFT JOIN latest_fx fx             ON fx.from_currency = p.currency AND fx.rn = 1
WHERE p.rn = 1;
