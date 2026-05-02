-- direct_mail_specs: canonical print specifications for every Lob mailer
-- format we may produce. Sourced verbatim from Lob's Help Center spec pages
-- and validated against MediaBox dimensions in Lob's published template
-- PDFs (see scripts/sync_lob_specs.py for re-fetch + verify).
--
-- Drives renderer guides (artboard outlines for each format) and the
-- pre-flight validator (artwork upload bounds-check vs. ink-free zones,
-- safe zones, bleed, etc.).
--
-- Identity: (mailer_category, variant) is the public key. mailer_category
-- is broader than direct_mail_pieces.piece_type because it includes
-- letter_envelopes, card_affix, and buckslip — none of which are top-level
-- "pieces" but all of which carry print specifications we must validate.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS direct_mail_specs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mailer_category TEXT NOT NULL
        CHECK (mailer_category IN (
            'postcard',
            'letter',
            'self_mailer',
            'snap_pack',
            'booklet',
            'check',
            'card_affix',
            'buckslip',
            'letter_envelope'
        )),
    variant TEXT NOT NULL,
    label TEXT NOT NULL,

    -- Core print geometry. NULL bleed_* means "no bleed" (e.g. letters,
    -- snap packs, envelopes). All measurements are inches; the renderer
    -- multiplies by required_dpi (commonly 300) for pixel coordinates.
    bleed_w_in    NUMERIC(7,4),
    bleed_h_in    NUMERIC(7,4),
    trim_w_in     NUMERIC(7,4) NOT NULL,
    trim_h_in     NUMERIC(7,4) NOT NULL,
    safe_inset_in NUMERIC(7,4),

    -- Format-specific zone descriptors (ink_free, address_block, envelope
    -- windows, qr_zone, no_print, binding_zone, signature_image, etc.).
    -- Each value is an object with absolute or anchor-relative coordinates.
    zones JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Self-mailers / snap packs / booklets: folding + panel geometry,
    -- glue zones, fold lines, panel offsets.
    folding JSONB,

    -- Booklets: page_count_min/max/step, bind_method, print_method,
    -- personalization_supported.
    pagination JSONB,

    -- Address placement options (letters): top_first_page, insert_blank_page.
    address_placement JSONB,

    -- Envelope assignment (letters): which envelope is used for which
    -- sheet count.
    envelope JSONB,

    -- Paper, finish, DPI, color space, sided, ink saturation cap, etc.
    production JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Min order qty, send qty, SLA, spoilage, lead time, enterprise_only.
    ordering JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Lob's canonical template PDF for this format (renderer baseline).
    template_pdf_url TEXT,
    -- Variant-specific extras (booklet page-count templates, second
    -- letter template for the flat envelope, etc.).
    additional_template_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Help Center URLs this row was sourced from.
    source_urls JSONB NOT NULL DEFAULT '[]'::jsonb,

    notes TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (mailer_category, variant)
);

CREATE INDEX IF NOT EXISTS idx_direct_mail_specs_category
    ON direct_mail_specs (mailer_category);

-- Universal Lob design rules that apply across every mailer format. One
-- row keyed by `key`; updated when Lob's artboard guidance changes.
CREATE TABLE IF NOT EXISTS direct_mail_design_rules (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    description TEXT,
    source_url TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
