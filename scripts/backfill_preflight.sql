BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

-- Read-only inventory of legacy evidence not yet present in the unified tables.
SELECT evidence_type, case_ref, legacy_document_id, historical_revisions,
       source_value_rows, file_bytes, checksum_matches, planned_classification
FROM (
    SELECT
        'MOD PDF'::text AS evidence_type,
        m.case_ref,
        s.document_id AS legacy_document_id,
        (SELECT COUNT(*) FROM extraction_revisions r
         WHERE r.document_id = s.document_id) AS historical_revisions,
        (SELECT COUNT(*) FROM extraction_candidates c
         JOIN extraction_revisions r ON r.revision_id = c.extraction_revision_id
         WHERE r.document_id = s.document_id) AS source_value_rows,
        OCTET_LENGTH(s.pdf_bytes) AS file_bytes,
        (s.file_sha256 = ENCODE(SHA256(s.pdf_bytes), 'hex')) AS checksum_matches,
        CASE WHEN e.elevator_ref LIKE 'TEST-LIFT-%'
             THEN 'test_fixture' ELSE 'unknown' END AS planned_classification
    FROM source_documents s
    JOIN modernisation_cases m ON m.case_id = s.case_id
    JOIN elevators e ON e.elevator_id = m.elevator_id
    WHERE NOT EXISTS (
        SELECT 1 FROM project_documents p
        WHERE p.case_id = s.case_id
          AND p.document_type = 'mod_pdf'
          AND p.file_sha256 = s.file_sha256
    )
    UNION ALL
    SELECT
        'ISO Excel'::text,
        m.case_ref,
        d.document_id,
        1::bigint,
        (SELECT COUNT(*) FROM iso_extracted_values v
         WHERE v.document_id = d.document_id),
        OCTET_LENGTH(d.content),
        (d.file_sha256 = ENCODE(SHA256(d.content), 'hex')),
        CASE WHEN e.elevator_ref LIKE 'TEST-LIFT-%'
             THEN 'test_fixture' ELSE 'unknown' END
    FROM iso_documents d
    JOIN iso_assessments a ON a.assessment_id = d.assessment_id
    JOIN modernisation_cases m ON m.case_id = a.case_id
    JOIN elevators e ON e.elevator_id = m.elevator_id
    WHERE d.document_type = 'iso_excel'
      AND NOT EXISTS (
          SELECT 1 FROM project_documents p
          WHERE p.case_id = a.case_id
            AND p.document_type = 'iso_excel'
            AND p.file_sha256 = d.file_sha256
      )
) inventory
ORDER BY evidence_type, case_ref, legacy_document_id;

-- A legacy pending revision followed by another revision needs special handling:
-- new schema 014 prevents inserting a new revision after a pending revision.
WITH revision_counts AS (
    SELECT m.case_ref, s.document_id AS legacy_document_id,
           r.revision_no, r.review_status,
           COUNT(c.candidate_id) AS comparison_fields
    FROM source_documents s
    JOIN modernisation_cases m ON m.case_id = s.case_id
    JOIN extraction_revisions r ON r.document_id = s.document_id
    LEFT JOIN extraction_candidates c ON c.extraction_revision_id = r.revision_id
    WHERE NOT EXISTS (
        SELECT 1 FROM project_documents p
        WHERE p.case_id = s.case_id
          AND p.document_type = 'mod_pdf'
          AND p.file_sha256 = s.file_sha256
    )
    GROUP BY m.case_ref, s.document_id, r.revision_id, r.revision_no, r.review_status
), checks AS (
    SELECT *,
           LEAD(revision_no) OVER (
               PARTITION BY legacy_document_id ORDER BY revision_no
           ) AS next_revision_no
    FROM revision_counts
)
SELECT case_ref, legacy_document_id, revision_no, review_status,
       comparison_fields,
       CASE WHEN comparison_fields <> 16 THEN 'CHECK_FIELDS'
            ELSE 'OK' END AS field_check,
       CASE WHEN review_status = 'pending' AND next_revision_no IS NOT NULL
            THEN 'PENDING_BEFORE_NEXT_REVISION'
            ELSE 'OK' END AS history_check
FROM checks
ORDER BY case_ref, revision_no;

COMMIT;
