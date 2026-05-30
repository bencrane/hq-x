SELECT (
  (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema = 'entities'
       AND table_name   = 'source_overture_places') = 1
  AND
  (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema = 'ops'
       AND table_name   = 'overture_places_ingest_runs') = 1
)::int;
