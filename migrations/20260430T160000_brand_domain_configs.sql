-- Brand-level domain configuration: Dub link host + landing-page host.
--
-- Two JSONB columns on business.brands carry the per-brand custom-domain
-- bindings the rest of the platform reads:
--
--   dub_domain_config         { domain, dub_domain_id, verified_at }
--   landing_page_domain_config { domain, entri_connection_id, verified_at }
--
-- Both stay nullable. A brand without dub_domain_config still gets dub.sh
-- short links; a brand without landing_page_domain_config still falls back
-- to the platform-default subdomain (e.g. pages.opsengine.run/<brand>/p/...).
-- Custom domains are opt-in per brand.
--
-- Each value's `domain` is the FQDN the customer's recipients see on the
-- piece. `dub_domain_id` is the Dub domain object id returned by Dub's
-- POST /domains; `entri_connection_id` joins to
-- business.entri_domain_connections.id.
--
-- We also tighten business.entri_domain_connections by adding an optional
-- brand_id pointer so we can fetch the connection straight off the brand
-- without a roundabout step → connection lookup. Connections that pre-date
-- this migration stay brand_id=NULL.

ALTER TABLE business.brands
    ADD COLUMN IF NOT EXISTS dub_domain_config JSONB,
    ADD COLUMN IF NOT EXISTS landing_page_domain_config JSONB;

ALTER TABLE business.entri_domain_connections
    ADD COLUMN IF NOT EXISTS brand_id UUID
        REFERENCES business.brands(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_entri_domain_brand
    ON business.entri_domain_connections(brand_id)
    WHERE brand_id IS NOT NULL;
