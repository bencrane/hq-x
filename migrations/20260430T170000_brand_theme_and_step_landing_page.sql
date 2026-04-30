-- Brand theme + step landing-page configuration.
--
-- Two JSONB columns added side-by-side, each populated by its own PATCH
-- endpoint. Both stay nullable; the landing-page render path treats null
-- as "fall back to platform defaults" rather than rejecting the request.
--
-- business.brands.theme_config:
--   {
--     "logo_url": "https://...",
--     "primary_color": "#FF6B35",
--     "secondary_color": "#1A1A1A",
--     "background_color": "#FFFFFF",
--     "text_color": "#222222",
--     "font_family": "Inter",
--     "custom_css": null
--   }
--
-- business.channel_campaign_steps.landing_page_config:
--   {
--     "headline": "Your appointment is ready, {recipient.display_name}",
--     "body": "We've reserved a spot...",
--     "cta": {
--       "type": "form",
--       "label": "Confirm now",
--       "form_schema": { "fields": [...] },
--       "thank_you_message": "Thanks!",
--       "thank_you_redirect_url": null
--     }
--   }
--
-- Schema validation (hex colors, HTTPS logo URL, custom CSS size cap,
-- field-name regex, type allowlist, char limits) happens at the API
-- boundary in app.models.brands / app.models.campaigns; we keep the DB
-- layer permissive on shape so future fields don't require a migration.

ALTER TABLE business.brands
    ADD COLUMN IF NOT EXISTS theme_config JSONB;

ALTER TABLE business.channel_campaign_steps
    ADD COLUMN IF NOT EXISTS landing_page_config JSONB;
