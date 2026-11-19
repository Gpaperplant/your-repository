-- DESTRUCTIVE: Clear ISO/EE assessment evidence in the local development database only.
-- Back up request_app before running this file.
BEGIN;

DO $$
DECLARE
    host INET := inet_server_addr();
BEGIN
    IF current_database() <> 'request_app' THEN
        RAISE EXCEPTION 'Reset refused: expected development database request_app, found %', current_database();
    END IF;
    IF host IS NOT NULL
        AND NOT (
            host <<= INET '127.0.0.0/8'
            OR host = INET '::1'
            OR host = INET '172.18.0.2'
        ) THEN
        RAISE EXCEPTION 'Reset refused: database connection is not local (%).', host;
    END IF;
END;
$$;

-- Unexpected foreign keys stop the reset instead of deleting other data.
TRUNCATE TABLE
    public.iso_extracted_values,
    public.iso_documents,
    public.iso_assessments
RESTART IDENTITY;

-- Verify all ISO assessment data is empty before committing.
SELECT 'iso_assessments' AS table_name, count(*) AS rows_left FROM public.iso_assessments
UNION ALL
SELECT 'iso_documents', count(*) FROM public.iso_documents
UNION ALL
SELECT 'iso_extracted_values', count(*) FROM public.iso_extracted_values;

COMMIT;
