
-- =====================================================
-- KONE FYP DATABASE
-- 002: EnerCal PDF, Extraction and Engineering Review
-- =====================================================


-- =====================================================
-- TABLE 1: Source Documents
-- =====================================================
-- Stores original uploaded EnerCal PDF files.
-- Each PDF belongs to a modernisation case.
-- The same PDF cannot be uploaded twice to the same case.

CREATE TABLE IF NOT EXISTS source_documents (

    document_id BIGINT
        GENERATED ALWAYS AS IDENTITY
        PRIMARY KEY,

    case_id BIGINT NOT NULL
        REFERENCES modernisation_cases(case_id),

    filename TEXT NOT NULL,

    file_sha256 CHAR(64) NOT NULL,

    file_size_bytes BIGINT NOT NULL
        CHECK (file_size_bytes > 0),

    pdf_bytes BYTEA NOT NULL,

    uploaded_at TIMESTAMPTZ
        NOT NULL DEFAULT NOW(),

    UNIQUE (case_id, file_sha256)

);


-- =====================================================
-- TABLE 2: Document Pages
-- =====================================================
-- Stores page-by-page text extracted by PyMuPDF.
-- Each page belongs to an original PDF document.

CREATE TABLE IF NOT EXISTS document_pages (

    page_id BIGINT
        GENERATED ALWAYS AS IDENTITY
        PRIMARY KEY,

    document_id BIGINT NOT NULL
        REFERENCES source_documents(document_id),

    page_number INTEGER NOT NULL
        CHECK (page_number >= 1),

    extracted_text TEXT NOT NULL,

    extraction_method VARCHAR(30)
        NOT NULL DEFAULT 'pymupdf',

    UNIQUE (document_id, page_number)

);


-- =====================================================
-- TABLE 3: Extraction Candidates
-- =====================================================
-- Stores all extracted EnerCal comparison fields.
--
-- Each revision contains 16 fields:
-- 2 elevator technology names
-- 12 energy, expenditure and CO2e values
-- 2 energy-saving percentages
--
-- Rejected revisions are preserved.
-- Corrected values are submitted as a new revision.

CREATE TABLE IF NOT EXISTS extraction_candidates (

    candidate_id BIGINT
        GENERATED ALWAYS AS IDENTITY
        PRIMARY KEY,

    page_id BIGINT NOT NULL
        REFERENCES document_pages(page_id),

    field_name TEXT NOT NULL,

    raw_value TEXT NOT NULL,

    numeric_value NUMERIC(14,3),

    unit VARCHAR(50),

    review_status VARCHAR(20)
        NOT NULL DEFAULT 'pending'
        CHECK (
            review_status IN (
                'pending',
                'approved',
                'rejected'
            )
        ),

    revision_no INTEGER NOT NULL DEFAULT 1
        CHECK (revision_no >= 1),

    review_note TEXT,

    reviewed_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ
        NOT NULL DEFAULT NOW()

);


-- =====================================================
-- UPDATE EXISTING TABLE
-- =====================================================
-- The table already exists in our Codespaces database.
--
-- CREATE TABLE IF NOT EXISTS does not add new columns
-- to an existing table.
--
-- These ALTER TABLE statements add the revision fields
-- without removing existing extraction records.

ALTER TABLE extraction_candidates
ADD COLUMN IF NOT EXISTS revision_no INTEGER
    NOT NULL DEFAULT 1;

ALTER TABLE extraction_candidates
ADD COLUMN IF NOT EXISTS review_note TEXT;

ALTER TABLE extraction_candidates
ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;


-- =====================================================
-- UNIQUE INDEX: Extraction Revision
-- =====================================================
-- Prevents duplicate field names within one revision
-- of the same PDF page.
--
-- Examples:
-- Page 4, Revision 1, existing_annual_energy_consumption
-- Page 4, Revision 2, existing_annual_energy_consumption
--
-- Both are allowed because they are different revisions.

CREATE UNIQUE INDEX IF NOT EXISTS
    uq_extraction_candidate_revision

ON extraction_candidates (
    page_id,
    revision_no,
    field_name
);


-- =====================================================
-- VIEW 1: EnerCal Energy Comparison
-- =====================================================
-- Displays the latest revision as two elevator rows.
--
-- Existing elevator
-- Proposed elevator
--
-- Includes all six numeric comparison columns and
-- both energy-saving percentages.
--
-- Rejected revisions remain stored in the table.
-- This view displays the latest revision only.
--
-- IMPORTANT:
-- This view may contain pending or rejected data.
-- It is intended for preview and engineering review.

CREATE OR REPLACE VIEW enercal_comparison_view AS

SELECT

    -- Document information
    d.document_id,

    m.case_ref,

    d.filename,

    p.page_number AS source_page,

    -- Existing or proposed elevator
    r.technology_role,

    -- Elevator technology name
    MAX(c.raw_value) FILTER (
        WHERE c.field_name =
            r.technology_role || '_elevator_technology'
    ) AS elevator_technology,


    -- Column 1: Annual energy consumption
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            r.technology_role ||
            '_annual_energy_consumption'
    ) AS annual_energy_consumption_kwh_year,


    -- Column 2: Annual regenerative energy
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            r.technology_role ||
            '_annual_regenerative_energy'
    ) AS annual_regenerative_energy_kwh_year,


    -- Column 3: Annual electricity expenditure
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            r.technology_role ||
            '_annual_electricity_expenditure'
    ) AS annual_electricity_expenditure_bhd_year,


    -- Column 4: Lifetime electricity expenditure
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            r.technology_role ||
            '_lifetime_electricity_expenditure'
    ) AS lifetime_electricity_expenditure_bhd,


    -- Column 5: Operational CO2e emissions
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            r.technology_role ||
            '_operational_co2e'
    ) AS operational_co2e_kg_lifetime_20_years,


    -- Column 6: Embodied CO2e emissions
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            r.technology_role ||
            '_embodied_co2e'
    ) AS embodied_co2e_kg,


    -- Saving shown in EnerCal
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            'reported_energy_saving_percent'
    ) AS reported_energy_saving_percent,


    -- Saving calculated by our Python extractor
    MAX(c.numeric_value) FILTER (
        WHERE c.field_name =
            'calculated_energy_saving_percent'
    ) AS calculated_energy_saving_percent,


    -- New: Revision currently displayed
    MAX(c.revision_no) AS revision_no,


    -- New: Overall review status
    CASE
        WHEN COUNT(*) = 16
             AND BOOL_AND(c.review_status = 'approved')
        THEN 'approved'

        WHEN COUNT(*) = 16
             AND BOOL_AND(c.review_status = 'rejected')
        THEN 'rejected'

        WHEN COUNT(*) = 16
             AND BOOL_AND(c.review_status = 'pending')
        THEN 'pending'

        ELSE 'incomplete_or_mixed'
    END AS review_status


FROM source_documents d

JOIN modernisation_cases m
    ON d.case_id = m.case_id

JOIN document_pages p
    ON d.document_id = p.document_id

JOIN extraction_candidates c
    ON p.page_id = c.page_id

    -- Use only the latest revision on this page
    AND c.revision_no = (
        SELECT MAX(c2.revision_no)
        FROM extraction_candidates c2
        WHERE c2.page_id = p.page_id
    )


-- Generate two elevator roles
CROSS JOIN (
    VALUES ('existing'), ('proposed')
) AS r(technology_role)


-- Combine 16 individual fields into two rows
GROUP BY

    d.document_id,
    m.case_ref,
    d.filename,
    p.page_number,
    r.technology_role


-- Show rows with identified elevator technologies
HAVING MAX(c.raw_value) FILTER (
    WHERE c.field_name =
        r.technology_role || '_elevator_technology'
) IS NOT NULL;


-- =====================================================
-- VIEW 2: Approved EnerCal Comparison
-- =====================================================
-- Contains only the latest extraction revision
-- when its overall review status is approved.
--
-- Use this view for downstream calculations and reports
-- that require engineer-approved EnerCal extraction.
--
-- This does NOT mean actual energy savings are verified.
-- Actual savings require independent meter measurements.

CREATE OR REPLACE VIEW enercal_approved_comparison_view AS

SELECT *

FROM enercal_comparison_view

WHERE review_status = 'approved';
