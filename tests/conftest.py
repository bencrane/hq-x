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
}
for k, v in _DUMMY_ENV.items():
    os.environ.setdefault(k, v)
