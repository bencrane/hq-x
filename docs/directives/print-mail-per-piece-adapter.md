# Directive: Print & Mail per-piece adapter (Lob primary, PostGrid skim, canonical event audit)

**Context:** You are working inside the `hq-x` repository. Read [CLAUDE.md](../../CLAUDE.md), [docs/strategic-direction-owned-brand-leadgen.md](../strategic-direction-owned-brand-leadgen.md), and [docs/lob-integration.md](../lob-integration.md) before starting.

**Scope clarification on autonomy:** This directive bundles light research + a focused implementation. You have judgment over the *shape* of internal types and per-piece error packaging within the constraints below. You do NOT have judgment over:
- Whether to use Lob's Campaigns API path. You don't. This directive is the Print & Mail API path. The existing Campaigns API path (`app/services/dmaas_campaign_activation.py`, `app/services/lob_audience_csv.py`) stays untouched.
- Whether to build a `DirectMailProvider` ABC / Protocol / abstract base class. You don't. This is single-provider (Lob) for the implementation phase. The PostGrid notes are research output, not code.
- Whether to wire the new primitive into step activation / multi-step scheduler. You don't. That is a follow-up directive.
- Whether to introduce a new public router. You don't. The primitive is a service surface; the existing one-off routes in `app/routers/direct_mail.py` stay.

**Background:** Per [strategic-direction-owned-brand-leadgen.md §4.1 + §9.1](../strategic-direction-owned-brand-leadgen.md), owned-brand initiatives need per-recipient bespoke HTML/PDF on every direct-mail piece in the sequence. Lob's Campaigns API does not support per-row creative selection — its `mergeVariableColumnMapping` substitutes values into one shared template. The Print & Mail API (`POST /v1/postcards`, `/v1/self_mailers`, `/v1/letters`, `/v1/snap_packs`, `/v1/booklets`) accepts the artwork field(s) as `html_string | tmpl_id | remote_file_url | local_file_path` per call, so each call carries a fully-unique creative. That is the substrate this directive builds. **All five Print & Mail piece types are in scope** — postcard, letter, self_mailer, snap_pack, booklet — because the per-recipient creative pattern applies uniformly across the touch sequence and the data model + webhook substrate already covers all five today.

Lob is the primary provider. PostGrid is the documented backup. We are not building a provider abstraction layer ahead of demand, but the **canonical webhook event taxonomy** in [app/webhooks/lob_normalization.py](../../app/webhooks/lob_normalization.py) is the one boundary that's expensive to redo later — so we audit it now and document any provider-leakage rather than retrofit during a future PostGrid build.

**Critical existing-state facts (verify before building):**

- The Lob Print & Mail per-piece create functions already exist in [app/providers/lob/client.py](../../app/providers/lob/client.py): `create_postcard`, `create_letter`, `create_self_mailer`, `create_snap_pack`, `create_booklet`. They accept `payload` dicts and per-call `idempotency_key`. Read those signatures (lines ~413, 494, 575, 656, 735) before designing the new service surface. Each piece type has a distinct artwork-field shape: postcard uses `front`+`back`, self_mailer uses `inside`+`outside` (with size-driven fold semantics), snap_pack uses `inside`+`outside` (8.5"×11" inside, 6"×18" outside), letter uses a single `file` (one PDF/HTML covering all pages), booklet uses a single `file` (multipage). The `PieceSpec` shape in §B1 is a discriminated union that mirrors this surface 1:1.
- The piece-persistence layer already exists at [app/direct_mail/persistence.py](../../app/direct_mail/persistence.py): `upsert_piece(provider_slug='lob', external_piece_id=..., piece_type=..., ...)`. `provider_slug` is already a column on `direct_mail_pieces`, defaulted to `'lob'`. The data model is provider-pluggable today.
- The canonical webhook event taxonomy lives in [app/webhooks/lob_normalization.py](../../app/webhooks/lob_normalization.py). Internal events are namespaced `piece.*` (e.g. `piece.created`, `piece.mailed`, `piece.delivered`, `piece.returned`). The `_PIECE_STATUS_MAPPING` collapses transit micro-states into a small status set: `queued | rendered | in_transit | delivered | returned | rejected | failed | canceled | pickup_available | issue`.
- The existing per-piece routes ([app/routers/direct_mail.py:671, 728, 785](../../app/routers/direct_mail.py)) are operator/ad-hoc — they take one payload and create one piece. They are NOT the batch fan-out we need for per-recipient creative across an audience. Do not modify those routes.
- `LOB_API_KEY` is in Doppler `hq-x/dev`. There is no PostGrid key today (intentional — research only).

---

## Existing code to read before starting

In order:

1. [docs/strategic-direction-owned-brand-leadgen.md](../strategic-direction-owned-brand-leadgen.md) — §4 (per-recipient bespoke creative), §4.1 (provider implications), §9.1 (audience-size-1 vs creative_ref-on-membership). The latter is **resolved** as: Print & Mail API per piece, no Lob `cmp_*` involved. This directive locks that resolution.
2. [docs/lob-integration.md](../lob-integration.md) — current Lob surface inventory.
3. [app/providers/lob/client.py](../../app/providers/lob/client.py) — focus on `_piece_create`, `create_postcard`, `create_letter`, `create_self_mailer`, `build_idempotency_material`, `LobProviderError`.
4. [app/direct_mail/persistence.py](../../app/direct_mail/persistence.py) — `upsert_piece`, `append_piece_event`, `update_piece_status`, `project_cost_cents`.
5. [app/direct_mail/addresses.py](../../app/direct_mail/addresses.py) — address normalization + suppression-hash convention.
6. [app/webhooks/lob_normalization.py](../../app/webhooks/lob_normalization.py) — full file.
7. [app/webhooks/lob_processor.py](../../app/webhooks/lob_processor.py) — how webhook → canonical event → `direct_mail_piece_events` row.
8. [migrations/0011_direct_mail_lob.sql](../../migrations/0011_direct_mail_lob.sql) — `direct_mail_pieces`, `direct_mail_piece_events`, `suppressed_addresses`.
9. [app/routers/direct_mail.py](../../app/routers/direct_mail.py) lines 671–800 — the existing one-off create routes (precedent for how `lob_client.create_*` is called and how the result feeds `upsert_piece`).
10. [api-reference-docs-new/lob/api-reference/03-print-and-mail-api/](../../../api-reference-docs-new/lob/api-reference/03-print-and-mail-api/) — Lob Print & Mail reference docs (postcards, self_mailers, letters).
11. [api-reference-docs-new/lob/api-reference/07-webhooks/](../../../api-reference-docs-new/lob/api-reference/07-webhooks/) — Lob webhook event taxonomy. Cross-reference against `_EVENT_TYPE_MAPPING` in `lob_normalization.py`.
12. Lob Print & Mail per-type create payloads: [postcards](../../../api-reference-docs-new/lob/api-reference/03-print-and-mail-api/01-postcards/04-create.md), [self_mailers](../../../api-reference-docs-new/lob/api-reference/03-print-and-mail-api/02-self-mailers/04-create.md), [snap_packs](../../../api-reference-docs-new/lob/api-reference/03-print-and-mail-api/05-snap-packs/04-create.md), [booklets](../../../api-reference-docs-new/lob/api-reference/03-print-and-mail-api/06-booklets/04-create.md). The local mirror's letter docs are stubs — for the letter payload shape, read `app/providers/lob/client.py:create_letter` plus Lob's live docs (lob.com/docs#letters). The letter payload's distinguishing fields are `file` (single artwork), `color`, `double_sided`, `address_placement` ("top_first_page" vs "insert_blank_page"), optional `extra_service` (e.g. "certified", "registered"), optional `cards`, `buckslips`, `return_envelope`, and the perforated-tear-off use-case.

---

## Phase A — Research deliverables (notes files only, no code)

### A1. PostGrid Print & Mail API skim

**File:** `docs/research/postgrid-print-mail-api-notes.md` (new)

Spend 30–60 min reading the PostGrid Print & Mail public docs (postgrid.com/docs). Produce a notes file capturing:

1. **Per-piece create endpoints — all five types**: postcard, letter, self_mailer, snap_pack, booklet. For each type: method + path + key fields. Specifically: how the artwork is supplied (HTML / template id / remote URL / file upload — and which field name(s) PostGrid uses, since per-type the shape differs: PostGrid's analog of postcard `front`+`back`, self_mailer `inside`+`outside`, letter single-`file`, etc.), how merge variables are passed, whether send-date / mail class / metadata are first-class. If PostGrid lacks a type Lob supports (e.g. snap_pack or booklet), call that out explicitly.
2. **Per-piece read/cancel/list**: paths + IDs.
3. **ID prefix conventions**: PostGrid's analog of Lob's `psc_` (postcard), `ltr_` (letter), `sfm_` (self_mailer), `ord_` (snap_pack — note this is `ord_` not `snp_`), `bkl_` (booklet). Note the prefix (or lack thereof) for each type.
4. **Auth header**: name + format.
5. **Webhook event taxonomy**: list every event the PostGrid API can emit for a piece's lifecycle. Map each to the closest existing hq-x canonical event (the `piece.*` keys in [app/webhooks/lob_normalization.py](../../app/webhooks/lob_normalization.py)). Flag any PostGrid event that does NOT have a clean canonical-event match.
6. **Rate limits**: documented per-second / per-minute / burst limits, plus any documented batch endpoints.
7. **Address verification**: whether PostGrid offers it inline at piece-create time, or as a separate endpoint.
8. **Idempotency**: whether PostGrid honors an `Idempotency-Key` header (or query param), and at what scope.
9. **Cost surfacing**: whether the API echoes per-piece cost on create (Lob does, e.g. `body.thumbnails` and `body.expected_delivery_date` plus pricing in some response paths).

The notes file is a reference, not a design doc. Bullet form. ~1–2 pages. End with one short section "Provider gaps relative to Lob" listing anything PostGrid lacks that Lob has (or vice-versa).

### A2. Canonical event taxonomy audit

**File:** `docs/research/canonical-piece-event-taxonomy.md` (new)

Read [app/webhooks/lob_normalization.py](../../app/webhooks/lob_normalization.py) end-to-end. Produce a notes file documenting the canonical hq-x `piece.*` event vocabulary as the **provider-neutral contract** that downstream analytics consumes.

Sections:

1. **Canonical event list**: every `piece.*` value in `_EVENT_TYPE_MAPPING` and `_PIECE_STATUS_MAPPING`, with one-line semantics. This is the read-side contract.
2. **Canonical piece-status enum**: the full set of values in `direct_mail_pieces.status` per `_PIECE_STATUS_MAPPING`. Confirm all are populated by at least one event mapping. Flag any gaps.
3. **Lob-specific leakage scan**: walk every key in `_EVENT_TYPE_MAPPING` and ask "does this name describe a Lob-specific concept, or a general direct-mail concept?" Examples to check:
   - `informed_delivery.*` — is "Informed Delivery" a USPS feature (general) or a Lob feature (specific)? Document.
   - `certified.*` — USPS Certified Mail (general).
   - `return_envelope.*` — Lob's return-envelope feature, but the underlying concept (a return envelope's lifecycle) is general.
   - `rendered_pdf` / `rendered_thumbnails` — provider artifacts that may or may not exist on PostGrid.
   For each Lob-leaky name, propose a renamed-canonical alternative (do NOT apply the rename — just propose). End the section with a list of "events that are safe as-is" vs "events that should be renamed when a second provider lands."
4. **PostGrid coverage matrix** (uses A1 output): cross-reference every PostGrid webhook event against the canonical vocabulary. Three buckets:
   - clean match (canonical name fits PostGrid's event verbatim)
   - close match (canonical name fits with a small mapping; document the mapping)
   - no match (PostGrid emits something hq-x has no canonical name for; propose a name and note it as a gap to fill on PostGrid integration)
5. **Conclusion**: one paragraph stating whether the canonical vocabulary as it stands is sufficient for PostGrid drop-in, or which specific renames/additions would be needed. This is the deciding output of the audit — if it says "vocabulary is fine as-is," PostGrid integration later is purely a translation-table addition. If it says "rename X→Y before PostGrid lands," that goes on the backlog.

No code changes in Phase A. Notes files only.

---

## Phase B — Per-piece batch activation primitive

This is the implementation. One service module + persistence wire-up + tests + seed script.

### B1. Service: `app/services/print_mail_activation.py` (new)

The single public surface is one function:

```python
async def activate_pieces_batch(
    *,
    organization_id: UUID,
    pieces: list[PieceSpec],
    test_mode: bool = False,
    correlation_id: str | None = None,
) -> ActivationBatchResult: ...
```

Where:

```python
# ---- Common base ----------------------------------------------------------

class _PieceSpecBase(BaseModel):
    """Fields every piece type carries. Subclasses add the type-specific
    artwork fields plus any type-specific controls."""
    model_config = {"extra": "forbid", "populate_by_name": True}

    # Back-references. Optional today; the step-activation directive will
    # set them. The primitive round-trips them onto direct_mail_pieces.metadata
    # under reserved keys (see §B2).
    recipient_id: UUID | None = None
    channel_campaign_step_id: UUID | None = None
    membership_id: UUID | None = None

    # Recipient. Either an existing adr_id or an inline object matching
    # Lob's address_editable_us / address_editable_intl shape.
    to: str | dict[str, Any]
    # From. Either an adr_id or inline. Required when `to` is international.
    from_: str | dict[str, Any] = Field(alias="from")

    # Common Lob controls.
    use_type: Literal["marketing", "operational"] = "marketing"
    mail_type: Literal["usps_first_class", "usps_standard"] | None = None
    merge_variables: dict[str, Any] | None = None
    send_date: datetime | None = None
    metadata: dict[str, str] | None = None
    description: str | None = None

    # Idempotency. The caller supplies a stable seed (e.g.
    # f"{step_id}:{membership_id}" or f"{run_id}:{spec_index}"). The
    # service hashes it via lob_client.build_idempotency_material to
    # derive the Lob Idempotency-Key.
    idempotency_seed: str


# ---- Per-type specs (discriminated union on `piece_type`) -----------------

class PostcardSpec(_PieceSpecBase):
    piece_type: Literal["postcard"]
    front: str   # html_string | tmpl_id | remote_file_url
    back: str    # html_string | tmpl_id | remote_file_url
    size: Literal["4x6", "6x9", "6x11", "5x7"] = "4x6"


class SelfMailerSpec(_PieceSpecBase):
    piece_type: Literal["self_mailer"]
    inside: str   # html_string | tmpl_id | remote_file_url
    outside: str  # html_string | tmpl_id | remote_file_url
    # Bifolds + the v1-out-of-scope trifold; matches direct_mail_specs catalog.
    size: Literal["6x18_bifold", "11x9_bifold", "12x9_bifold", "17.75x9_trifold"] = "11x9_bifold"


class LetterSpec(_PieceSpecBase):
    piece_type: Literal["letter"]
    file: str    # html_string | tmpl_id | remote_file_url (single artwork, all pages)
    color: bool = False
    double_sided: bool = True
    address_placement: Literal["top_first_page", "insert_blank_page"] = "top_first_page"
    # Letter add-ons (all optional, all pass through to Lob untouched).
    extra_service: Literal["certified", "registered", "certified_return_receipt"] | None = None
    return_envelope: str | None = None        # rtn_* envelope id
    perforated_page: int | None = None        # 1-indexed page with perforation
    cards: list[str] | None = None            # crd_* affixed-card ids
    buckslips: list[str] | None = None        # bck_* buckslip ids


class SnapPackSpec(_PieceSpecBase):
    piece_type: Literal["snap_pack"]
    inside: str   # html_string | tmpl_id | remote_file_url (8.5"x11" inside)
    outside: str  # html_string | tmpl_id | remote_file_url (6"x18" outside)
    size: Literal["8.5x11"] = "8.5x11"
    color: bool = False


class BookletSpec(_PieceSpecBase):
    piece_type: Literal["booklet"]
    file: str    # html_string | tmpl_id | remote_file_url (multipage, sized per `size`)
    size: Literal["8.375x5.375", "8.25x5.5", "8.5x5.5"] = "8.375x5.375"


PieceSpec = Annotated[
    PostcardSpec | SelfMailerSpec | LetterSpec | SnapPackSpec | BookletSpec,
    Field(discriminator="piece_type"),
]


# ---- Results --------------------------------------------------------------

class PieceResult(BaseModel):
    spec_index: int
    piece_type: Literal["postcard", "self_mailer", "letter", "snap_pack", "booklet"]
    status: Literal["created", "skipped_suppressed", "failed"]
    piece_id: UUID | None = None              # hq-x direct_mail_pieces.id
    external_piece_id: str | None = None      # Lob psc_/sfm_/ltr_/ord_/bkl_ id
    error_code: str | None = None
    error_detail: dict[str, Any] | None = None


class ActivationBatchResult(BaseModel):
    correlation_id: str
    total: int
    created: int
    skipped: int
    failed: int
    results: list[PieceResult]
```

Confirm the exact `BookletSpec.size` enum values and any letter `extra_service` values against `app/providers/lob/client.py` and Lob's live docs before locking the literals. If a value in the enum is wrong, fix the enum, not the docs link. The point of the discriminated union is that the executor cannot accidentally pass `front` to a letter or `file` to a self_mailer — type-checking catches the misuse before Lob does.

**Behavior contract:**

1. **Per-piece isolation.** A failure on piece N does not abort the batch. Every `PieceSpec` produces exactly one `PieceResult`. `ActivationBatchResult.failed` may be > 0 with `created` > 0 in the same batch.
2. **Suppression check.** Before calling Lob, hash the piece's recipient address via [app/direct_mail/addresses.py:normalize](../../app/direct_mail/addresses.py) and check `suppressed_addresses` for any row matching `address_hash`. If found, do not call Lob. Emit a `PieceResult` with `status='skipped_suppressed'`, `error_code='suppressed'`, and the matching `reason` in `error_detail`.
3. **Idempotency.** Compute the Lob `Idempotency-Key` via `lob_client.build_idempotency_material(idempotency_seed)`. Pass it on every Lob call. Re-running `activate_pieces_batch` with the same `(organization_id, pieces)` is a safe no-op — Lob returns the previously-created piece, and `upsert_piece` reconciles it idempotently.
4. **Persistence ordering.** After Lob returns 2xx for piece N: call `direct_mail.persistence.upsert_piece` with `provider_slug='lob'`, the returned `external_piece_id`, `piece_type`, `status='queued'` (initial), `cost_cents` derived via `project_cost_cents`, the full Lob response JSON in `raw_payload`, and the spec's back-references (`recipient_id`, `channel_campaign_step_id`, `membership_id`) in `metadata`. The webhook pipeline is what advances status from `queued` onward — this primitive does not mutate status post-create.
5. **Test mode.** If `test_mode=True`, set `is_test_mode=True` on the persisted row AND pass Lob's test API key (the existing `LOB_API_KEY_TEST` env var if present, falling back to `LOB_API_KEY` with the piece flagged test-mode). Confirm the existing one-off routes' test-mode handling pattern in [app/routers/direct_mail.py](../../app/routers/direct_mail.py) and copy it.
6. **Concurrency.** Issue Lob calls with bounded concurrency (e.g. `asyncio.Semaphore(8)`). Lob's documented sustained rate is north of that; the cap is to be a polite citizen and to keep error blast radius small.
7. **Correlation id.** Generate one if not supplied (`uuid4().hex`). Log it on every Lob call for traceability. Round-trip on the result.
8. **No partial-piece state.** If `upsert_piece` fails after a successful Lob create (DB-side error), the result is `status='failed'`, `error_code='persistence_failed'`, with the Lob `external_piece_id` populated in `error_detail.lob_external_piece_id` so an operator can reconcile manually. Do NOT attempt to cancel the Lob piece — that's a recovery decision, not an activation decision.

**Internals to keep tight:**

- One private `_dispatch_one(spec)` that handles a single `PieceSpec` end-to-end. The public function is suppression-pre-check + `asyncio.gather`-with-semaphore over `_dispatch_one`.
- Per-type payload builder. One private function per type — `_build_postcard_payload(spec) -> dict`, `_build_self_mailer_payload(spec) -> dict`, `_build_letter_payload(spec) -> dict`, `_build_snap_pack_payload(spec) -> dict`, `_build_booklet_payload(spec) -> dict` — each returning the exact dict shape Lob's `create_*` expects (with the `from` aliasing handled). The dispatcher `_dispatch_one` selects builder + Lob create function via a small dispatch table keyed on `piece_type`.
- Translate `LobProviderError` → `PieceResult(status='failed', error_code=err.category, error_detail={...})`. Other exceptions → `PieceResult(status='failed', error_code='internal_error', error_detail={...})`.
- The dispatch table is the only place that maps `piece_type` strings to behavior. Adding a sixth type (e.g. checks, when that lands) is a one-line addition there plus a new `_build_*_payload`.

### B2. Persistence helper additions (if needed)

Read [app/direct_mail/persistence.py:upsert_piece](../../app/direct_mail/persistence.py) carefully. If `upsert_piece` does NOT currently accept `recipient_id`, `channel_campaign_step_id`, or `membership_id` as part of the metadata it persists, **do not modify the function signature** — instead, the activation service merges those fields into the `metadata` dict passed to `upsert_piece` under reserved keys: `_recipient_id`, `_channel_campaign_step_id`, `_membership_id`. That keeps the persistence layer untouched and keeps these back-references queryable via JSONB indexing later.

If `direct_mail_pieces` does not already have a JSONB `metadata` column with a GIN index on it, add a migration:

`migrations/<UTC_TIMESTAMP>_direct_mail_pieces_metadata_gin.sql` (new, only if a GIN index does not already exist):

```sql
CREATE INDEX IF NOT EXISTS idx_direct_mail_pieces_metadata_gin
    ON direct_mail_pieces USING GIN (metadata);
```

Verify the column exists first via `\d direct_mail_pieces` (or read the migration). Skip the migration if the index already exists.

### B3. Tests

`tests/test_print_mail_activation.py` (new). Tests must exercise all five piece types — none of the type-specific code paths are skippable:

**Per-type dispatch (one test per type, five total):**
1. `test_batch_dispatches_postcard` — submit a `PostcardSpec`, mock `lob_client.create_postcard`, assert it is called with `{to, from, front, back, size, use_type, ...}` and the response's `psc_*` id round-trips into `direct_mail_pieces.external_piece_id` with `piece_type='postcard'`.
2. `test_batch_dispatches_self_mailer` — submit a `SelfMailerSpec`, mock `lob_client.create_self_mailer`, assert payload has `inside`+`outside` (NOT `front`+`back`) and `size` is one of the bifold/trifold values; assert `sfm_*` id round-trips with `piece_type='self_mailer'`.
3. `test_batch_dispatches_letter` — submit a `LetterSpec` with `color=True`, `double_sided=False`, `address_placement='insert_blank_page'`, and an `extra_service='certified'`; mock `lob_client.create_letter`; assert payload has `file` (single artwork field, NOT `front`+`back`) plus the four letter-specific controls; assert `ltr_*` id round-trips with `piece_type='letter'`.
4. `test_batch_dispatches_snap_pack` — submit a `SnapPackSpec`, mock `lob_client.create_snap_pack`, assert payload has `inside`+`outside` and `size='8.5x11'`; assert `ord_*` id round-trips with `piece_type='snap_pack'` (note Lob's snap_pack id prefix is `ord_`, not `snp_` — this is a real footgun the test catches).
5. `test_batch_dispatches_booklet` — submit a `BookletSpec`, mock `lob_client.create_booklet`, assert payload has `file` (single multipage artwork, NOT `front`+`back`); assert `bkl_*` id round-trips with `piece_type='booklet'`.

**Per-type payload negative tests (catch shape misuse early):**
6. `test_postcard_spec_rejects_inside_outside_fields` — Pydantic should refuse a `PostcardSpec` constructed with `inside=...`/`outside=...` (extra=forbid). Same negative test for letter rejecting `front`/`back`, self_mailer rejecting `file`, etc. One parametrized test covering the cross-product is fine.
7. `test_size_enum_per_type_is_enforced` — `PostcardSpec(size='11x9_bifold')` raises; `SelfMailerSpec(size='4x6')` raises; `BookletSpec(size='4x6')` raises. Each piece type's `size` enum is closed.

**Cross-type behavior tests:**
8. `test_batch_mixes_all_five_types_in_one_call` — one batch containing one of each type (5 specs total). Assert the right `lob_client.create_*` is called for each, all five rows land in `direct_mail_pieces` with correct `piece_type`, the `ActivationBatchResult.created == 5`.
9. `test_batch_continues_on_single_failure` — five-piece mixed batch, the letter's mock raises `LobProviderError`. Result: `created=4, failed=1`, no exception bubbled, the four successful pieces are persisted.
10. `test_batch_skips_suppressed_address` — pre-seed `suppressed_addresses` with one piece's address hash. That piece (regardless of type) returns `status='skipped_suppressed'`, no Lob call made for it. Other pieces dispatch normally. Run this with the suppressed piece being a snap_pack to confirm suppression is type-agnostic.
11. `test_batch_idempotency_key_derived_from_seed` — assert the same `idempotency_seed` produces the same `Idempotency-Key` across two runs (use `lob_client.build_idempotency_material` directly to confirm). Verify across all five types (parametrized).
12. `test_batch_persists_back_references_in_metadata` — assert `direct_mail_pieces.metadata->>'_recipient_id'`, `_channel_campaign_step_id`, `_membership_id` round-trip from spec to row, for each of the five types (parametrized).
13. `test_batch_persistence_failure_after_lob_success` — mock `upsert_piece` to raise. Result: `status='failed'`, `error_code='persistence_failed'`, `error_detail.lob_external_piece_id` populated. Run for one type (postcard) — the persistence path is shared.
14. `test_batch_concurrency_bound` — submit 32 pieces (mixed types), assert at most 8 in-flight at any moment (instrument the `_dispatch_one` mock with a counter).
15. `test_batch_correlation_id_round_trips` — supplied id surfaces on result; absent id is auto-generated and stable across the batch.

Use `pytest-asyncio` and the existing test fixtures for DB + mocked HTTP. Parametrize wherever the test body is type-agnostic to keep the file readable.

Use `pytest-asyncio` and the existing test fixtures for DB + mocked HTTP.

### B4. Seed/exercise script

**File:** `scripts/seed_print_mail_batch_demo.py` (new)

End-to-end smoke test against Lob test mode. Reads from Doppler at runtime. **Exercises all five piece types in one batch** — this is the canonical smoke-test for the primitive.

1. Connect to hq-x DB. Look up or create a test organization (`slug='print-mail-demo'`).
2. Build 5 `PieceSpec`s — one of each type (`PostcardSpec`, `SelfMailerSpec`, `LetterSpec`, `SnapPackSpec`, `BookletSpec`) — each with bespoke per-piece artwork content (five distinct short HTML strings, one per piece, to demonstrate the per-piece variation contract). Each spec carries a different fake `recipient_id` / `membership_id` UUID so the back-reference round-trip is observable in the result. All five target the same fictitious recipient address (use a Lob test address from `api-reference-docs-new` — the goal is to exercise the API surface, not deliver real mail). For the letter spec, set `color=True` + `address_placement='top_first_page'` to exercise letter-specific fields; for snap_pack set `color=False`; for booklet use the default `8.375x5.375` size.
3. Call `activate_pieces_batch(organization_id=org_id, pieces=specs, test_mode=True)`.
4. Print, in this order:
   - The `ActivationBatchResult` summary (`total`, `created`, `skipped`, `failed`, `correlation_id`).
   - Per result, in the order submitted: `(spec_index, piece_type, status, external_piece_id, error_code or '-')`. All five must show `status='created'` for a green run.
   - SELECT the matching rows from `direct_mail_pieces` keyed on the five `external_piece_id`s and pretty-print `(piece_type, provider_slug, external_piece_id, status, metadata)`. Assert each row's `metadata->>'_recipient_id'` matches what was submitted.
5. Exit 0 only if `result.created == 5 and result.failed == 0 and result.skipped == 0`. Non-zero with descriptive message otherwise (which type(s) failed, and the `error_code` / `error_detail` for each).

Run via:

```bash
doppler --project hq-x --config dev run -- uv run python -m scripts.seed_print_mail_batch_demo
```

The seed script is the smoke gate — if it doesn't go green end-to-end against Lob test mode for all five types, the directive is not done.

### B5. Documentation update

Append to [CLAUDE.md](../../CLAUDE.md) under a new section "Per-piece direct-mail activation":

- One-paragraph description: this primitive is the substrate owned-brand initiatives use to mint per-recipient bespoke direct mail. It bypasses Lob's Campaigns API entirely and goes through Print & Mail per piece. Provider abstraction is intentionally absent today; PostGrid is documented in `docs/research/postgrid-print-mail-api-notes.md` for when it lands.
- The Doppler command for the seed script.
- A pointer to `docs/research/canonical-piece-event-taxonomy.md` as the read-side contract.

---

## What NOT to do

- Do **not** modify [app/services/dmaas_campaign_activation.py](../../app/services/dmaas_campaign_activation.py), [app/services/lob_audience_csv.py](../../app/services/lob_audience_csv.py), or anything under [app/dmaas/](../../app/dmaas/). The Campaigns API path stays as-is — DMaaS for non-owned-brand orgs continues to use it.
- Do **not** introduce a `DirectMailProvider` ABC, `Protocol`, or any abstract base class for providers. PostGrid notes are research, not stubs.
- Do **not** add a PostGrid client, even an empty skeleton. The `app/providers/postgrid/` directory must not exist after this directive.
- Do **not** wire `activate_pieces_batch` into [app/services/step_scheduler.py](../../app/services/step_scheduler.py) or any other activation pipeline. Step-activation rewrite is a sibling directive.
- Do **not** modify the existing one-off create routes in [app/routers/direct_mail.py](../../app/routers/direct_mail.py).
- Do **not** add a public router for the batch primitive. It is an internal service surface in this directive. A public surface comes when step activation lands.
- Do **not** modify [app/webhooks/lob_normalization.py](../../app/webhooks/lob_normalization.py) or [app/webhooks/lob_processor.py](../../app/webhooks/lob_processor.py). The audit in A2 is read-only — proposed renames are documented, not applied.
- Do **not** touch [app/direct_mail/persistence.py](../../app/direct_mail/persistence.py) function signatures. If you need to thread back-references onto rows, do it via the `metadata` JSONB.
- Do **not** introduce a new migration that alters `direct_mail_pieces` columns. The only allowed migration in scope is the optional GIN index in §B2.
- Do **not** retry failed pieces inside `activate_pieces_batch`. The per-call retry config in `lob_client._request_with_retry` already handles transient HTTP errors; per-batch retry is a downstream orchestration concern.
- Do **not** call Lob's `/uploads`, `/campaigns`, or `/creatives` endpoints from anywhere in the new code. This path is `/postcards`, `/letters`, `/self_mailers` only.
- Do **not** persist creative content (HTML strings or PDF URLs) into `direct_mail_pieces`. Lob holds the rendered artifact; we hold the pointer (`external_piece_id`) and a `raw_payload` snapshot of Lob's create response. If a thumbnail URL appears in Lob's response, it is part of `raw_payload` and that's enough — no separate column.
- Do **not** read or write the user's clipboard, browser, or any tool outside this repo plus the api-reference-docs-new tree.

---

## Scope

Files to create or modify:

- `docs/research/postgrid-print-mail-api-notes.md` (new)
- `docs/research/canonical-piece-event-taxonomy.md` (new)
- `app/services/print_mail_activation.py` (new)
- `migrations/<UTC_TIMESTAMP>_direct_mail_pieces_metadata_gin.sql` (new, conditional — only if GIN index does not already exist)
- `tests/test_print_mail_activation.py` (new)
- `scripts/seed_print_mail_batch_demo.py` (new)
- `CLAUDE.md` (modify — append "Per-piece direct-mail activation" section with the Doppler command and pointers to the two notes files)

**One commit. Do not push.**

Commit message:

> feat(direct-mail): per-piece batch activation primitive (Lob Print & Mail, all 5 types)
>
> Add `app/services/print_mail_activation.py` exposing `activate_pieces_batch`
> — the substrate for owned-brand initiatives where each recipient receives
> bespoke per-piece HTML/PDF via Lob's Print & Mail API. Covers all five
> piece types: postcard (front+back), self_mailer (inside+outside, bifold/
> trifold sizes), letter (single file + color/double_sided/address_placement/
> extra_service add-ons), snap_pack (inside+outside, 8.5x11), booklet
> (single multipage file).
>
> Discriminated-union `PieceSpec` enforces per-type artwork-field shape at
> construction time. Per-piece isolation, idempotency via the existing
> `lob_client.build_idempotency_material`, suppression-list pre-check,
> persistence via `direct_mail.persistence.upsert_piece` (provider_slug='lob').
>
> Includes PostGrid Print & Mail API research notes (all five types) and
> a canonical-event taxonomy audit documenting the provider-neutral `piece.*`
> vocabulary in webhooks/lob_normalization.py — research output for when a
> second direct-mail provider lands. No provider abstraction layer introduced.
> Step-activation wiring is a follow-up directive.

---

## When done

Report back with:

(a) The path to `docs/research/postgrid-print-mail-api-notes.md` and a 3-bullet summary: PostGrid's per-piece-create endpoint shape, its idempotency model, and your one-line conclusion on whether Lob and PostGrid are structurally congruent enough that the same `PieceSpec` shape can drive both with adapter-level translation only.

(b) The path to `docs/research/canonical-piece-event-taxonomy.md` and the conclusion sentence from §5 of that file (canonical vocabulary sufficient as-is for PostGrid, or specific renames needed before PostGrid lands).

(c) `uv run pytest tests/test_print_mail_activation.py -v` — pass count + total time.

(d) Output of running the seed script end-to-end against Lob test mode: the `ActivationBatchResult` summary, all **five** `external_piece_id`s minted (one of each type, with the type prefix visible — `psc_`, `sfm_`, `ltr_`, `ord_`, `bkl_`), the matching `(piece_type, provider_slug, external_piece_id, status, metadata)` rows from `direct_mail_pieces`, and total runtime.

(e) Confirmation that the GIN index on `direct_mail_pieces.metadata` either (i) already existed before this directive (cite migration + index name) or (ii) was added by the new migration (cite the new migration filename).

(f) The single commit SHA. Do not push.
