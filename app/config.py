from typing import Literal

from pydantic import HttpUrl, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_LOB_WEBHOOK_MODES = {"permissive_audit", "disabled"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=True)

    HQX_DB_URL_POOLED: PostgresDsn
    HQX_DB_URL_DIRECT: PostgresDsn
    HQX_SUPABASE_URL: HttpUrl
    HQX_SUPABASE_SERVICE_ROLE_KEY: SecretStr
    HQX_SUPABASE_PUBLISHABLE_KEY: SecretStr
    HQX_SUPABASE_PROJECT_REF: str

    # APP_ENV is set inside each Doppler config (dev/stg/prd) and injected
    # at runtime by `doppler run`. The Doppler service token is scoped to a
    # single config, so Railway only needs DOPPLER_TOKEN — APP_ENV comes
    # along automatically with the right value for that environment.
    APP_ENV: Literal["dev", "stg", "prd"]
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    CAL_WEBHOOK_SECRET: str = ""
    EMAILBISON_WEBHOOK_PATH_TOKEN: str | None = None
    EMAILBISON_WEBHOOK_ALLOWED_ORIGINS: str = "app.emailbison.com,emailbison.com"
    EMAILBISON_API_BASE: str = "https://app.outboundsolutions.com"
    EMAILBISON_API_KEY: str | None = None
    EMAILBISON_DEFAULT_FROM_NAME: str | None = None

    # Cluster 3 outbound — when truthy, the intro composer's send seam
    # actually POSTs /api/replies/new to EmailBison. When falsy (the
    # default for sim / pre-prod), the seam returns a deterministic
    # fake eb_reply_id and the orchestrator records the would-have-sent
    # payload on the email_messages metadata.
    CLUSTER3_LIVE_SEND: bool = False

    # Cluster 3 reliability + observability.
    # Telegram alert helper sends to the operator chat when these are
    # configured. Both must be present for delivery; otherwise the
    # business.cluster3_alerts row is still written (durable audit) but
    # the Telegram side-channel is skipped.
    TELEGRAM_BOT_TOKEN: SecretStr | None = None
    TELEGRAM_OPERATOR_CHAT_ID: str | None = None

    # Default classifier + composer + verdict modes for the live dispatch
    # path. 'auto' resolves to 'anthropic' when ANTHROPIC_API_KEY is set,
    # else 'stub'. Override per-call from admin endpoints if needed.
    CLUSTER3_CLASSIFIER_MODE: str = "auto"
    CLUSTER3_COMPOSER_MODE: str = "auto"
    CLUSTER3_VERDICT_MODE: str = "auto"

    # When verdict gate blocks an intro, the lead_transfer parks in
    # 'pending_review' status. When false, we ship anyway after logging
    # — only set to false during the bootstrap window before the verdict
    # rubric is dialed in. Default true (production-grade safety).
    CLUSTER3_VERDICT_GATES_SEND: bool = True

    # Cluster 1 auto-reply (in-thread reply to demand-side prospects who
    # said yes to the operator's outreach). When truthy, the auto-reply
    # composer's send seam actually POSTs /api/replies/{id}/reply to
    # EmailBison. Defaults false in dev/pre-prod; flip to true in prd
    # Doppler when ready.
    CLUSTER1_LIVE_SEND: bool = False

    # Outbound lead-attach kill switch. The eb_lead_attach service
    # decides live vs dry_run from a three-tier opt-in:
    #   1. This kill switch (must be true to consider live mode)
    #   2. Org metadata.outbound_live_lead_attach_enabled (default true
    #      when this kill switch is on)
    #   3. Initiative metadata.outbound_live_lead_attach_enabled
    #      (per-initiative override; absent = inherit org default)
    # Defaulting to false means no real EB lead-attach happens until
    # the operator deliberately flips this in Doppler hq-x/prd. Even
    # then, every initiative inherits the org default until explicitly
    # set, so the operator can stage rollout per-initiative.
    OUTBOUND_LIVE_LEAD_ATTACH: bool = False

    TRIGGER_SHARED_SECRET: str | None = None
    # Trigger.dev secret-key API key (tr_dev_... / tr_prod_...) used by hq-x
    # to enqueue tasks via /api/v1/tasks/{taskIdentifier}/trigger and cancel
    # runs via /api/v2/runs/{runId}/cancel. Distinct from TRIGGER_SHARED_SECRET
    # (which authenticates Trigger.dev tasks calling back into hq-x).
    TRIGGER_API_KEY: SecretStr | None = None
    # Override the Trigger.dev API base. Defaults to https://api.trigger.dev
    # when None. Tests set this to a local stub.
    TRIGGER_API_BASE_URL: str | None = None

    # Slice 3 — DMaaS reconciliation crons. Each task has its own kill switch
    # so an operator can disable a single noisy reconciler via Doppler
    # without a deploy. Defaults true (enabled).
    DMAAS_RECONCILE_STALE_JOBS_ENABLED: bool = True
    DMAAS_RECONCILE_LOB_ENABLED: bool = True
    DMAAS_RECONCILE_DUB_ENABLED: bool = True
    DMAAS_RECONCILE_WEBHOOK_REPLAYS_ENABLED: bool = True
    DMAAS_RECONCILE_CUSTOMER_WEBHOOKS_ENABLED: bool = True
    # Stale-job threshold: jobs running longer than this many hours get
    # marked failed by the reconciliation cron. Default 2h matches the
    # max activation window we'd ever expect for a 50k-recipient campaign.
    DMAAS_RECONCILE_STALE_JOB_THRESHOLD_HOURS: int = 2
    # Failed-job dead-letter delay: jobs in `failed` for this many hours
    # without retry transition to `dead_lettered` so operators have a
    # bounded review queue.
    DMAAS_RECONCILE_DEAD_LETTER_DELAY_HOURS: int = 24

    # ── Voice infrastructure (Twilio + Vapi + ClickHouse) ───────────────────
    # Public-facing API base URL — used to construct Twilio status-callback
    # URLs and Vapi inbound webhook URLs.
    HQX_API_BASE_URL: str = "http://localhost:8000"

    # Master key for encrypting brands.twilio_account_sid_enc /
    # twilio_auth_token_enc via pgcrypto pgp_sym_encrypt.
    BRAND_CREDS_ENCRYPTION_KEY: SecretStr | None = None

    # Vapi — global account (single operator). VAPI_API_KEY is used by
    # outbound REST calls; VAPI_WEBHOOK_SECRET signs inbound webhooks.
    VAPI_API_KEY: SecretStr | None = None
    VAPI_WEBHOOK_SECRET: SecretStr | None = None
    # Default `strict` (drift fix §7.8 — was permissive_audit in OEX code).
    VAPI_WEBHOOK_SIGNATURE_MODE: Literal["strict", "permissive_audit", "disabled"] = "strict"

    # Twilio webhook signature mode. `enforce` rejects on mismatch.
    TWILIO_WEBHOOK_SIGNATURE_MODE: Literal["enforce", "permissive_audit", "disabled"] = "enforce"

    # ClickHouse (analytics dual-write). All optional — analytics is
    # fire-and-forget and skips when unconfigured.
    CLICKHOUSE_URL: str | None = None
    CLICKHOUSE_USER: str | None = None
    CLICKHOUSE_PASSWORD: SecretStr | None = None
    CLICKHOUSE_DATABASE: str = "default"

    # RudderStack (analytics fan-out). Both optional — when either is
    # missing the rudder.track() shim is a no-op (events still log).
    # Doppler `hq-x/dev` and `hq-x/prd` carry both keys today; the
    # Python source name in the RudderStack workspace is `hq-x-server`.
    RUDDERSTACK_WRITE_KEY: SecretStr | None = None
    RUDDERSTACK_DATA_PLANE_URL: str | None = None

    # data-engine-x base URL — for Vapi `lookup_carrier` tool (drift fix §7.4).
    DEX_BASE_URL: str | None = None
    # Super-admin API key for DEX. Used by hq-x ↔ DEX server-to-server calls
    # (e.g. seed scripts, reconciliation jobs) when no user JWT is available.
    # User-initiated calls pass through the operator's hq-x Supabase JWT
    # instead via DEX's hq-x JWKS path.
    DEX_SUPER_ADMIN_API_KEY: SecretStr | None = None

    # ── Exa research API ─────────────────────────────────────────────────
    # Exa's HTTP API authenticates via `x-api-key`. Used by the Exa
    # research orchestration to run search / contents / findSimilar /
    # research / answer calls; raw payloads land in exa.exa_calls in
    # either hq-x or DEX based on the per-run `destination` flag.
    EXA_API_KEY: SecretStr | None = None
    EXA_API_BASE: str = "https://api.exa.ai"
    # Per-day webset run cap. Defaults to 15 so boot succeeds even when the
    # Doppler secret is not yet set. Operator MUST run:
    #   doppler secrets set EXA_WEBSETS_DAILY_RUN_CAP=15 --project hq-all --config prd
    # before relying on this cap to take effect server-side.
    EXA_WEBSETS_DAILY_RUN_CAP: int = 15

    # ── Anthropic API ────────────────────────────────────────────────────
    # First hq-x → Anthropic call site is the gtm-initiative strategy
    # synthesizer. The wrapper in app/services/anthropic_client.py uses
    # this key plus the default model below.
    ANTHROPIC_API_KEY: SecretStr | None = None
    ANTHROPIC_DEFAULT_MODEL: str = "claude-opus-4-7"

    # Anthropic Managed Agents API (the post-payment GTM pipeline runtime).
    # Distinct from ANTHROPIC_API_KEY so the two paths can be billed /
    # scoped / rotated independently — even when both keys end up holding
    # the same workspace value. The hq-x service in
    # app/services/anthropic_managed_agents.py reads this exclusively.
    ANTHROPIC_MANAGED_AGENTS_API_KEY: SecretStr | None = None
    # Vault and environment ids passed to /v1/sessions when starting a MAGS
    # session. Hardcoded defaults match managed-agents-x's setup scripts;
    # operator can override per-env via Doppler if a vault/env is rotated.
    ANTHROPIC_MAGS_DEFAULT_VAULT_ID: str = "vlt_011CZtjQ5LjLrbAd4gX7xA6E"
    ANTHROPIC_MAGS_DEFAULT_ENVIRONMENT_ID: str = "env_01T3cywTrvvtZoUQYAzxMA1D"

    # ── Lob (direct mail) ───────────────────────────────────────────────────
    # Single global API key (no per-org credentials).
    LOB_API_KEY: str | None = None
    # Optional test-mode key. When a request opts in via `test_mode=true`,
    # the route uses this key instead of LOB_API_KEY. Lets prd mint zero-cost
    # test pieces / address verifies without burning credits.
    LOB_API_KEY_TEST: str | None = None
    # Lob runs separate webhook subscriptions for live vs test mode, each
    # with its own signing secret. Pieces created with LOB_API_KEY trigger
    # webhooks signed with LOB_WEBHOOKS_SECRET_LIVE; pieces created with
    # LOB_API_KEY_TEST get signed with LOB_WEBHOOKS_SECRET_TEST. The
    # receiver tries both — whichever verifies is the environment we
    # record on the event.
    LOB_WEBHOOKS_SECRET_LIVE: str | None = None
    LOB_WEBHOOKS_SECRET_TEST: str | None = None
    LOB_WEBHOOK_SIGNATURE_MODE: str = "permissive_audit"
    LOB_WEBHOOK_SIGNATURE_TOLERANCE_SECONDS: int = 300
    LOB_WEBHOOK_SCHEMA_VERSIONS: str = "v1"

    # SLO thresholds (rates expressed as 0.01 = 1%). Negative disables.
    LOB_SLO_SIGNATURE_REJECT_RATE_THRESHOLD: float = 0.01
    LOB_SLO_DEAD_LETTER_RATE_THRESHOLD: float = 0.01
    LOB_SLO_REPLAY_FAILURE_RATE_THRESHOLD: float = 0.05
    LOB_SLO_PROJECTION_FAILURE_RATE_THRESHOLD: float = 0.01
    LOB_SLO_DUPLICATE_IGNORE_RATE_THRESHOLD: float = 0.2

    # ── DMaaS MCP server ────────────────────────────────────────────────────
    # Bearer token managed-agent clients present in the Authorization header
    # to call /mcp/dmaas/*. When None in dev/stg, the MCP mount accepts any
    # request (convenient for local testing). Production refuses to boot
    # without it (see `assert_production_safe`).
    DMAAS_MCP_BEARER_TOKEN: SecretStr | None = None

    # ── Dub.co link attribution ─────────────────────────────────────────────
    # API key from app.dub.co/settings/tokens. Required in prd.
    DUB_API_KEY: SecretStr | None = None
    # Default short-link domain (e.g. "dub.sh" or our custom "go.hq-x.com").
    # When None, dub uses the workspace's primary domain.
    DUB_DEFAULT_DOMAIN: str | None = None
    # Default tenantId stamped on every link create — lets us segment
    # attribution by environment / brand later. None = no stamping.
    DUB_DEFAULT_TENANT_ID: str | None = None
    # HMAC secret for verifying inbound Dub webhook calls (Dub-Signature
    # header = hex HMAC-SHA256 over the raw body). Required in prd whenever
    # DUB_WEBHOOK_SIGNATURE_MODE != 'disabled'.
    DUB_WEBHOOK_SECRET: SecretStr | None = None
    # Signature enforcement mode. Mirrors LOB_WEBHOOK_SIGNATURE_MODE:
    #   enforce          - reject anything that doesn't verify
    #   permissive_audit - audit + log failures but accept the event
    #   disabled         - do not verify (insecure; refused at boot in prd)
    DUB_WEBHOOK_SIGNATURE_MODE: Literal["enforce", "permissive_audit", "disabled"] = (
        "permissive_audit"
    )
    # Override base URL for tests / self-hosted dub. Defaults to api.dub.co.
    DUB_API_BASE_URL: str | None = None

    # ── Entri (custom domains for DMaaS landing pages) ─────────────────────
    # Single partner account. `applicationId` is public-shippable;
    # `secret` is server-only and used to mint short-lived (60-min) JWTs.
    # The webhook secret is separate and used to verify HMAC-SHA256 V2
    # signatures on inbound events.
    #
    # When ENTRI_APPLICATION_ID is unset, all /api/v1/entri/* endpoints
    # return 503 entri_not_configured — the integration is fully built
    # but inert until we sign up for a paid Entri plan.
    ENTRI_APPLICATION_ID: str | None = None
    ENTRI_SECRET: SecretStr | None = None
    ENTRI_WEBHOOK_SECRET: SecretStr | None = None
    # Hostname we own (e.g. "domains.dmaas.ourcompany.com") which has been
    # CNAMEd to power.goentri.com and registered as cname_target in the
    # Entri dashboard. Customer subdomains CNAME here.
    ENTRI_CNAME_TARGET: str | None = None
    # Origin URL that Entri Power proxies traffic to. Per-campaign paths
    # are appended at session-creation time (e.g. <base>/lp/<step_id>).
    ENTRI_APPLICATION_URL_BASE: str | None = None
    # API base. Override for tests.
    ENTRI_API_BASE: str = "https://api.goentri.com"
    # Webhook signature mode — same semantics as LOB/DUB.
    ENTRI_WEBHOOK_SIGNATURE_MODE: Literal["enforce", "permissive_audit", "disabled"] = (
        "permissive_audit"
    )
    # Replay-window for V2 signature timestamp check (seconds).
    ENTRI_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS: int = 300

    # Per-env salt for hashing visitor IPs before persistence into
    # business.landing_page_views.source_metadata. Raw IPs never land in
    # the row. Setting must be unique per env so dev IPs don't deanon
    # against prod hashes.
    LANDING_PAGE_IP_HASH_SALT: str = "hq-x-landing-page-ip-salt-dev"

    # Dedupe window for landing-page emit_event("page.viewed", ...).
    # A second render from the same hashed IP within this many seconds
    # still serves the page but skips the second emit + insert.
    LANDING_PAGE_VIEW_DEDUPE_SECONDS: int = 60

    # ── Stripe (proposals → checkout → webhook → instantiate-for-payment) ──
    # Both _TEST and _LIVE pairs of (secret, publishable, webhook secret)
    # can live in Doppler simultaneously; STRIPE_MODE selects which the
    # runtime actually uses. Use the lowercase ``stripe_*`` properties
    # below — never the raw fields — so all consumers honor the mode.
    # The active webhook secret is required at boot in prd whenever the
    # active secret key is set (see assert_production_safe).
    # STRIPE_API_BASE: override for tests / proxies. Defaults to live API.
    # Stripe — mode-switched. Both _TEST and _LIVE pairs can live in
    # Doppler simultaneously; STRIPE_MODE picks which set the runtime
    # actually uses. Default is "test" so production won't accidentally
    # run live charges if STRIPE_MODE is forgotten.
    STRIPE_MODE: Literal["test", "live"] = "test"
    STRIPE_SECRET_KEY_TEST: SecretStr | None = None
    STRIPE_SECRET_KEY_LIVE: SecretStr | None = None
    STRIPE_PUBLISHABLE_KEY_TEST: str | None = None
    STRIPE_PUBLISHABLE_KEY_LIVE: str | None = None
    STRIPE_WEBHOOK_SECRET_TEST: SecretStr | None = None
    STRIPE_WEBHOOK_SECRET_LIVE: SecretStr | None = None
    STRIPE_API_BASE: str = "https://api.stripe.com"
    STRIPE_API_VERSION: str = "2024-11-20.acacia"
    # Tolerance for webhook timestamp replay-protection (Stripe default 5m).
    STRIPE_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS: int = 300
    # Public-facing base for the partner-platform proposal page; used to
    # build success_url / cancel_url returned to Stripe Checkout.
    PARTNER_PLATFORM_BASE_URL: str = "http://localhost:3000"

    # ----- mode-aware Stripe key accessors -----
    # Consumers read these properties (lowercase) instead of the raw
    # *_TEST / *_LIVE fields, so the whole codebase honors STRIPE_MODE
    # from one place.

    @property
    def stripe_secret_key(self) -> SecretStr | None:
        return (
            self.STRIPE_SECRET_KEY_LIVE
            if self.STRIPE_MODE == "live"
            else self.STRIPE_SECRET_KEY_TEST
        )

    @property
    def stripe_publishable_key(self) -> str | None:
        return (
            self.STRIPE_PUBLISHABLE_KEY_LIVE
            if self.STRIPE_MODE == "live"
            else self.STRIPE_PUBLISHABLE_KEY_TEST
        )

    @property
    def stripe_webhook_secret(self) -> SecretStr | None:
        return (
            self.STRIPE_WEBHOOK_SECRET_LIVE
            if self.STRIPE_MODE == "live"
            else self.STRIPE_WEBHOOK_SECRET_TEST
        )

    # ── Resend (transactional email) ─────────────────────────────────────
    RESEND_API_KEY: SecretStr | None = None
    RESEND_FROM_ADDRESS: str = "Benjamin Crane <benjamin.crane@engineereddemand.com>"
    RESEND_OPERATOR_ADDRESS: str = "benjamin.crane@engineereddemand.com"


settings = Settings()


def _strict_signature_modes() -> None:
    """Production safety check — refuse to boot with relaxed signature modes."""
    if settings.APP_ENV != "prd":
        return
    if settings.VAPI_WEBHOOK_SIGNATURE_MODE != "strict":
        raise RuntimeError(
            "VAPI_WEBHOOK_SIGNATURE_MODE must be 'strict' in production "
            f"(got '{settings.VAPI_WEBHOOK_SIGNATURE_MODE}')"
        )
    if settings.TWILIO_WEBHOOK_SIGNATURE_MODE != "enforce":
        raise RuntimeError(
            "TWILIO_WEBHOOK_SIGNATURE_MODE must be 'enforce' in production "
            f"(got '{settings.TWILIO_WEBHOOK_SIGNATURE_MODE}')"
        )


_strict_signature_modes()


def assert_production_safe(s: Settings = settings) -> None:
    """Refuse to boot in production with insecure Lob webhook signature modes.

    If APP_ENV=prd and LOB_WEBHOOK_SIGNATURE_MODE is not `enforce`, the app
    will not start. The live webhook secret is also required in prd.
    """
    if s.APP_ENV != "prd":
        return
    mode = (s.LOB_WEBHOOK_SIGNATURE_MODE or "").strip().lower()
    if mode in _INSECURE_LOB_WEBHOOK_MODES:
        raise RuntimeError(
            f"LOB_WEBHOOK_SIGNATURE_MODE={mode!r} is insecure when APP_ENV=prd; "
            "set LOB_WEBHOOK_SIGNATURE_MODE=enforce and provide LOB_WEBHOOKS_SECRET_LIVE"
        )
    if not s.LOB_WEBHOOKS_SECRET_LIVE:
        raise RuntimeError("LOB_WEBHOOKS_SECRET_LIVE must be set when APP_ENV=prd")
    if not s.DMAAS_MCP_BEARER_TOKEN:
        raise RuntimeError(
            "DMAAS_MCP_BEARER_TOKEN must be set when APP_ENV=prd; "
            "the /mcp/dmaas server refuses to start unauthenticated in production"
        )
    if not s.DUB_API_KEY:
        raise RuntimeError("DUB_API_KEY must be set when APP_ENV=prd")
    dub_mode = (s.DUB_WEBHOOK_SIGNATURE_MODE or "").strip().lower()
    if dub_mode in _INSECURE_LOB_WEBHOOK_MODES:
        raise RuntimeError(
            f"DUB_WEBHOOK_SIGNATURE_MODE={dub_mode!r} is insecure when APP_ENV=prd; "
            "set DUB_WEBHOOK_SIGNATURE_MODE=enforce and provide DUB_WEBHOOK_SECRET"
        )
    if not s.DUB_WEBHOOK_SECRET:
        raise RuntimeError("DUB_WEBHOOK_SECRET must be set when APP_ENV=prd")
    # Entri is opt-in: if ENTRI_APPLICATION_ID is set we're committing to
    # the integration in this env, so demand a verifying webhook config.
    # If unset, the entri router self-disables via 503 and that's fine.
    if s.ENTRI_APPLICATION_ID:
        if not s.ENTRI_SECRET:
            raise RuntimeError(
                "ENTRI_SECRET must be set when ENTRI_APPLICATION_ID is set in prd"
            )
        entri_mode = (s.ENTRI_WEBHOOK_SIGNATURE_MODE or "").strip().lower()
        if entri_mode in _INSECURE_LOB_WEBHOOK_MODES:
            raise RuntimeError(
                f"ENTRI_WEBHOOK_SIGNATURE_MODE={entri_mode!r} is insecure when APP_ENV=prd; "
                "set ENTRI_WEBHOOK_SIGNATURE_MODE=enforce and provide ENTRI_WEBHOOK_SECRET"
            )
        if not s.ENTRI_WEBHOOK_SECRET:
            raise RuntimeError("ENTRI_WEBHOOK_SECRET must be set when APP_ENV=prd")
    # Stripe webhook-secret pairing is only enforced in LIVE mode. In
    # test/sandbox mode (the default), Stripe events are not real money
    # movements; if the webhook secret is missing the webhook handler
    # itself returns a clear 503 with the missing-key name — no need to
    # hard-fail boot on a sandbox misconfiguration. The live-mode pair
    # is still mandatory so unverified prod events can never be accepted.
    if s.STRIPE_MODE == "live" and s.stripe_secret_key:
        if not s.stripe_webhook_secret:
            raise RuntimeError(
                "STRIPE_WEBHOOK_SECRET_LIVE must be set when "
                "STRIPE_SECRET_KEY_LIVE is set with STRIPE_MODE=live in prd"
            )
