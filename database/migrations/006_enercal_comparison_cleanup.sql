
-- =====================================================
-- KONE FYP DATABASE
-- 006: NORMALISED ENERCAL COMPARISON VIEW
-- =====================================================

-- This view always displays the latest revision.
-- Review status comes from extraction_revisions.
-- It may display pending or rejected values.
-- It is NOT an approved-only reporting view.

CREATE OR REPLACE VIEW enercal_comparison_view AS

SELECT
    d.document_id,
    m.case_ref,
    d.filename,
    p.page_number AS source_page,
    t.technology_role,

    -- Elevator name
    MAX(c.raw_value) FILTER (
        WHERE c.field_name =
            t.technology_role || '_elevator_technology'
    ) AS elevator_technology,

    -- Annual energy
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            t.technology_role ||
            '_annual_energy_consumption'
    ) AS annual_energy_consumption_kwh_year,

    -- Regenerative energy
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            t.technology_role ||
            '_annual_regenerative_energy'
    ) AS annual_regenerative_energy_kwh_year,

    -- Annual electricity expenditure
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            t.technology_role ||
            '_annual_electricity_expenditure'
    ) AS annual_electricity_expenditure_bhd_year,

    -- Lifetime electricity expenditure
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            t.technology_role ||
            '_lifetime_electricity_expenditure'
    ) AS lifetime_electricity_expenditure_bhd,

    -- Operational emissions
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            t.technology_role ||
            '_operational_co2e'
    ) AS operational_co2e_kg_lifetime_20_years,

    -- Embodied emissions
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            t.technology_role ||
            '_embodied_co2e'
    ) AS embodied_co2e_kg,

    -- Reported saving
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            'reported_energy_saving_percent'
    ) AS reported_energy_saving_percent,

    -- Python calculated saving
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            'calculated_energy_saving_percent'
    ) AS calculated_energy_saving_percent,

    -- Authoritative revision number
    r.revision_no,

    -- Authoritative review status
    r.review_status::TEXT AS review_status

FROM source_documents d

JOIN modernisation_cases m
    ON m.case_id = d.case_id

-- Use only the latest revision for each document
JOIN extraction_revisions r
    ON r.document_id = d.document_id
    AND r.revision_no = (
        SELECT MAX(r2.revision_no)
        FROM extraction_revisions r2
        WHERE r2.document_id = d.document_id
    )

-- Use fields linked to that exact revision
JOIN extraction_candidates c
    ON c.extraction_revision_id = r.revision_id

JOIN document_pages p
    ON p.page_id = c.page_id
    AND p.document_id = d.document_id

CROSS JOIN (
    VALUES ('existing'), ('proposed')
) AS t(technology_role)

GROUP BY
    d.document_id,
    m.case_ref,
    d.filename,
    p.page_number,
    t.technology_role,
    r.revision_no,
    r.review_status

HAVING MAX(c.raw_value) FILTER (
    WHERE c.field_name =
        t.technology_role || '_elevator_technology'
) IS NOT NULL;
