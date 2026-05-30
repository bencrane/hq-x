SELECT (
  (SELECT COUNT(*) FROM pg_matviews
     WHERE schemaname = 'entities'
       AND matviewname = 'mv_nppes_provider_targeting'
       AND ispopulated) = 1
  AND (SELECT COUNT(*) FROM pg_matviews
         WHERE schemaname = 'entities'
           AND matviewname = 'mv_nppes_signal_delta_new_enumerations'
           AND ispopulated) = 1
  AND (SELECT COUNT(*) FROM entities.mv_nppes_provider_targeting) > 0
  AND (SELECT COUNT(*) FROM pg_proc p
         JOIN pg_namespace n ON p.pronamespace = n.oid
        WHERE n.nspname = 'ops'
          AND p.proname IN (
            'refresh_mv_nppes_provider_targeting',
            'refresh_mv_nppes_signal_delta_new_enumerations'
          )) = 2
  AND (SELECT COUNT(*) FROM cron.job
         WHERE jobname IN (
           'refresh_mv_nppes_provider_targeting',
           'refresh_mv_nppes_signal_delta_new_enumerations'
         )) = 2
)::int;
