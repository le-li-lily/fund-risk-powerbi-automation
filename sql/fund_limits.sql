/* Approved limits in force on the reporting date, one row per fund.

   Limits belong to the limit-management system, not to pipeline configuration: they are
   approved per fund, change on their own cycle, and must be auditable. The pipeline fails
   the FUNDS_WITHOUT_LIMITS check rather than falling back to a default.
*/
SELECT l.as_of_date, l.fund_id, l.var_limit_usd, l.stressed_var_limit_usd, l.gross_exposure_limit_usd
FROM (
    SELECT fl.fund_id,
           CAST(:as_of_date AS DATE) AS as_of_date,
           fl.var_limit_usd, fl.stressed_var_limit_usd, fl.gross_exposure_limit_usd,
           ROW_NUMBER() OVER (PARTITION BY fl.fund_id ORDER BY fl.effective_from DESC) rn
    FROM risk_master.fund_limit fl
    WHERE fl.limit_status = 'APPROVED'
      AND fl.effective_from <= CAST(:as_of_date AS DATE)
      AND (fl.effective_to IS NULL OR fl.effective_to >= CAST(:as_of_date AS DATE))
) l
WHERE l.rn = 1;
