-- existing_index_check.sql — Guard: list existing indexes on a candidate MV.
-- Run before proposing any new index to avoid re-proposing what's already there.
--
-- Caller: mv-optimization-validator or mv-optimization-executor, via execute_sql.
-- Substitute :schema and :mv before calling.
--
-- Output: JSON array of {indexname, indexdef, is_unique, columns_covered}
--
-- Usage contract: executor must call this and confirm the proposed index column
-- is not already covered before calling execute_sql with CREATE INDEX CONCURRENTLY.
-- If a matching index exists, emit verdict=skip reason=already_indexed and stop.

SELECT
  i.indexname,
  i.indexdef,
  ix.indisunique AS is_unique,
  -- extract column list from indexdef for quick human-readable check
  regexp_replace(
    i.indexdef,
    '^.+\((.+)\)$',
    '\1'
  ) AS columns_covered
FROM pg_indexes i
JOIN pg_class c ON c.relname = i.tablename
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = i.schemaname
JOIN pg_index ix ON ix.indrelid = c.oid
JOIN pg_class ic ON ic.oid = ix.indexrelid AND ic.relname = i.indexname
WHERE i.schemaname = :'schema'
  AND i.tablename = :'mv'
ORDER BY i.indexname;
