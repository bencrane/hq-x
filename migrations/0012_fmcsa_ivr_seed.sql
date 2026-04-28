-- Migration 0011: Seed the FMCSA Carrier Qualification IVR flow.
--
-- Mirrors OEX migration 031. The OEX seed depended on a real org_id for
-- Outbound Solutions; in hq-x we create a stub brand row keyed by the stable
-- name 'fmcsa-stub' (idempotent ON CONFLICT) and attach the flow rows to it.

DO $$
DECLARE
    v_brand_id UUID;
    v_flow_id UUID;
BEGIN
    -- 1. Idempotently ensure stub brand exists.
    INSERT INTO business.brands (name, display_name)
    VALUES ('fmcsa-stub', 'FMCSA Stub Brand (seed)')
    ON CONFLICT DO NOTHING;

    SELECT id INTO v_brand_id
    FROM business.brands
    WHERE name = 'fmcsa-stub'
    LIMIT 1;

    IF v_brand_id IS NULL THEN
        RAISE NOTICE 'fmcsa-stub brand missing — skipping IVR seed';
        RETURN;
    END IF;

    -- 2. Skip seeding if a flow with this name already exists for the brand.
    IF EXISTS (
        SELECT 1 FROM ivr_flows
        WHERE brand_id = v_brand_id AND name = 'FMCSA Carrier Qualification'
    ) THEN
        RAISE NOTICE 'FMCSA flow already seeded — skipping';
        RETURN;
    END IF;

    -- 3. Insert flow.
    INSERT INTO ivr_flows (
        brand_id, name, description,
        default_voice, default_language,
        lookup_type, lookup_config,
        default_transfer_number, transfer_timeout_seconds,
        recording_enabled, recording_consent_required
    ) VALUES (
        v_brand_id,
        'FMCSA Carrier Qualification',
        'Inbound IVR for carrier factoring qualification. Collects MC number, performs carrier lookup, routes based on eligibility.',
        'Polly.Joanna-Generative', 'en-US',
        'stub',
        '{"source": "fmcsa", "endpoint": "https://mobile.fmcsa.dot.gov/qc/services/carriers/{mc_number}"}'::JSONB,
        '+15551234567', 30,
        FALSE, TRUE
    ) RETURNING id INTO v_flow_id;

    -- Step 1: greeting
    INSERT INTO ivr_flow_steps (flow_id, brand_id, step_key, step_type, position, say_text, next_step_key)
    VALUES (v_flow_id, v_brand_id, 'greeting', 'greeting', 1,
        'Thank you for calling. We help carriers with factoring solutions. To check your eligibility, we''ll need your MC number.',
        'gather_mc');

    -- Step 2: gather_mc
    INSERT INTO ivr_flow_steps (
        flow_id, brand_id, step_key, step_type, position,
        say_text, next_step_key,
        gather_input, gather_num_digits, gather_validation_regex,
        gather_max_retries, gather_invalid_message, gather_finish_on_key
    ) VALUES (
        v_flow_id, v_brand_id, 'gather_mc', 'gather_dtmf', 2,
        'Please enter your 6 or 7 digit MC number, followed by the pound sign.',
        'lookup',
        'dtmf', NULL, '^\d{6,7}$',
        2, 'That doesn''t look like a valid MC number. Please try again.', '#');

    -- Step 3: lookup
    INSERT INTO ivr_flow_steps (
        flow_id, brand_id, step_key, step_type, position,
        say_text, lookup_input_key, lookup_store_key,
        branches
    ) VALUES (
        v_flow_id, v_brand_id, 'lookup', 'data_lookup', 3,
        'One moment while we look up your carrier information.',
        'gather_mc', 'lookup_result',
        '[{"condition": "lookup_found", "next_step_key": "say_result"}, {"condition": "lookup_not_found", "next_step_key": "not_found"}]'::JSONB);

    -- Step 4: say_result
    INSERT INTO ivr_flow_steps (flow_id, brand_id, step_key, step_type, position, say_text, next_step_key)
    VALUES (v_flow_id, v_brand_id, 'say_result', 'say_dynamic', 4,
        'We found your carrier record. Based on your information, you may qualify for our factoring program.',
        'transfer');

    -- Step 5: not_found
    INSERT INTO ivr_flow_steps (flow_id, brand_id, step_key, step_type, position, say_text, next_step_key)
    VALUES (v_flow_id, v_brand_id, 'not_found', 'say_dynamic', 5,
        'We were unable to locate a carrier with that MC number. Let me connect you with someone who can help.',
        'transfer_fallback');

    -- Step 6: transfer
    INSERT INTO ivr_flow_steps (
        flow_id, brand_id, step_key, step_type, position,
        next_step_key, branches
    ) VALUES (
        v_flow_id, v_brand_id, 'transfer', 'transfer', 6,
        'goodbye',
        '[{"condition": "transfer_completed", "next_step_key": "goodbye"}, {"condition": "transfer_failed", "next_step_key": "transfer_failed_msg"}]'::JSONB);

    -- Step 7: transfer_fallback
    INSERT INTO ivr_flow_steps (
        flow_id, brand_id, step_key, step_type, position,
        next_step_key, branches
    ) VALUES (
        v_flow_id, v_brand_id, 'transfer_fallback', 'transfer', 7,
        'goodbye',
        '[{"condition": "transfer_completed", "next_step_key": "goodbye"}, {"condition": "transfer_failed", "next_step_key": "transfer_failed_msg"}]'::JSONB);

    -- Step 8: transfer_failed_msg
    INSERT INTO ivr_flow_steps (flow_id, brand_id, step_key, step_type, position, say_text, next_step_key)
    VALUES (v_flow_id, v_brand_id, 'transfer_failed_msg', 'greeting', 8,
        'I''m sorry, all of our representatives are currently busy. Please call back during business hours or leave a message after the beep.',
        'record_vm');

    -- Step 9: record_vm
    INSERT INTO ivr_flow_steps (
        flow_id, brand_id, step_key, step_type, position,
        say_text, next_step_key,
        record_max_length_seconds, record_play_beep
    ) VALUES (
        v_flow_id, v_brand_id, 'record_vm', 'record', 9,
        'Please leave your name, MC number, and a callback number after the beep.',
        'goodbye',
        120, TRUE);

    -- Step 10: goodbye
    INSERT INTO ivr_flow_steps (flow_id, brand_id, step_key, step_type, position, say_text)
    VALUES (v_flow_id, v_brand_id, 'goodbye', 'hangup', 10,
        'Thank you for calling. Goodbye.');

    RAISE NOTICE 'FMCSA IVR flow seeded: brand_id=% flow_id=%', v_brand_id, v_flow_id;
END $$;
