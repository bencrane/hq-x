-- 0019: DMaaS v1 spec data — face-aware zones for postcards + self_mailer bifolds.
--
-- Adds a new optional `faces` JSONB column to direct_mail_specs and re-seeds the
-- v1 spec rows (4 postcards + 3 self_mailer bifolds + the trifold for forward
-- compat in `folding`) with the richer zone catalog the DMaaS DSL needs.
--
-- This migration is strictly additive:
--   * No DROP. No RENAME. Existing columns and existing rows untouched (other
--     than UPDATEs to the seven v1 rows + the trifold).
--   * `faces` defaults to '[]'::jsonb so non-v1 rows continue to round-trip.
--   * Legacy `zones.ink_free` / `zones.usps_scan_warning` are retained verbatim
--     so already-shipped scaffolds and any external reader keep working.
--
-- Why now: the next directive (DMaaS scaffold-authoring managed agent) needs
-- richer zone vocabulary to author DSL constraints. The Help Center publishes
-- only the bundled `ink_free` rectangle; sub-zones (postage_indicia,
-- return_address, address_block/address_window, usps_barcode_clear) are
-- USPS DMM convention and source-tagged accordingly. Bundled `ink_free`
-- is preserved as its own zone (also surfaced as a `*_face`-namespaced alias)
-- so the agent can pick the granularity it needs.

ALTER TABLE direct_mail_specs
    ADD COLUMN IF NOT EXISTS faces JSONB NOT NULL DEFAULT '[]'::jsonb;

-- ---------------------------------------------------------------------------
-- Postcard 4x6  (face dims 4.25 x 6.25 bleed; ink_free 3.2835 x 2.375)
-- ---------------------------------------------------------------------------
UPDATE direct_mail_specs
SET faces = '[
  {
    "name": "front",
    "is_addressable": false,
    "zones": [
      {"name": "usps_scan_warning", "type": "informational", "rect_in": {"w_full_face": true, "h": 2.375, "from_bottom": 0.0}, "source": "lob_help_center", "rule": "USPS scanner conflict — avoid address-like content in bottom 2.375\""}
    ]
  },
  {
    "name": "back",
    "is_addressable": true,
    "zones": [
      {"name": "ink_free", "type": "ink_free", "rect_in": {"w": 3.2835, "h": 2.375, "from_right": 0.275, "from_bottom": 0.25}, "source": "lob_help_center", "note": "Lob bundled address+postage+barcode region; decomposed below."},
      {"name": "address_block", "type": "address_block", "rect_in": {"w": 2.7835, "h": 1.5, "from_right": 0.525, "from_bottom": 0.875}, "source": "usps_dmm", "note": "Address-text region within ink_free, above the USPS IMb clear zone."},
      {"name": "postage_indicia", "type": "postage", "rect_in": {"w": 1.0, "h": 1.0, "from_right": 0.25, "from_top": 0.25}, "source": "usps_dmm"},
      {"name": "return_address", "type": "return_address", "rect_in": {"w": 2.5, "h": 0.5, "from_left": 0.25, "from_top": 0.25}, "source": "usps_dmm"},
      {"name": "usps_barcode_clear", "type": "barcode_clear", "rect_in": {"w": 3.2835, "h": 0.625, "from_right": 0.275, "from_bottom": 0.25}, "source": "usps_dmm", "note": "USPS IMb clear zone. Width clamped to fit 4x6 face."}
    ]
  }
]'::jsonb,
    updated_at = NOW()
WHERE mailer_category = 'postcard' AND variant = '4x6';

-- ---------------------------------------------------------------------------
-- Postcard 5x7
-- ---------------------------------------------------------------------------
UPDATE direct_mail_specs
SET faces = '[
  {
    "name": "front",
    "is_addressable": false,
    "zones": [
      {"name": "usps_scan_warning", "type": "informational", "rect_in": {"w_full_face": true, "h": 2.375, "from_bottom": 0.0}, "source": "lob_help_center", "rule": "USPS scanner conflict — avoid address-like content in bottom 2.375\""}
    ]
  },
  {
    "name": "back",
    "is_addressable": true,
    "zones": [
      {"name": "ink_free", "type": "ink_free", "rect_in": {"w": 4.0, "h": 2.375, "from_right": 0.275, "from_bottom": 0.25}, "source": "lob_help_center", "note": "Lob bundled region; decomposed below."},
      {"name": "address_block", "type": "address_block", "rect_in": {"w": 3.5, "h": 1.5, "from_right": 0.525, "from_bottom": 0.875}, "source": "usps_dmm"},
      {"name": "postage_indicia", "type": "postage", "rect_in": {"w": 1.0, "h": 1.0, "from_right": 0.25, "from_top": 0.25}, "source": "usps_dmm"},
      {"name": "return_address", "type": "return_address", "rect_in": {"w": 3.0, "h": 0.5, "from_left": 0.25, "from_top": 0.25}, "source": "usps_dmm"},
      {"name": "usps_barcode_clear", "type": "barcode_clear", "rect_in": {"w": 4.0, "h": 0.625, "from_right": 0.275, "from_bottom": 0.25}, "source": "usps_dmm"}
    ]
  }
]'::jsonb,
    updated_at = NOW()
WHERE mailer_category = 'postcard' AND variant = '5x7';

-- ---------------------------------------------------------------------------
-- Postcard 6x9
-- ---------------------------------------------------------------------------
UPDATE direct_mail_specs
SET faces = '[
  {
    "name": "front",
    "is_addressable": false,
    "zones": [
      {"name": "usps_scan_warning", "type": "informational", "rect_in": {"w_full_face": true, "h": 2.375, "from_bottom": 0.0}, "source": "lob_help_center", "rule": "USPS scanner conflict — avoid address-like content in bottom 2.375\""}
    ]
  },
  {
    "name": "back",
    "is_addressable": true,
    "zones": [
      {"name": "ink_free", "type": "ink_free", "rect_in": {"w": 4.0, "h": 2.375, "from_right": 0.275, "from_bottom": 0.25}, "source": "lob_help_center", "note": "Lob bundled region; decomposed below."},
      {"name": "address_block", "type": "address_block", "rect_in": {"w": 3.5, "h": 1.5, "from_right": 0.525, "from_bottom": 0.875}, "source": "usps_dmm"},
      {"name": "postage_indicia", "type": "postage", "rect_in": {"w": 1.0, "h": 1.0, "from_right": 0.25, "from_top": 0.25}, "source": "usps_dmm"},
      {"name": "return_address", "type": "return_address", "rect_in": {"w": 3.0, "h": 0.5, "from_left": 0.25, "from_top": 0.25}, "source": "usps_dmm"},
      {"name": "usps_barcode_clear", "type": "barcode_clear", "rect_in": {"w": 4.0, "h": 0.625, "from_right": 0.275, "from_bottom": 0.25}, "source": "usps_dmm"}
    ]
  }
]'::jsonb,
    updated_at = NOW()
WHERE mailer_category = 'postcard' AND variant = '6x9';

-- ---------------------------------------------------------------------------
-- Postcard 6x11
-- ---------------------------------------------------------------------------
UPDATE direct_mail_specs
SET faces = '[
  {
    "name": "front",
    "is_addressable": false,
    "zones": [
      {"name": "usps_scan_warning", "type": "informational", "rect_in": {"w_full_face": true, "h": 2.375, "from_bottom": 0.0}, "source": "lob_help_center", "rule": "USPS scanner conflict — avoid address-like content in bottom 2.375\""}
    ]
  },
  {
    "name": "back",
    "is_addressable": true,
    "zones": [
      {"name": "ink_free", "type": "ink_free", "rect_in": {"w": 4.0, "h": 2.375, "from_right": 0.275, "from_bottom": 0.25}, "source": "lob_help_center", "note": "Lob bundled region; decomposed below."},
      {"name": "address_block", "type": "address_block", "rect_in": {"w": 3.5, "h": 1.5, "from_right": 0.525, "from_bottom": 0.875}, "source": "usps_dmm"},
      {"name": "postage_indicia", "type": "postage", "rect_in": {"w": 1.0, "h": 1.0, "from_right": 0.25, "from_top": 0.25}, "source": "usps_dmm"},
      {"name": "return_address", "type": "return_address", "rect_in": {"w": 3.0, "h": 0.5, "from_left": 0.25, "from_top": 0.25}, "source": "usps_dmm"},
      {"name": "usps_barcode_clear", "type": "barcode_clear", "rect_in": {"w": 4.0, "h": 0.625, "from_right": 0.275, "from_bottom": 0.25}, "source": "usps_dmm"}
    ]
  }
]'::jsonb,
    updated_at = NOW()
WHERE mailer_category = 'postcard' AND variant = '6x11';

-- ---------------------------------------------------------------------------
-- Self-mailer 6x18 bifold (vertical fold at x=9; cover = outside_left_panel,
-- 9"w × 6"h. Cover-panel-local coords below.)
-- ---------------------------------------------------------------------------
UPDATE direct_mail_specs
SET folding = '{
  "folded_w_in": 9.0,
  "folded_h_in": 6.0,
  "panel_count": 4,
  "panel_offset_in": null,
  "fold_axis": "vertical",
  "fold_orientation": "horizontal",
  "fold_lines_in_from_left": [9.0],
  "panel_naming": "left_right",
  "cover_panel": "outside_left_panel",
  "opening_edges": ["top", "bottom", "left_outer"],
  "glue_zone_width_in": 0.25,
  "fold_gutter_half_width_in": 0.125,
  "glue_adhesive": "Stain-resistant low-tack clear fugitive glue, within 0.25\" of opening edges"
}'::jsonb,
    faces = '[
      {
        "name": "outside",
        "is_addressable": true,
        "cover_panel": "outside_left_panel",
        "panel_zones": {
          "outside_left_panel": [
            {"name": "ink_free", "type": "ink_free", "rect_in": {"w": 4.0, "h": 2.375, "from_right": 0.15, "from_bottom": 0.25}, "source": "lob_help_center", "note": "Lob bundled region; decomposed below."},
            {"name": "address_window", "type": "address_window", "rect_in": {"w": 3.5, "h": 1.5, "from_right": 0.4, "from_bottom": 0.875}, "source": "usps_dmm"},
            {"name": "postage_indicia", "type": "postage", "rect_in": {"w": 1.0, "h": 1.0, "from_right": 0.25, "from_top": 0.25}, "source": "usps_dmm"},
            {"name": "usps_barcode_clear", "type": "barcode_clear", "rect_in": {"w": 4.0, "h": 0.625, "from_right": 0.15, "from_bottom": 0.25}, "source": "usps_dmm"}
          ]
        }
      },
      {
        "name": "inside",
        "is_addressable": false,
        "panel_zones": {}
      }
    ]'::jsonb,
    updated_at = NOW()
WHERE mailer_category = 'self_mailer' AND variant = '6x18_bifold';

-- ---------------------------------------------------------------------------
-- Self-mailer 12x9 bifold (horizontal fold at y=6 from top; cover =
-- outside_top_panel, 12"w × 6"h.)
-- ---------------------------------------------------------------------------
UPDATE direct_mail_specs
SET folding = '{
  "folded_w_in": 12.0,
  "folded_h_in": 6.0,
  "panel_count": 4,
  "panel_offset_in": null,
  "fold_axis": "horizontal",
  "fold_orientation": "horizontal",
  "fold_lines_in_from_top": [6.0],
  "panel_naming": "top_bottom",
  "cover_panel": "outside_top_panel",
  "opening_edges": ["top", "left_outer", "right_outer"],
  "glue_zone_width_in": 0.25,
  "fold_gutter_half_width_in": 0.125,
  "glue_adhesive": "Stain-resistant low-tack clear fugitive glue, within 0.25\" of opening edges"
}'::jsonb,
    faces = '[
      {
        "name": "outside",
        "is_addressable": true,
        "cover_panel": "outside_top_panel",
        "panel_zones": {
          "outside_top_panel": [
            {"name": "ink_free", "type": "ink_free", "rect_in": {"w": 4.0, "h": 2.375, "from_right": 0.25, "from_bottom": 0.15}, "source": "lob_help_center", "note": "Lob bundled region; decomposed below."},
            {"name": "address_window", "type": "address_window", "rect_in": {"w": 3.5, "h": 1.5, "from_right": 0.5, "from_bottom": 0.775}, "source": "usps_dmm"},
            {"name": "postage_indicia", "type": "postage", "rect_in": {"w": 1.0, "h": 1.0, "from_right": 0.25, "from_top": 0.25}, "source": "usps_dmm"},
            {"name": "usps_barcode_clear", "type": "barcode_clear", "rect_in": {"w": 4.0, "h": 0.625, "from_right": 0.25, "from_bottom": 0.15}, "source": "usps_dmm"}
          ]
        }
      },
      {
        "name": "inside",
        "is_addressable": false,
        "panel_zones": {}
      }
    ]'::jsonb,
    updated_at = NOW()
WHERE mailer_category = 'self_mailer' AND variant = '12x9_bifold';

-- ---------------------------------------------------------------------------
-- Self-mailer 11x9 bifold (horizontal fold at y=5 with 1" panel offset; cover
-- = outside_top_panel, 11"w × 5"h.)
-- ---------------------------------------------------------------------------
UPDATE direct_mail_specs
SET folding = '{
  "folded_w_in": 11.0,
  "folded_h_in": 5.0,
  "panel_count": 4,
  "panel_offset_in": 1.0,
  "fold_axis": "horizontal",
  "fold_orientation": "horizontal",
  "fold_lines_in_from_top": [5.0],
  "panel_naming": "top_bottom",
  "cover_panel": "outside_top_panel",
  "opening_edges": ["top", "left_outer", "right_outer"],
  "glue_zone_width_in": 0.25,
  "fold_gutter_half_width_in": 0.125,
  "panel_offset_note": "1\" offset between top (cover, 5\") and bottom (4\") panels when folded",
  "glue_adhesive": "Stain-resistant low-tack clear fugitive glue, within 0.25\" of opening edges"
}'::jsonb,
    faces = '[
      {
        "name": "outside",
        "is_addressable": true,
        "cover_panel": "outside_top_panel",
        "panel_zones": {
          "outside_top_panel": [
            {"name": "ink_free", "type": "ink_free", "rect_in": {"w": 4.0, "h": 2.375, "from_right": 0.25, "from_bottom": 0.15}, "source": "lob_help_center", "note": "Lob bundled region; decomposed below."},
            {"name": "address_window", "type": "address_window", "rect_in": {"w": 3.5, "h": 1.5, "from_right": 0.5, "from_bottom": 0.775}, "source": "usps_dmm"},
            {"name": "postage_indicia", "type": "postage", "rect_in": {"w": 1.0, "h": 1.0, "from_right": 0.25, "from_top": 0.25}, "source": "usps_dmm"},
            {"name": "usps_barcode_clear", "type": "barcode_clear", "rect_in": {"w": 4.0, "h": 0.625, "from_right": 0.25, "from_bottom": 0.15}, "source": "usps_dmm"}
          ]
        }
      },
      {
        "name": "inside",
        "is_addressable": false,
        "panel_zones": {}
      }
    ]'::jsonb,
    updated_at = NOW()
WHERE mailer_category = 'self_mailer' AND variant = '11x9_bifold';

-- ---------------------------------------------------------------------------
-- Self-mailer 17.75x9 trifold (NOT v1 implementation; folding metadata
-- normalized so the resolver's panel-derivation logic accepts it without
-- refactor. faces remains empty until a future directive populates it.)
-- ---------------------------------------------------------------------------
UPDATE direct_mail_specs
SET folding = '{
  "folded_w_in": 6.0,
  "folded_h_in": 9.0,
  "panel_count": 3,
  "panel_offset_in": 0.25,
  "fold_axis": "vertical",
  "fold_orientation": "vertical",
  "fold_type": "C-fold inward",
  "fold_lines_in_from_left": [5.875, 11.875],
  "panel_naming": "left_middle_right",
  "cover_panel": "outside_left_panel",
  "opening_edges": ["top", "bottom", "right_outer"],
  "glue_zone_width_in": 0.5,
  "fold_gutter_half_width_in": 0.125,
  "glue_zone_dimensions_in": {"w": 9.0, "h": 0.5, "x_in_from_left": 12.0, "anchor": "bottom"},
  "glue_adhesive": "Stain-resistant low-tack clear fugitive glue"
}'::jsonb,
    updated_at = NOW()
WHERE mailer_category = 'self_mailer' AND variant = '17.75x9_trifold';
