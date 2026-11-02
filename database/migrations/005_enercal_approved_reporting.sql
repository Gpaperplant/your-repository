CREATE OR REPLACE VIEW
    enercal_approved_comparison_view AS

SELECT

    v.document_id,

    v.case_ref,

    v.filename,

    v.source_page,

    v.technology_role,

    v.elevator_technology,

    v.annual_energy_consumption_kwh_year,

    v.annual_regenerative_energy_kwh_year,

    v.annual_electricity_expenditure_bhd_year,

    v.lifetime_electricity_expenditure_bhd,

    v.operational_co2e_kg_lifetime_20_years,

    v.embodied_co2e_kg,

    v.reported_energy_saving_percent,

    v.calculated_energy_saving_percent,

    v.revision_no,

    r.review_status::TEXT AS review_status


FROM enercal_comparison_view v

JOIN extraction_revisions r
    ON r.document_id = v.document_id
    AND r.revision_no = v.revision_no


-- Only engineer-approved revisions
WHERE r.review_status = 'approved'


-- Only the latest revision of each document
AND r.revision_no = (

    SELECT MAX(r2.revision_no)

    FROM extraction_revisions r2

    WHERE r2.document_id = r.document_id

)


-- Require exactly 16 extracted fields
AND (

    SELECT COUNT(*)

    FROM extraction_candidates c

    WHERE c.extraction_revision_id = r.revision_id

) = 16


-- Require 16 distinct field names
AND (

    SELECT COUNT(DISTINCT c.field_name)

    FROM extraction_candidates c

    WHERE c.extraction_revision_id = r.revision_id

) = 16


-- All comparison fields must come from one PDF page
AND (

    SELECT COUNT(DISTINCT c.page_id)

    FROM extraction_candidates c

    WHERE c.extraction_revision_id = r.revision_id

) = 1;
