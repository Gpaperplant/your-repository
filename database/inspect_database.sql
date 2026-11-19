-- Inspect current database contents and ISO/EE schema before cleanup.
SELECT current_database() AS database_name, current_user AS connected_as;

-- Show exact row counts for public tables.
DO $$
DECLARE
    item RECORD;
    row_count BIGINT;
BEGIN
    FOR item IN
        SELECT schemaname, tablename
        FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %I.%I', item.schemaname, item.tablename
        ) INTO row_count;
        RAISE NOTICE '%.%: % rows', item.schemaname, item.tablename, row_count;
    END LOOP;
END;
$$;

-- Find legacy EE/PDF fields and the current ISO tables.
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (
      table_name LIKE 'iso_%'
      OR column_name ~* '(^ee_|^pdf_|pair|comparison)'
  )
ORDER BY table_name, ordinal_position;

-- Show foreign keys involving ISO tables.
SELECT conrelid::regclass AS source_table,
       conname AS constraint_name,
       pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE contype = 'f'
  AND (
      conrelid::regclass::text LIKE '%iso_%'
      OR confrelid::regclass::text LIKE '%iso_%'
  )
ORDER BY conrelid::regclass::text, conname;

-- Show ISO indexes, including legacy document-pair uniqueness rules.
SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public' AND tablename LIKE 'iso_%'
ORDER BY tablename, indexname;psql -h 127.0.0.1 -U app_user -d request_app \
  -f database/inspect_database.sql
