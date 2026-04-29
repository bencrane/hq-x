# Directive handoff — DMaaS Lob mailer-spec v2 zones

**Status:** Shipped. PR [hq-x#13](https://github.com/bencrane/hq-x/pull/13) merged to `main` as `a5c1fbb`.
**Target directive:** "Complete Lob mailer-spec data for postcard + self_mailer (DMaaS v1)".
**Successor directive can be issued:** Yes. The substrate the scaffold-authoring managed agent needs is in place.

This doc is the handoff back to the AI agent that authored the original directive. It captures the shape of what shipped, the decisions made, and the loose ends — so the next directive (DMaaS scaffold-authoring managed agent) can be drafted with accurate context.

---

## 1. What shipped

| Artifact | Location |
|---|---|
| Migration (additive) | `migrations/0019_lob_specs_v2_zones.sql` |
| Spec data v2 | `data/lob_mailer_specs.json` |
| Resolver + region typing | `app/dmaas/service.py` |
| MCP tools `list_specs` / `get_spec` | `app/mcp/dmaas.py` |
| Drift guard (zones + MediaBox) | `scripts/sync_lob_specs.py` |
| Tests | `tests/test_dmaas_spec_binding.py` (new), `tests/test_dmaas_mcp.py` (extended) |
| Docs | `CLAUDE.md` (root, new), `data/lob_mailer_specs.json#_meta.v2_changelog` |

**Test counts:** 214 → 296 total (+82); DMaaS-specific 73 → 100 (+27). All green. No regressions.

---

## 2. Surface / panel / region model adopted

### 2.1 Storage shape

Three coexisting shapes — pick the right one for the use case:

| Shape | Where | Audience |
|---|---|---|
| Flat `zones: {name: Rect}` | `SpecBinding.zones` | Solver (kiwi). Consumed verbatim. Includes legacy + v2 names + aliases. |
| Typed `regions: list[RegionDescriptor]` | `SpecBinding.regions` | MCP-facing. Each has `name`, `type`, `face`, `panel`, `derived_from`, `source`, `aliases`, `note`. |
| Face descriptors `faces: list[FaceDescriptor]` | `SpecBinding.faces` | UI / agent overview. `name`, `rect`, `is_addressable`, `cover_panel`. |

The DSL grammar is unchanged. The agent references zones by name; the typed `regions` give it the metadata it needs to *pick* the right name.

### 2.2 Data vs derive

| Datum | Stored vs Derived | Reason |
|---|---|---|
| Panel rectangles (self-mailers) | **Derived** in `_derive_panels` from `folding.fold_lines_in_from_left` / `_from_top` + `panel_naming` + bleed margin | `folding` is canonical. Denormalizing creates drift on a fold-line edit. |
| Cover-panel zones (address_window, postage, barcode_clear) | **Stored** in `direct_mail_specs.faces[].panel_zones` in panel-local coords | Lob publishes these per-format; not derivable from fold lines. |
| Postcard back-face zones (address_block, postage, return_address, barcode_clear, ink_free) | **Stored** in `direct_mail_specs.faces[].zones` in face-local coords | Same reason — face-local positions Lob (or USPS DMM) publishes per format. |
| `*_safe` rectangles | **Derived** from face/panel rect minus `safe_inset_in` | One rule for everything. |
| Glue strips | **Derived** from `folding.opening_edges` + `glue_zone_width_in` | Edge list is data, geometry is one rule. Trifold's explicit `glue_zone_dimensions_in` is honored as well. |
| Fold gutters | **Derived** from fold-line positions + `fold_gutter_half_width_in` | One rule, all fold counts. |

### 2.3 Single-canvas-per-face (Option A from the directive)

Front + back of a postcard share the same canvas; outside + inside of a self-mailer share the same canvas. A scaffold designs for ONE face at a time. Zones are face-namespaced (`back_address_block`, `front_safe`, `outside_top_panel_safe`) so the agent picks which face's geometry applies. Two-face designs become two scaffolds joined at the campaign level.

This matches the directive's recommendation and was chosen because (a) the existing `bleed_w_in × bleed_h_in` is one face's worth, and (b) doubling the canvas to fit both faces side-by-side would invalidate every existing scaffold.

### 2.4 Region types currently emitted

`face` | `safe` | `panel` | `address_block` | `address_window` | `postage` | `barcode_clear` | `return_address` | `informational` | `ink_free` | `fold_gutter` | `glue`

Reserved (not yet emitted; future formats):

`window_cutout` (letter envelopes) | `perforation` (snap-pack)

---

## 3. Required-zone catalog the scaffold-authoring agent can rely on

Every v1 spec passed through `bind_spec_zones` produces these zones. Names are normative and locked in by `tests/test_dmaas_spec_binding.py`. The list is also documented in the docstring of the new `get_spec` MCP tool.

### 3.1 Postcard (4x6, 5x7, 6x9, 6x11)

Faces:
- `front_face`, `back_face` — full bleed rect
- `front_safe`, `back_safe` — face minus `safe_inset_in`

Front face zones:
- `front_usps_scan_warning` (informational; bottom 2.375")

Back face zones (mutually non-overlapping by invariant):
- `back_address_block` (3.5×1.5", from_right=0.525, from_bottom=0.875; 2.7835×1.5 for 4x6)
- `back_postage_indicia` (1×1", top-right)
- `back_return_address` (3×0.5", top-left; 2.5×0.5 for 4x6)
- `back_usps_barcode_clear` (4×0.625", bottom of ink_free; 3.2835×0.625 for 4x6)

Plus legacy:
- `back_ink_free` (4×2.375", Lob's bundled rect; 3.2835×2.375 for 4x6) — **alias** of the typed `ink_free` region. Kept so the `hero-headline-postcard` seed scaffold continues to solve.
- `safe_zone` (= front_safe + back_safe, both projected to canvas)
- `canvas`, `trim`

### 3.2 Self-mailer bifold (11x9_bifold, 12x9_bifold, 6x18_bifold)

Faces:
- `outside_face`, `inside_face` — full bleed rect
- One of `outside_top_panel` / `outside_bottom_panel` (11x9 + 12x9: horizontal fold, top is cover) **or** `outside_left_panel` / `outside_right_panel` (6x18: vertical fold, left is cover); plus inside variants
- `<panel_name>_safe` for every panel — panel rect minus `safe_inset_in`

Cover-panel zones (mutually non-overlapping by invariant):
- `outside_address_window` (3.5×1.5")
- `outside_postage_indicia` (1×1")
- `outside_usps_barcode_clear` (4×0.625")
- `outside_ink_free` (4×2.375", Lob bundled, alias)

Per-format:
- `glue_zone_top` / `_bottom` / `_left` / `_right` — emitted only for declared `opening_edges`. 11x9 + 12x9 bifolds: top + left + right (horizontal-fold; bottom is the fold). 6x18: top + bottom + left (vertical-fold; right is the fold).
- `fold_gutter_1` (one fold for bifolds; trifold gets `fold_gutter_1` + `fold_gutter_2`)

### 3.3 Trifold (17.75x9_trifold) — out of v1 implementation but model-complete

Panels derive automatically from `folding.fold_lines_in_from_left: [5.875, 11.875]` + `panel_naming: "left_middle_right"`:
- `outside_left_panel`, `outside_middle_panel`, `outside_right_panel` (+ inside variants)
- `fold_gutter_1`, `fold_gutter_2`
- Glue strips from `opening_edges`
- Explicit `glue_zone_dimensions_in: {w: 9.0, h: 0.5, x_in_from_left: 12.0, anchor: "bottom"}` honored as `glue_zone_explicit`

`faces[].panel_zones` is empty. Populating it for the trifold is **data-only**; no code change required. That's the test of the model and it passes.

---

## 4. Source-of-truth notes (where the numbers came from)

The directive said: prose wins from the Help Center; geometry wins from the PDF when they disagree.

| Datum | Source | Tag in `regions[].source` |
|---|---|---|
| Bleed / trim dims | Help Center prose; PDF MediaBox spot-checked | `lob_help_center` |
| `ink_free` rect on each format | Help Center prose | `lob_help_center` |
| Fold lines, panel offsets, panel count, glue width | Help Center prose | `lob_help_center` |
| Cover-panel address-window position (Lob `from_center_fold` etc.) | Help Center prose | `lob_help_center` |
| `address_block` / `address_window` (decomposed text-only sub-rect) | USPS DMM convention | `usps_dmm` |
| `postage_indicia` (1×1 top-right) | USPS DMM convention | `usps_dmm` |
| `return_address` (3×0.5 top-left, postcards) | USPS DMM convention | `usps_dmm` |
| `usps_barcode_clear` (4×0.625 IMb clear zone) | USPS DMM convention | `usps_dmm` |
| Panel rects, safes, glue strips, fold gutters | Computed | `derived` |

### 4.1 Why `ink_free` was decomposed

Lob publishes ONE rectangle for the address area: `ink_free`, 4×2.375" (3.2835 for 4x6). The directive's §10 sync-script invariant required `address_block`, `postage_indicia`, `return_address`, `usps_barcode_clear` to be **mutually non-overlapping** on the back face (postcards) / cover panel (self-mailers).

Two ways to satisfy that:
1. Treat `address_block` = full `ink_free` rect, and put `usps_barcode_clear` outside `ink_free`. Doesn't work — IMb is *inside* the bottom of ink_free per USPS.
2. Treat `address_block` as a smaller sub-rect of `ink_free` (just the address text), and `usps_barcode_clear` as a separate sub-rect along ink_free's bottom. They abut but don't overlap.

I went with (2). Concrete numbers: `address_block` is 3.5×1.5", positioned 0.875" up from face/panel bottom (= 0.625" IMb + 0.25" margin). `usps_barcode_clear` is 4×0.625" at face/panel bottom-right with the same edge offsets as `ink_free`.

The original Lob `ink_free` is preserved as its own typed region (`type: "ink_free"`, `source: lob_help_center`) and as a flat-zones alias (`back_ink_free` / `outside_ink_free`). The agent can pick the granularity it wants: bundled `ink_free` or decomposed sub-zones.

This is the single most opinionated decision in the PR. If the directive author disagrees, the alternative is to weaken the non-overlap invariant in §10 to apply only to `{postage_indicia, return_address}` (the strictly-disjoint pair).

### 4.2 Fold-axis normalization for 11x9 + 12x9 bifolds

The original `data/lob_mailer_specs.json` had:

```json
"11x9_bifold": { "fold_orientation": "vertical", "fold_lines_in_from_top": [5.0] }
"12x9_bifold": { "fold_orientation": "vertical", "fold_lines_in_from_top": [6.0] }
```

The directive's §4.2 says these two have *top* and *bottom* panels with a 1" panel offset on 11x9 — implying a **horizontal** fold line separating top/bottom panels. That's geometrically inconsistent with `fold_lines_in_from_top: 5.0` if you also believe `folded_w_in: 6.0, folded_h_in: 9.0` (a horizontal fold doesn't reduce width). The two source-data fields contradicted each other.

I followed the directive. Final normalized data for these two bifolds:

```json
"fold_axis": "horizontal",
"fold_lines_in_from_top": [5.0],   // 11x9: top panel 5" tall, bottom 4" tall
"folded_w_in": 11.0,               // height matches top-panel height (5")
"folded_h_in": 5.0,
```

For 6x18 the source data was already consistent (`fold_lines_in_from_left: [9.0]`, vertical fold, 9×6 folded) and was kept.

Added an explicit `fold_axis: "vertical" | "horizontal"` field to every `folding` blob so the resolver doesn't have to guess from which key is set.

If you have authoritative info from a Lob template PDF that disagrees with this interpretation, the data is one migration away from being corrected — none of it is denormalized into the resolver code.

### 4.3 Where Lob and the USPS DMM are silent

* No published position for `postage_indicia` distinct from `ink_free` — used USPS DMM convention "top right corner area, 1.625"×1.0" reserved for postage" but rounded to 1.0×1.0 to fit 4x6.
* No published `return_address` rectangle — used the conventional top-left 3×0.5" band on postcards. Not emitted for self-mailers (the cover panel is too constrained; if needed, add to `faces[].panel_zones`).
* Lob's IMb position is not given a coordinate; USPS DMM specifies 4.75" × 0.625" but I clamped width to fit the per-format ink_free.

All `usps_dmm`-tagged numbers are guard-rails, not pixel-perfect — they're the constraints the agent should reason against ("don't put a headline on top of the address block"). They're not where the actual barcode gets rendered (Lob does that at print time).

---

## 5. The new MCP surface

```
list_specs(category?: str) -> {count, specs: [{id, mailer_category, variant, label, bleed_*, trim_*, full_bleed, addressable_face_count, has_faces_v2}]}
get_spec(category: str, variant: str) -> {spec, dpi, canvas, zones, regions, faces}
```

Both wrap directly into the service layer (no HTTP hop). Bearer-token auth at the ASGI boundary applies (existing middleware from `ae5abc3`). Tests cover happy path + 404. The bearer-auth tests already cover the wrapper; the new tools inherit that.

The agent's expected workflow:

```
agent: list_specs(category="postcard")
  → catalog of {category, variant, label, bleed dims}
agent: get_spec(category="postcard", variant="6x9")
  → {zones: {back_address_block, back_postage_indicia, ...}, regions: [...]}
agent: validate_constraints(spec_category="postcard", spec_variant="6x9", constraint_specification={...refers to back_address_block...}, sample_content={...})
  → tight inner loop
agent: create_scaffold(slug="...", compatible_specs=[{category: "postcard", variant: "6x9"}], constraint_specification={...})
```

---

## 6. Loose ends / caveats / what's NOT in this PR

* **Booklet specs** (`9x6_digital`, `8.375x5.375_offset`) untouched — directive said don't touch. No bleed dims, no `template_pdf_url`. Separate workstream.
* **Snap-pack** (`8.5x11`) carries legacy `zones` only; `faces` empty. The model accommodates it (see §3 of PR description) but no `perforation` region type emitted yet.
* **Letters + letter envelopes** unchanged. Their `zones` (clear_space, qr_code, window_top/bottom, etc.) remain as-is. No `attachments` mechanism in the schema yet.
* **Trifold** has folding metadata normalized but `faces` empty — out of v1 scope. Populating it is data-only, no code change.
* **Front/back face awareness in DSL**: the DSL doesn't enforce face consistency. An author could write a scaffold that mixes `back_address_block` and `front_safe` constraints. That's not a face-consistent scaffold but the solver will accept it. If the next agent should enforce face consistency, that's a `validate_constraints`-time check (not a binding-time check).
* **The seeded `hero-headline-postcard` scaffold** still references `safe_zone` (the legacy whole-trim-minus-inset zone). I did not migrate it to `front_safe` or `back_safe` because the scaffold is face-agnostic by design (just constrains content to the safe area). The legacy `safe_zone` is preserved by the resolver. Future scaffolds should prefer `front_safe` / `back_safe`.
* **PDF MediaBox check**: the existing `sync_lob_specs.py` PDF check still runs; I extended the script with zone-catalog checks but didn't re-fetch every PDF to verify the v2 numbers don't drift PDF-side (the v2 numbers are face/panel-local, not MediaBox-derivable). Fold positions and panel offsets should still be cross-checked against the PDFs by eye when Lob updates them. The script's PDF check would still catch a bleed-dim drift.
* **CI**: still none. The script's existence + `CLAUDE.md` / `pyproject.toml` exposure is the drift guard.

---

## 7. Recommendations for the successor directive (scaffold-authoring agent)

1. **Tooling the agent should use:** `list_specs` → pick a target → `get_spec` to learn zone names → `validate_constraints` in a loop → `create_scaffold` once it solves cleanly. The agent never has to scrape HTML or read JSON files.
2. **Prompt the agent with the v1 zone vocabulary** (§3 above) so it picks names that exist. The `get_spec` response carries everything but the agent should know the typed-region taxonomy.
3. **Face-consistency authoring rule**: instruct the agent to author one scaffold per face. A back-face scaffold references only `back_*` zones. Mixing front + back zones in one scaffold is a design smell.
4. **Fallback to `safe_zone` is OK**: simple "content must fit in the safe area" scaffolds can use the legacy `safe_zone` and remain face-agnostic. The hero-headline-postcard seed is the example.
5. **For self-mailers, the cover panel is the addressable face.** Scaffolds for the cover constrain content to `<cover>_safe` and avoid `outside_address_window`, `outside_postage_indicia`, `outside_usps_barcode_clear`, `glue_zone_*`, and `fold_gutter_*`.
6. **Trifold + snap-pack + letter + letter-envelope authoring** is a follow-up directive. Don't combine with the bifold + postcard authoring agent; the prompt becomes too long and the failure modes too varied.
7. **One thing I'd call out as a useful future invariant**: the agent should `preview_scaffold` against every entry in `compatible_specs` (the `create_scaffold` path already does this server-side — the agent doesn't have to). If a scaffold solves on 6x9 but not 4x6 (because intrinsics don't fit), `create_scaffold` returns a structured error per the existing pattern.

---

## 8. Quick reference: invariants the test suite locks in

* All required zones in §3.1 / §3.2 are present in `binding.zones` for every v1 spec.
* On postcard back face, the four sub-zones are mutually non-overlapping.
* On self-mailer cover panel, the three sub-zones are mutually non-overlapping.
* Every `*_safe` rectangle is fully inside its parent face/panel.
* Concrete pixel coordinates locked in for one postcard (`back_address_block` on 6x9) and one self-mailer (`outside_top_panel` on 11x9).
* Trifold panel derivation produces 6 panels (3 × outside + 3 × inside) and 2 fold gutters even with empty `faces`.
* All `compatible_specs` referenced by `data/dmaas_seed_scaffolds.json` continue to expose `safe_zone` (back-compat).
* `get_spec` MCP tool returns a JSON-serializable dict with `zones`, `regions`, `faces`, `canvas`.

If you're drafting the successor directive, you can rely on every one of these holding for the lifetime of this codebase — they're enforced by `tests/test_dmaas_spec_binding.py` and `scripts/sync_lob_specs.py`.
