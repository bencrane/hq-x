-- Migration 0007: SMS — sms_messages + sms_suppressions
--
-- sms_messages ports OEX 034 onto brand_id. sms_suppressions is NEW for the
-- in-app STOP/HELP suppression list (drift fix §7.3).

CREATE TABLE IF NOT EXISTS sms_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE CASCADE,
    partner_id UUID NULL,
    campaign_id UUID NULL,

    message_sid TEXT NOT NULL,
    account_sid TEXT NOT NULL,
    messaging_service_sid TEXT,

    direction TEXT NOT NULL
        CHECK (direction IN ('inbound', 'outbound-api', 'outbound-reply')),
    from_number TEXT NOT NULL,
    to_number TEXT NOT NULL,

    body TEXT,
    num_segments INT,
    num_media INT DEFAULT 0,
    media_urls JSONB,

    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN (
            'accepted', 'scheduled', 'canceled', 'queued', 'sending',
            'sent', 'failed', 'delivered', 'undelivered',
            'receiving', 'received', 'read'
        )),
    error_code INT,
    error_message TEXT,

    price TEXT,
    price_unit TEXT,

    last_callback_payload JSONB,

    date_sent TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT sms_messages_partner_same_brand_fk
        FOREIGN KEY (partner_id, brand_id)
        REFERENCES business.partners(id, brand_id),
    CONSTRAINT sms_messages_campaign_same_brand_fk
        FOREIGN KEY (campaign_id, brand_id)
        REFERENCES business.campaigns(id, brand_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sms_messages_message_sid
    ON sms_messages(message_sid);
CREATE INDEX IF NOT EXISTS idx_sms_messages_brand
    ON sms_messages(brand_id);
CREATE INDEX IF NOT EXISTS idx_sms_messages_status
    ON sms_messages(brand_id, status);
CREATE INDEX IF NOT EXISTS idx_sms_messages_direction
    ON sms_messages(brand_id, direction);

-- ----------------------------------------------------------------------------
-- sms_suppressions — in-app STOP/HELP/manual suppression (NEW, drift §7.3)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sms_suppressions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE CASCADE,
    phone_number TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('stop_keyword', 'manual', 'bounce')),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sms_suppressions_brand_phone
    ON sms_suppressions(brand_id, phone_number);
CREATE INDEX IF NOT EXISTS idx_sms_suppressions_phone
    ON sms_suppressions(phone_number);
