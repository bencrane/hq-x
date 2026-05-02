-- Migration 0005: voice base tables
--
-- Folds OEX migration 051 (voice_assistants, voice_phone_numbers, call_logs,
-- transfer_territories) and 032+054 (outbound_call_configs) onto the
-- brand/partner/campaign axis. Drops voicemail_drops (Vapi handles VM TTS
-- dynamically). Drops voice_sessions (redundant with call_logs); folds its
-- Twilio-AMD-era fields onto call_logs.

-- ----------------------------------------------------------------------------
-- voice_assistants — Vapi assistant config (brand-owned)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voice_assistants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    partner_id UUID NULL,
    campaign_id UUID NULL,

    name TEXT NOT NULL,
    assistant_type TEXT NOT NULL
        CHECK (assistant_type IN ('outbound_qualifier', 'inbound_ivr', 'callback')),
    vapi_assistant_id TEXT,
    system_prompt TEXT,
    first_message TEXT,
    first_message_mode TEXT DEFAULT 'assistant-speaks-first',
    model_config JSONB,
    voice_config JSONB,
    transcriber_config JSONB,
    tools_config JSONB,
    analysis_config JSONB,
    max_duration_seconds INT DEFAULT 600,
    metadata JSONB,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'archived')),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,

    CONSTRAINT voice_assistants_partner_same_brand_fk
        FOREIGN KEY (partner_id, brand_id)
        REFERENCES business.partners(id, brand_id),
    CONSTRAINT voice_assistants_campaign_same_brand_fk
        FOREIGN KEY (campaign_id, brand_id)
        REFERENCES business.campaigns(id, brand_id)
);

CREATE INDEX IF NOT EXISTS idx_voice_assistants_brand
    ON voice_assistants(brand_id) WHERE deleted_at IS NULL;

-- ----------------------------------------------------------------------------
-- voice_phone_numbers — phone↔assistant mapping; carries Vapi + Twilio identity
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voice_phone_numbers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    partner_id UUID NULL,
    campaign_id UUID NULL,

    phone_number VARCHAR(30) NOT NULL,
    vapi_phone_number_id TEXT,
    twilio_phone_number_sid TEXT,
    provider TEXT DEFAULT 'twilio',
    voice_assistant_id UUID REFERENCES voice_assistants(id),
    label TEXT,
    purpose TEXT CHECK (purpose IN ('outbound', 'inbound', 'both')),
    status TEXT DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'inactive', 'failed')),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,

    CONSTRAINT voice_phone_numbers_partner_same_brand_fk
        FOREIGN KEY (partner_id, brand_id)
        REFERENCES business.partners(id, brand_id),
    CONSTRAINT voice_phone_numbers_campaign_same_brand_fk
        FOREIGN KEY (campaign_id, brand_id)
        REFERENCES business.campaigns(id, brand_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_phone_brand
    ON voice_phone_numbers(phone_number, brand_id) WHERE deleted_at IS NULL;

-- Provider-identity uniqueness (merged from OEX migration 061).
CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_phone_numbers_vapi_phone_id
    ON voice_phone_numbers(vapi_phone_number_id)
    WHERE deleted_at IS NULL AND vapi_phone_number_id IS NOT NULL;

-- ----------------------------------------------------------------------------
-- call_logs — unified call history (Vapi + Twilio AMD; folds voice_sessions)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS call_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    partner_id UUID NULL,
    campaign_id UUID NULL,
    voice_assistant_id UUID REFERENCES voice_assistants(id),
    voice_phone_number_id UUID REFERENCES voice_phone_numbers(id),

    vapi_call_id TEXT UNIQUE,
    twilio_call_sid TEXT,
    direction TEXT CHECK (direction IN ('outbound', 'inbound')),
    call_type TEXT CHECK (call_type IN ('outbound', 'inbound', 'callback')),
    customer_number TEXT,
    from_number TEXT,
    status TEXT DEFAULT 'queued'
        CHECK (status IN ('queued', 'ringing', 'in-progress', 'forwarding', 'ended')),
    ended_reason TEXT,
    outcome TEXT CHECK (outcome IN (
        'qualified_transfer', 'not_qualified', 'callback_requested',
        'voicemail_left', 'no_answer', 'busy', 'error'
    )),

    -- Folded from voice_sessions (Twilio-AMD-era fields):
    amd_result TEXT,
    business_disposition TEXT,
    dial_action_status TEXT,

    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    duration_seconds INT,

    transcript TEXT,
    transcript_messages JSONB,
    recording_url TEXT,
    recording_sid TEXT,
    structured_data JSONB,
    analysis_summary TEXT,
    success_evaluation TEXT,
    cost_breakdown JSONB,
    cost_total NUMERIC(10, 4),

    metadata JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,

    CONSTRAINT call_logs_partner_same_brand_fk
        FOREIGN KEY (partner_id, brand_id)
        REFERENCES business.partners(id, brand_id),
    CONSTRAINT call_logs_campaign_same_brand_fk
        FOREIGN KEY (campaign_id, brand_id)
        REFERENCES business.campaigns(id, brand_id)
);

CREATE INDEX IF NOT EXISTS idx_call_logs_brand
    ON call_logs(brand_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_call_logs_vapi_call_id
    ON call_logs(vapi_call_id) WHERE vapi_call_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_call_logs_twilio_call_sid
    ON call_logs(twilio_call_sid) WHERE twilio_call_sid IS NOT NULL;

-- ----------------------------------------------------------------------------
-- transfer_territories — transfer routing rules
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transfer_territories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    partner_id UUID NULL,
    campaign_id UUID NULL,

    name TEXT NOT NULL,
    rules JSONB,
    destination_phone TEXT NOT NULL,
    destination_label TEXT,
    priority INT DEFAULT 0,
    active BOOLEAN DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,

    CONSTRAINT transfer_territories_partner_same_brand_fk
        FOREIGN KEY (partner_id, brand_id)
        REFERENCES business.partners(id, brand_id),
    CONSTRAINT transfer_territories_campaign_same_brand_fk
        FOREIGN KEY (campaign_id, brand_id)
        REFERENCES business.campaigns(id, brand_id)
);

CREATE INDEX IF NOT EXISTS idx_transfer_territories_brand
    ON transfer_territories(brand_id, active) WHERE deleted_at IS NULL;

-- ----------------------------------------------------------------------------
-- outbound_call_configs — per-call Twilio TwiML config
--
-- OEX original FK'd to voice_sessions; here we FK to call_logs since the
-- two-table split is collapsed.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbound_call_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    call_log_id UUID NOT NULL REFERENCES call_logs(id) ON DELETE CASCADE,

    twiml_token TEXT NOT NULL,

    greeting_text TEXT,
    voicemail_text TEXT,
    voicemail_audio_url TEXT,
    human_message_text TEXT,
    voice TEXT NOT NULL DEFAULT 'Polly.Matthew-Generative',
    language TEXT NOT NULL DEFAULT 'en-US',

    -- Vapi AMD bridge (from OEX migration 054):
    amd_strategy TEXT CHECK (amd_strategy IN ('vapi', 'twilio', 'none')),
    vapi_assistant_id TEXT,
    vapi_sip_uri TEXT,
    campaign_voice_config JSONB,
    from_number TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_call_configs_call_log
    ON outbound_call_configs(call_log_id);
CREATE INDEX IF NOT EXISTS idx_outbound_call_configs_brand
    ON outbound_call_configs(brand_id);
