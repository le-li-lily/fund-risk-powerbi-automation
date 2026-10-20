/* One row per instrument/risk factor. Do not join this to positions in SQL. */
SELECT as_of_date, instrument_id, risk_factor, factor_exposure_usd
FROM risk_engine.instrument_factor_sensitivity_usd
WHERE as_of_date = :as_of_date AND calculation_status = 'APPROVED';
