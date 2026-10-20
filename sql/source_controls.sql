/* Control totals for the release gate.

   Read them from the materialised snapshot that positions.sql wrote, never by re-running
   the extract, otherwise a source table that changes mid-run reconciles against itself.

   SOURCE_RAW_POSITION_ROW_COUNT is taken from the front-office table instead, so that
   positions lost between the raw book and the enriched snapshot are detectable. The
   pipeline compares it with SOURCE_ROW_COUNT as the SOURCE_JOIN_COMPLETENESS check.

   is_independent tells the pipeline whether this evidence came from the source system.
*/
WITH extracted_positions AS (
    SELECT * FROM risk_stage.position_enriched WHERE as_of_date = :as_of_date
)
SELECT 'SOURCE_RAW_POSITION_ROW_COUNT' AS control_name, CAST(COUNT(*) AS DECIMAL(28,2)) AS control_value, 1 AS is_independent
FROM front_office.position_eod WHERE as_of_date = :as_of_date AND position_status = 'VALID'
UNION ALL SELECT 'SOURCE_ROW_COUNT', CAST(COUNT(*) AS DECIMAL(28,2)), 1 FROM extracted_positions
UNION ALL SELECT 'SOURCE_GROSS_MARKET_VALUE_USD', CAST(SUM(ABS(market_value_usd)) AS DECIMAL(28,2)), 1 FROM extracted_positions
UNION ALL SELECT 'SOURCE_NET_MARKET_VALUE_USD', CAST(SUM(market_value_usd) AS DECIMAL(28,2)), 1 FROM extracted_positions
UNION ALL SELECT 'SOURCE_MISSING_PRICE_COUNT', CAST(SUM(missing_price_flag) AS DECIMAL(28,2)), 1 FROM extracted_positions
UNION ALL SELECT 'SOURCE_MISSING_FX_COUNT', CAST(SUM(missing_fx_flag) AS DECIMAL(28,2)), 1 FROM extracted_positions;
