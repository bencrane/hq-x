-- Migration 0009: voice AI extras — campaign configs, active calls, metrics,
-- inbound phone configs, DNC, callbacks (with reminder + voicemail cols),
-- transcript events.
--
-- Ports OEX migrations 052, 053, 055, 057. Adds NEW columns on
-- voice_callback_requests for the SMS reminder flow (§7.6) and AI-callback
-- voicemail-leaving flow (§7.7).

-- ----------------------------------------------------------------------------
-- voice_ai_campaign_configs — 1:1 with campaigns (assistant + AMD strategy)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voice_ai_campaign_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    campaign_id UUID NOT NULL UNIQUE,

    voice_assistant_id UUID REFERENCES voice_assistants(id),
    voice_phone_number_id UUID REFERENCES voice_phone_numbers(id),

    amd_strategy TEXT DEFAULT 'vapi'
        CHECK (amd_strategy IN ('vapi', 'twilio', 'none')),
    max_concurrent_calls INT DEFAULT 5,
    call_window_start TIME,
    call_window_end TIME,
    call_window_timezone TEXT DEFAULT 'America/New_York',
    retry_policy JSONB DEFAULT
        '{"max_attempts": 3, "delay_hours": 4, "backoff_multiplier": 1.5}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,

    CONSTRAINT vac_campaign_same_brand_fk
        FOREIGN KEY (campaign_id, brand_id)
        REFERENCES business.campaigns(id, brand_id)
);

CREATE INDEX IF NOT EXISTS idx_voice_ai_campaign_configs_brand
    ON voice_ai_campaign_configs(brand_id) WHERE deleted_at IS NULL;

-- ----------------------------------------------------------------------------
-- voice_campaign_active_calls — concurrency tracking
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voice_campaign_active_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    campaign_id UUID NOT NULL,

    call_id TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('vapi', 'twilio')),
    status TEXT DEFAULT 'initiated',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    outcome TEXT,
    duration_seconds INT,
    cost_cents INT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,

    CONSTRAINT vcac_campaign_same_brand_fk
        FOREIGN KEY (campaign_id, brand_id)
        REFERENCES business.campaigns(id, brand_id)
);

CREATE INDEX IF NOT EXISTS idx_vcac_campaign_status
    ON voice_campaign_active_calls(campaign_id, status);

-- ----------------------------------------------------------------------------
-- voice_campaign_metrics — aggregated per-campaign metrics
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voice_campaign_metrics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    campaign_id UUID NOT NULL UNIQUE,

    total_calls INT DEFAULT 0,
    calls_connected INT DEFAULT 0,
    calls_voicemail INT DEFAULT 0,
    calls_no_answer INT DEFAULT 0,
    calls_busy INT DEFAULT 0,
    calls_error INT DEFAULT 0,
    calls_transferred INT DEFAULT 0,
    calls_qualified INT DEFAULT 0,
    total_duration_seconds INT DEFAULT 0,
    total_cost_cents INT DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,

    CONSTRAINT vcm_campaign_same_brand_fk
        FOREIGN KEY (campaign_id, brand_id)
        REFERENCES business.campaigns(id, brand_id)
);

-- ----------------------------------------------------------------------------
-- voice_assistant_phone_configs — inbound phone → assistant mapping
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voice_assistant_phone_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    phone_number VARCHAR(30) NOT NULL,
    phone_number_sid TEXT,
    voice_assistant_id UUID NOT NULL REFERENCES voice_assistants(id),
    partner_id UUID NULL,
    routing_mode TEXT DEFAULT 'static'
        CHECK (routing_mode IN ('static', 'dynamic')),
    first_message_mode TEXT,
    inbound_config JSONB,
    is_active BOOLEAN DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,

    CONSTRAINT vapc_partner_same_brand_fk
        FOREIGN KEY (partner_id, brand_id)
        REFERENCES business.partners(id, brand_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_vapc_brand_phone
    ON voice_assistant_phone_configs(brand_id, phone_number) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_vapc_phone
    ON voice_assistant_phone_configs(phone_number)
    WHERE deleted_at IS NULL AND is_active = TRUE;

-- ----------------------------------------------------------------------------
-- do_not_call_lists — voice-DNC suppression
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS do_not_call_lists (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    phone_number VARCHAR(30) NOT NULL,
    source TEXT,
    reason TEXT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    added_by UUID REFERENCES business.users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dnc_brand_phone
    ON do_not_call_lists(brand_id, phone_number) WHERE deleted_at IS NULL;

-- ----------------------------------------------------------------------------
-- voice_callback_requests — durable callback queue
--
-- Adds (vs OEX 057):
--   leave_voicemail_on_no_answer  — §7.7 voicemail-leaving on AI callback
--   voicemail_script              — §7.7
--   reminder_sent_at              — §7.6 SMS callback-reminder tracking
--   reminder_sms_sid              — §7.6
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voice_callback_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    partner_id UUID NULL,
    campaign_id UUID NULL,

    source_call_log_id UUID REFERENCES call_logs(id) ON DELETE SET NULL,
    source_vapi_call_id TEXT NOT NULL,
    voice_assistant_id UUID REFERENCES voice_assistants(id),
    voice_phone_number_id UUID REFERENCES voice_phone_numbers(id),
    customer_number TEXT,
    preferred_time TIMESTAMPTZ NOT NULL,
    timezone TEXT NOT NULL,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled', 'processing', 'completed', 'cancelled', 'failed')),

    -- §7.7 — leave a TTS voicemail if AMD detects voicemail box.
    leave_voicemail_on_no_answer BOOLEAN NOT NULL DEFAULT FALSE,
    voicemail_script TEXT,

    -- §7.6 — SMS reminder send tracking.
    reminder_sent_at TIMESTAMPTZ,
    reminder_sms_sid TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,

    CONSTRAINT vcb_partner_same_brand_fk
        FOREIGN KEY (partner_id, brand_id)
        REFERENCES business.partners(id, brand_id),
    CONSTRAINT vcb_campaign_same_brand_fk
        FOREIGN KEY (campaign_id, brand_id)
        REFERENCES business.campaigns(id, brand_id)
);

CREATE INDEX IF NOT EXISTS idx_vcb_brand_status
    ON voice_callback_requests(brand_id, status) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_vcb_preferred_time
    ON voice_callback_requests(preferred_time) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_vcb_source_call
    ON voice_callback_requests(source_vapi_call_id) WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_vcb_source_call_time
    ON voice_callback_requests(brand_id, source_vapi_call_id, preferred_time, timezone)
    WHERE deleted_at IS NULL;

-- ----------------------------------------------------------------------------
-- vapi_transcript_events — immutable transcript chunk persistence
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vapi_transcript_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    call_log_id UUID REFERENCES call_logs(id) ON DELETE SET NULL,
    vapi_call_id TEXT,
    event_key TEXT NOT NULL,
    event_timestamp TIMESTAMPTZ,
    speaker TEXT,
    channel TEXT,
    is_final BOOLEAN,
    chunk_index INT,
    transcript_text TEXT,
    metadata JSONB,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_vapi_transcript_events_event_key
    ON vapi_transcript_events(event_key);
CREATE INDEX IF NOT EXISTS idx_vapi_transcript_events_brand
    ON vapi_transcript_events(brand_id);
CREATE INDEX IF NOT EXISTS idx_vapi_transcript_events_call_id
    ON vapi_transcript_events(vapi_call_id);
CREATE INDEX IF NOT EXISTS idx_vapi_transcript_events_call_log
    ON vapi_transcript_events(call_log_id);
