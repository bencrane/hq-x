import os

# Populate tier-1 secrets with dummy values BEFORE app modules are imported,
# so app.config.Settings() validation passes at import time.
_DUMMY_ENV = {
    "HQX_DB_URL_POOLED": "postgresql://user:pass@localhost:6543/postgres",
    "HQX_DB_URL_DIRECT": "postgresql://user:pass@localhost:5432/postgres",
    "HQX_SUPABASE_URL": "https://example.supabase.co",
    "HQX_SUPABASE_SERVICE_ROLE_KEY": "dummy-service-role-key",
    "HQX_SUPABASE_PUBLISHABLE_KEY": "dummy-publishable-key",
    "HQX_SUPABASE_PROJECT_REF": "examplezzzzzzzzzzzz",
    "APP_ENV": "dev",
    "EMAILBISON_WEBHOOK_PATH_TOKEN": "test-path-token",
    "EMAILBISON_WEBHOOK_ALLOWED_ORIGINS": "app.emailbison.com,emailbison.com",
    "TRIGGER_SHARED_SECRET": "test-trigger-secret",
    "LOB_API_KEY": "test_lob_key",
    "LOB_API_KEY_TEST": "test_lob_test_key",
    "LOB_WEBHOOKS_SECRET_LIVE": "test_lob_webhook_live_secret",
    "LOB_WEBHOOKS_SECRET_TEST": "test_lob_webhook_test_secret",
    "LOB_WEBHOOK_SIGNATURE_MODE": "permissive_audit",
    "DUB_API_KEY": "dub_test_key",
}
for k, v in _DUMMY_ENV.items():
    os.environ.setdefault(k, v)
