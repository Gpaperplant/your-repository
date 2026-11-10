
-- Expose approved MOD KPIs with report-specific units for Power BI.
BEGIN;

-- Preserve the existing comparison views for application compatibility.
CREATE OR REPLACE VIEW enercal_approved_kpis_view AS
WITH report_inputs AS (
    SELECT
        document_id,
        MAX(field_value) FILTER (WHERE field_name = 'country') AS country,
        MAX(field_value) FILTER (WHERE field_name = 'report_date') AS report_date,
        MAX(field_value) FILTER (WHERE field_name = 'electricity_price') AS price_text,
        MAX(unit) FILTER (WHERE field_name = 'electricity_price') AS price_unit,
        MAX(field_value) FILTER (WHERE field_name = 'lifetime_years') AS lifetime_text
    FROM enercal_specifications
    WHERE field_name IN ('country', 'report_date', 'electricity_price', 'lifetime_years')
    GROUP BY document_id
)
SELECT
    c.document_id,
    c.case_ref,
    c.filename,
    c.revision_no,
    c.source_page,
    c.technology_role,
    c.elevator_technology,
    c.annual_energy_consumption_kwh_year AS annual_energy_kwh_year,
    c.annual_regenerative_energy_kwh_year AS regenerative_energy_kwh_year,
    c.annual_electricity_expenditure_bhd_year AS annual_electricity_cost_per_year,
    c.lifetime_electricity_expenditure_bhd AS lifetime_electricity_cost,
    c.operational_co2e_kg_lifetime_20_years AS operational_co2e_kg_lifetime,
    c.embodied_co2e_kg,
    c.reported_energy_saving_percent,
    c.calculated_energy_saving_percent,
    i.country,
    i.report_date,
    CASE
        WHEN i.price_text ~ '^[0-9]+(\.[0-9]+)?$'
        THEN i.price_text::numeric
    END AS electricity_price_per_kwh,
    i.price_unit AS electricity_price_unit,
    CASE
        WHEN i.price_unit LIKE '%/kWh'
        THEN NULLIF(split_part(i.price_unit, '/', 1), '')
    END AS currency_symbol,
    CASE
        WHEN i.lifetime_text ~ '^[0-9]+$'
        THEN i.lifetime_text::integer
    END AS lifetime_years
FROM enercal_approved_comparison_view AS c
LEFT JOIN report_inputs AS i USING (document_id);

COMMIT;
