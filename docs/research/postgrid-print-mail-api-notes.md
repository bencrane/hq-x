# PostGrid Print & Mail API — research notes

Source: postgrid.com/docs (PostGrid Print & Mail public docs, 2026-04-30 read).
Purpose: backup-provider reference for the per-piece direct-mail activation
primitive (`app/services/print_mail_activation.py`). Lob is primary; PostGrid
is documented here so a future provider port has a single entry point.
This file is a reference. It is **not** a design doc, and no code in
`app/providers/postgrid/` exists — by design.

---

## 1. Per-piece create endpoints — all five Lob types

All endpoints are under `https://api.postgrid.com/print-mail/v1`. Auth header
is `x-api-key: <key>` (see §4). Bodies are `application/x-www-form-urlencoded`
or `multipart/form-data` for file uploads; PostGrid also accepts JSON on
most endpoints. Per-call idempotency uses `Idempotency-Key` header (§8).

### 1.1 Postcard
- **Path**: `POST /postcards`
- **Artwork fields**: `frontHTML` + `backHTML` (PostGrid's analog of Lob's
  `front` + `back`) — accept full HTML strings. Alternatives:
  - `frontTemplate` / `backTemplate` — PostGrid template id (`template_*`)
  - `frontPDF` / `backPDF` — multipart file upload, OR `frontPDFURL` /
    `backPDFURL` for a remote URL
- **Merge variables**: `mergeVariables` (object). Same `{{var}}` substitution
  semantics as Lob; both keys-and-values stringified.
- **Sizes**: `size` accepts `"6x4"`, `"9x6"`, `"11x6"` (note PostGrid's
  width-x-height ordering is the inverse of Lob's `4x6` / `6x9` / `6x11` —
  same physical sizes, different string).
- **Other first-class fields**: `to`, `from` (each: contact id `contact_*`,
  or inline object), `mailingClass` ("first_class" | "standard_class"),
  `sendDate`, `metadata`, `description`.
- **Response**: piece id `postcard_*`.

### 1.2 Letter
- **Path**: `POST /letters`
- **Artwork field**: `pdf` (single file upload) or `pdfURL` (remote) or
  `template` (template id) or `html` (full HTML string). One artwork field
  covers the whole letter — same as Lob's `file`.
- **Letter-specific controls**:
  - `color: bool` — color vs B/W printing
  - `doubleSided: bool`
  - `addressPlacement: "top_first_page" | "insert_blank_page"` — same enum
    as Lob
  - `extraService: "certified" | "registered" | "certified_return_receipt"`
    — same enum as Lob
  - `returnEnvelope: bool` (PostGrid auto-includes a #9 BRE) — Lob requires
    a separate `rtn_*` envelope id, so the surfaces are not 1:1
  - `perforatedPage: int` — 1-indexed page with tear-off
  - `cards`, `buckslips` arrays — IDs of pre-uploaded affixed cards /
    buckslips. PostGrid calls them `card` / `buckslip` resources (no
    explicit `crd_` / `bck_` prefix shown in docs).
- **Response**: piece id `letter_*`.

### 1.3 Self-mailer
- **Path**: `POST /selfmailers`
- **Artwork fields**: `insidePDF` + `outsidePDF` (or `insideHTML` /
  `outsideHTML`, or `insideTemplate` / `outsideTemplate`, or `insidePDFURL`
  / `outsidePDFURL`). Field shape: same as Lob's `inside` / `outside`,
  different field-name suffix per source-mode.
- **Sizes**: PostGrid documents `"6x18"`, `"9x12"`, `"9x6"` — overlap
  with Lob's `6x18_bifold` / `12x9_bifold` / `11x9_bifold` is partial.
  Ordering convention differs (PostGrid uses width-x-height again).
  PostGrid does not appear to expose the trifold variant Lob calls
  `17.75x9_trifold`.
- **Other fields**: `to`, `from`, `mailingClass`, `sendDate`, `metadata`,
  `description`, `mergeVariables`.
- **Response**: piece id `selfmailer_*`.

### 1.4 Snap pack — **PostGrid does not support snap packs.**
Lob's `/v1/snap_packs` endpoint has no PostGrid analog as of 2026-04-30.
A PostGrid port of the activation primitive would need to surface this
gap explicitly: either reject `SnapPackSpec` when provider=postgrid, or
fall back to a letter-with-tearoff approximation (semantically different;
not a drop-in replacement).

### 1.5 Booklet — **PostGrid does not support booklets.**
Similarly no `/booklets` analog. Same gap-surfacing decision needed in a
future port.

### Summary of per-create field shape
| piece type   | Lob fields                | PostGrid fields                          | gap?                        |
|--------------|---------------------------|------------------------------------------|-----------------------------|
| postcard     | `front`, `back`           | `frontHTML`/`frontPDF`/`frontTemplate` + `back*` | none (parallel multi-source) |
| letter       | `file`                    | `pdf` / `pdfURL` / `template` / `html`   | none (parallel multi-source) |
| self_mailer  | `inside`, `outside`       | `insidePDF`/etc + `outsidePDF`/etc       | partial size overlap         |
| snap_pack    | `inside`, `outside`       | —                                        | **unsupported**              |
| booklet      | `file`                    | —                                        | **unsupported**              |

---

## 2. Per-piece read / cancel / list

Same shape across types. Letter examples below; substitute the resource
prefix (`postcards`, `letters`, `selfmailers`) for other types.

- `GET  /letters/{id}` — read
- `GET  /letters` — list, supports `limit`, `skip`, `search`, date filters
- `DELETE /letters/{id}` — cancel (only valid before `processed_for_delivery`;
  PostGrid documents the same cancel-window semantics as Lob's pre-render
  rule)

---

## 3. ID prefix conventions

| resource     | Lob prefix | PostGrid prefix         |
|--------------|------------|-------------------------|
| postcard     | `psc_`     | `postcard_`             |
| letter       | `ltr_`     | `letter_`               |
| self_mailer  | `sfm_`     | `selfmailer_`           |
| snap_pack    | `ord_`     | n/a                     |
| booklet      | `bkl_`     | n/a                     |
| contact      | `adr_`     | `contact_`              |
| template     | `tmpl_`    | `template_`             |
| webhook      | `whk_`     | `webhook_`              |

PostGrid uses verbose prefixes (full resource name + underscore + nano-id)
rather than Lob's three-letter convention. Ports must unpack both shapes.

---

## 4. Auth header

PostGrid: `x-api-key: live_<...>` (test keys: `test_<...>`). HTTP Basic
auth (Lob's pattern: `key:`) is **not** supported.

Implication for adapter design: the per-call signature in
`app/providers/lob/client.py` (`api_key: str` → HTTP Basic) does not
translate cleanly. A PostGrid client would inject `x-api-key` instead.

---

## 5. Webhook event taxonomy

PostGrid emits webhooks for piece lifecycle changes. Cross-referenced
against the canonical hq-x `piece.*` vocabulary in
[`app/webhooks/lob_normalization.py`](../../app/webhooks/lob_normalization.py)
(see [canonical-piece-event-taxonomy.md](canonical-piece-event-taxonomy.md)
for the full audit).

| PostGrid event                  | nearest hq-x canonical      | match quality |
|---------------------------------|-----------------------------|---------------|
| `letter.created`                | `piece.created`             | clean         |
| `letter.cancelled`              | `piece.canceled`            | clean (spelling) |
| `letter.in_transit`             | `piece.in_transit`          | clean         |
| `letter.processed_for_delivery` | `piece.processed_for_delivery` | clean      |
| `letter.delivered`              | `piece.delivered`           | clean         |
| `letter.returned_to_sender`     | `piece.returned`            | clean         |
| `letter.failed`                 | `piece.failed`              | clean         |
| `letter.ready`                  | `piece.rendered_pdf` (approx) | close (PostGrid's "ready" = printable PDF rendered) |
| `letter.printing`               | — (no analog)               | **gap**       |
| `letter.in_local_area`          | `piece.in_local_area`       | clean         |
| `letter.re_routed`              | `piece.re_routed`           | clean         |
| `postcard.*`                    | (same suffix set as letter) | clean         |
| `selfmailer.*`                  | (same suffix set as letter) | clean         |

**No analog for**: `informed_delivery.*` family (USPS Informed Delivery is
Lob-specific surfacing today — PostGrid does not appear to forward IMb
recipient-email events), `certified.*` family (PostGrid surfaces certified
status via `extraService` echoes on the piece object, not via dedicated
events), `return_envelope.*` family (PostGrid's BRE is a flag, not a
separate trackable artifact).

PostGrid-only events not on Lob: `letter.printing` (more granular than
Lob's `created` → `mailed` jump). Could surface as a new canonical
`piece.printing` if useful, but probably collapsible into `piece.rendered_*`.

---

## 6. Rate limits

- Documented sustained rate: **60 requests/second** per API key (Lob's
  documented sustained rate is roughly an order of magnitude higher; both
  comfortably above the directive's `asyncio.Semaphore(8)` cap).
- **No batch create endpoint** — every piece is one HTTP call (same as
  Lob's Print & Mail surface). PostGrid offers a CSV-bulk path for
  postcards-with-shared-creative, which is the analog of Lob's Campaigns
  API and is **out of scope** for the per-recipient bespoke creative
  pattern this directive targets.

---

## 7. Address verification

PostGrid offers inline address verification on piece create (`addressVerify:
true` body flag — when set, PostGrid runs USPS CASS verification before
accepting the piece and rejects undeliverable addresses with a
`411_undeliverable` body code). Also exposes a separate `POST /addresses/verify`
endpoint when callers want verification ahead of send time.

Lob splits the same surface into `/v1/us_verifications` (single) and
`/v1/bulk/us_verifications` (bulk) and does **not** offer inline-on-create.
The hq-x activation service runs verify ahead of the piece create
(`app/direct_mail/addresses.py:verify_or_suppress`), which is congruent
with both providers — a PostGrid port can either keep that pre-check or
delegate to PostGrid's inline flag.

---

## 8. Idempotency

PostGrid honors an `Idempotency-Key` request header on POST endpoints.
Same semantics as Lob: same-key + same-body = previously-created resource
returned; same-key + different-body = 409 conflict.

The hq-x activation service's `idempotency_seed` → sha256 → header-key
derivation is **provider-neutral** and works against both providers
unchanged.

---

## 9. Cost surfacing on create

Lob echoes per-piece cost on the create response under `price` (string,
US dollars, e.g. `"0.84"`). The hq-x persistence layer projects this to
integer cents via `direct_mail/persistence.py:project_cost_cents`.

PostGrid's create response includes `metadata.estimatedCharge` (object
with `currency` and `amount` in cents). Different shape, same semantic
content. `project_cost_cents` would need a per-provider variant, or a
small normalization shim in the PostGrid adapter.

---

## Provider gaps relative to Lob

Things PostGrid lacks that Lob has:
- Snap packs (no analog endpoint)
- Booklets (no analog endpoint)
- Informed Delivery webhook events (`piece.informed_delivery.*`)
- Return-envelope as a first-class trackable artifact (PostGrid treats
  return envelope as a flag, not a tracked piece)
- The `tmpl_*` template versioning surface PostGrid offers is shallower
  than Lob's `templates` + template-versions surface

Things PostGrid has that Lob doesn't:
- Inline address-verification flag on piece create (`addressVerify: true`)
- More granular print-stage event (`letter.printing`) between create and
  mailed
- CSV-bulk postcard path (analog of Lob's Campaigns API; not relevant to
  the per-recipient per-piece directive)

Bottom line: the per-recipient bespoke creative pattern (postcard,
letter, self_mailer) translates cleanly between providers; the snap_pack
and booklet types are Lob-only. A PostGrid port of the activation
primitive must either reject those two `PieceSpec` variants or implement
provider-specific fallbacks.
