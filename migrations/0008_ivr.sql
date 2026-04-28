-- Migration 0008: IVR — flows, flow steps, phone configs, sessions
--
-- Ports OEX migrations 030 + 044 (audio_url merged in) on brand_id. Drops
-- ivr_sessions FK to voice_sessions (replaced by call_logs).

CREATE TABLE IF NOT EXISTS ivr_flows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    default_voice TEXT NOT NULL DEFAULT 'Polly.Joanna-Generative',
    default_language TEXT NOT NULL DEFAULT 'en-US',

    lookup_type TEXT,
    lookup_config JSONB,

    default_transfer_number TEXT,
    transfer_timeout_seconds INT NOT NULL DEFAULT 30,

    recording_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    recording_consent_required BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_ivr_flows_brand ON ivr_flows(brand_id);
CREATE INDEX IF NOT EXISTS idx_ivr_flows_active
    ON ivr_flows(brand_id, is_active) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS ivr_flow_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flow_id UUID NOT NULL REFERENCES ivr_flows(id) ON DELETE CASCADE,
    brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE CASCADE,

    step_key TEXT NOT NULL,
    step_type TEXT NOT NULL CHECK (step_type IN (
        'greeting', 'gather_dtmf', 'gather_speech', 'data_lookup',
        'say_dynamic', 'transfer', 'record', 'hangup'
    )),
    position INT NOT NULL,

    say_text TEXT,
    say_voice TEXT,
    say_language TEXT,

    audio_url TEXT,

    gather_input TEXT
        CHECK (gather_input IS NULL OR gather_input IN ('dtmf', 'speech', 'dtmf speech')),
    gather_num_digits INT,
    gather_timeout_seconds INT DEFAULT 5,
    gather_finish_on_key TEXT DEFAULT '#',
    gather_max_retries INT DEFAULT 2,
    gather_invalid_message TEXT,
    gather_validation_regex TEXT,

    next_step_key TEXT,
    branches JSONB,

    transfer_number TEXT,
    transfer_caller_id TEXT,
    transfer_record TEXT DEFAULT 'do-not-record'
        CHECK (transfer_record IN (
            'do-not-record', 'record-from-answer', 'record-from-ringing',
            'record-from-answer-dual', 'record-from-ringing-dual'
        )),

    record_max_length_seconds INT DEFAULT 120,
    record_play_beep BOOLEAN DEFAULT TRUE,

    lookup_input_key TEXT,
    lookup_store_key TEXT DEFAULT 'lookup_result',

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ivr_flow_steps_key
    ON ivr_flow_steps(flow_id, step_key);
CREATE INDEX IF NOT EXISTS idx_ivr_flow_steps_flow_id
    ON ivr_flow_steps(flow_id);
CREATE INDEX IF NOT EXISTS idx_ivr_flow_steps_brand
    ON ivr_flow_steps(brand_id);

CREATE TABLE IF NOT EXISTS ivr_phone_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE CASCADE,

    phone_number TEXT NOT NULL,
    phone_number_sid TEXT,
    flow_id UUID NOT NULL REFERENCES ivr_flows(id) ON DELETE RESTRICT,

    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ivr_phone_configs_number
    ON ivr_phone_configs(phone_number) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ivr_phone_configs_brand
    ON ivr_phone_configs(brand_id);
CREATE INDEX IF NOT EXISTS idx_ivr_phone_configs_flow
    ON ivr_phone_configs(flow_id);

-- ivr_sessions — application-level state of active IVR calls.
-- Linked to call_logs (the unified call history) instead of voice_sessions.
CREATE TABLE IF NOT EXISTS ivr_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE CASCADE,
    call_log_id UUID REFERENCES call_logs(id) ON DELETE SET NULL,

    flow_id UUID NOT NULL REFERENCES ivr_flows(id),
    call_sid TEXT NOT NULL,
    caller_number TEXT NOT NULL,
    called_number TEXT NOT NULL,

    current_step_key TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'completed', 'transferred', 'abandoned', 'error')),
    session_data JSONB NOT NULL DEFAULT '{}',
    retry_counts JSONB NOT NULL DEFAULT '{}',

    transfer_result TEXT,
    disposition TEXT,

    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ivr_sessions_call_sid ON ivr_sessions(call_sid);
CREATE INDEX IF NOT EXISTS idx_ivr_sessions_brand ON ivr_sessions(brand_id);
CREATE INDEX IF NOT EXISTS idx_ivr_sessions_active
    ON ivr_sessions(brand_id, status) WHERE status = 'active';
