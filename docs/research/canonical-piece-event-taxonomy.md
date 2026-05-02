# Canonical piece-event taxonomy — provider-neutral contract audit

Source of truth: [`app/webhooks/lob_normalization.py`](../../app/webhooks/lob_normalization.py)
(`_EVENT_TYPE_MAPPING`, `_PIECE_STATUS_MAPPING`, `SUPPRESSION_TRIGGERS`).
Read-side consumer: [`app/webhooks/lob_processor.py`](../../app/webhooks/lob_processor.py),
plus every analytics rollup that joins on `direct_mail_piece_events.event_type`.

This document treats the `piece.*` event vocabulary as the **provider-neutral
read contract** — downstream code does not know or care that Lob is the
producer today. The audit answers: would this contract survive a PostGrid
drop-in?

---

## 1. Canonical event list

Every value in `_EVENT_TYPE_MAPPING` is prefixed `piece.*`. One-line
semantics:

### Core lifecycle (all piece types)

| canonical event              | semantics                                                       |
|------------------------------|-----------------------------------------------------------------|
| `piece.created`              | provider accepted the piece; render not yet started             |
| `piece.rejected`             | provider rejected the piece pre-render (validation, etc.)       |
| `piece.rendered_pdf`         | rendered PDF artifact is available                              |
| `piece.rendered_thumbnails`  | rendered thumbnails are available                               |
| `piece.canceled`             | piece cancelled before mailing (caller-initiated or auto)       |
| `piece.mailed`               | piece handed off to USPS / carrier                              |
| `piece.in_transit`           | piece scanned in transit                                        |
| `piece.in_local_area`        | piece reached the destination's local sorting facility          |
| `piece.processed_for_delivery` | piece processed for last-mile delivery                        |
| `piece.delivered`            | piece marked delivered                                          |
| `piece.failed`               | piece failed in the carrier network (lost, damaged, etc.)       |
| `piece.re_routed`            | piece re-routed mid-transit (e.g. forwarding address)           |
| `piece.returned`             | piece returned to sender (RTS)                                  |
| `piece.international_exit`   | piece exited the US destined for an international address       |
| `piece.viewed`               | tracking-pixel / link-shortener "first hit" engagement signal   |

### Informed Delivery (USPS recipient-email engagement)

| canonical event                                | semantics                                |
|------------------------------------------------|------------------------------------------|
| `piece.informed_delivery.email_sent`           | USPS sent the daily-mail-preview email   |
| `piece.informed_delivery.email_opened`         | recipient opened the preview email       |
| `piece.informed_delivery.email_clicked_through` | recipient clicked through               |

### Certified Mail (letters only)

| canonical event                          | semantics                                         |
|------------------------------------------|---------------------------------------------------|
| `piece.certified.mailed`                 | certified piece handed off                        |
| `piece.certified.in_transit`             | scan in transit                                   |
| `piece.certified.in_local_area`          | reached local sort                                |
| `piece.certified.processed_for_delivery` | processed for last-mile                           |
| `piece.certified.re_routed`              | re-routed                                         |
| `piece.certified.returned`               | returned to sender                                |
| `piece.certified.delivered`              | delivered (signature captured)                    |
| `piece.certified.pickup_available`       | recipient must pick up at post office             |
| `piece.certified.issue`                  | delivery issue (failed-attempt, refused, etc.)    |

### Return envelope (letter add-on)

| canonical event                                | semantics                                    |
|------------------------------------------------|----------------------------------------------|
| `piece.return_envelope.created`                | return envelope artifact created             |
| `piece.return_envelope.in_transit`             | recipient mailed it back; in transit         |
| `piece.return_envelope.in_local_area`          | reached the original sender's local sort     |
| `piece.return_envelope.processed_for_delivery` | processed back to sender                     |
| `piece.return_envelope.re_routed`              | re-routed back                               |
| `piece.return_envelope.returned`               | returned-to-sender on the return envelope    |

### Catch-all

- `piece.unknown` — Lob emitted an event suffix not in `_EVENT_TYPE_MAPPING`.
  Logged + counted; no status transition.

---

## 2. Canonical piece-status enum

The set of values `direct_mail_pieces.status` takes after webhook
projection (`_PIECE_STATUS_MAPPING` values, plus the initial `queued`
written at create time):

| status              | populating events                                                                 |
|---------------------|----------------------------------------------------------------------------------|
| `queued`            | initial; `piece.created`                                                          |
| `rejected`          | `piece.rejected`                                                                  |
| `rendered`          | `piece.rendered_pdf`, `piece.rendered_thumbnails`                                  |
| `canceled`          | `piece.canceled`                                                                  |
| `in_transit`        | `piece.mailed`, `piece.in_transit`, `piece.in_local_area`, `piece.processed_for_delivery`, `piece.re_routed`, `piece.international_exit`, plus the `piece.certified.*` transit set |
| `delivered`         | `piece.delivered`, `piece.certified.delivered`                                    |
| `failed`            | `piece.failed`                                                                    |
| `returned`          | `piece.returned`, `piece.certified.returned`                                      |
| `pickup_available`  | `piece.certified.pickup_available`                                                |
| `issue`             | `piece.certified.issue`                                                           |

All status values are populated by at least one event mapping.
**No gaps.** Engagement-only events (`piece.viewed`, `piece.informed_delivery.*`,
`piece.return_envelope.*`) intentionally do not transition status —
`_PIECE_STATUS_MAPPING.get(event)` returns `None` and the processor skips
the status update while still appending to the event log.

The migration's `direct_mail_pieces.status` column has no CHECK constraint
on the value set ([`migrations/0011_direct_mail_lob.sql:26`](../../migrations/0011_direct_mail_lob.sql)
declares `VARCHAR(40) NOT NULL DEFAULT 'unknown'`). The status enum is
enforced in code, not in the schema. That tolerance is fine for adding
new status values when a second provider lands.

---

## 3. Lob-specific leakage scan

Walk through every key in `_EVENT_TYPE_MAPPING` and ask: does the name
describe a Lob-specific concept, or a general direct-mail concept?

| canonical event name              | Lob-leaky? | reasoning                                                                                                   |
|-----------------------------------|------------|-------------------------------------------------------------------------------------------------------------|
| `piece.created`                   | no         | every provider has a "create" lifecycle event                                                                |
| `piece.rejected`                  | no         | ditto                                                                                                       |
| `piece.rendered_pdf`              | **soft**   | "rendered PDF" is provider-side artifact terminology; the concept generalizes (rename candidate: `piece.rendered`) |
| `piece.rendered_thumbnails`       | **soft**   | "thumbnails" is a Lob artifact name; PostGrid does not emit thumbnail events. Rename candidate: same as above (collapse both into `piece.rendered`) |
| `piece.canceled`                  | no         | universal                                                                                                   |
| `piece.mailed`                    | no         | universal — "handed to USPS"                                                                                 |
| `piece.in_transit`                | no         | universal                                                                                                   |
| `piece.in_local_area`             | no         | universal — both Lob and PostGrid surface this                                                               |
| `piece.processed_for_delivery`    | no         | universal                                                                                                   |
| `piece.delivered`                 | no         | universal                                                                                                   |
| `piece.failed`                    | no         | universal                                                                                                   |
| `piece.re_routed`                 | no         | universal                                                                                                   |
| `piece.returned`                  | no         | universal — RTS                                                                                              |
| `piece.international_exit`        | no         | USPS concept, not Lob-specific; any provider routing internationally surfaces this                          |
| `piece.viewed`                    | **soft**   | "viewed" is generic; the underlying mechanic (Lob tracking pixel / shortlink) is provider-specific. Rename candidate: keep `piece.viewed` but document that the trigger source varies per provider |
| `piece.informed_delivery.email_sent` | **no** (USPS feature) | "Informed Delivery" is a USPS service Lob surfaces. PostGrid does not surface it today, but that's a *coverage* gap, not a *naming* gap. The canonical name is fine — when a provider doesn't have IMb→email coverage, no events fire on this name |
| `piece.informed_delivery.email_opened` | no    | same                                                                                                        |
| `piece.informed_delivery.email_clicked_through` | no | same                                                                                                  |
| `piece.certified.*` (9 events)    | no         | "Certified Mail" is a USPS service. The whole `certified.*` family is USPS-canonical. Provider-neutral.    |
| `piece.return_envelope.*` (6 events) | **soft** | The concept (a return-envelope's lifecycle) is general; Lob's `rtn_*` artifact is a Lob-specific implementation. PostGrid bundles return-envelope as a flag, not a separately-trackable artifact, so PostGrid will never emit on these names. The canonical names are fine; coverage will just be uneven |

### Events safe as-is (no rename needed for PostGrid)

All except the soft cases above. That includes the entire core lifecycle,
the entire `certified.*` family, and the `informed_delivery.*` family.

### Events that should be renamed when a second provider lands

Two candidates only:

1. **`piece.rendered_pdf` + `piece.rendered_thumbnails` → collapse to
   `piece.rendered`** (one canonical, with `raw_payload.artifact_kind`
   differentiating "pdf" vs "thumbnails" for Lob and "pdf" vs whatever
   PostGrid emits). This is a clean rename — the `_PIECE_STATUS_MAPPING`
   already collapses both to status `rendered`, so callers reading status
   are unaffected. Only callers reading `event_type` directly need to
   update.

2. **`piece.return_envelope.*` family** — could rename to `piece.bre.*`
   (Business Reply Envelope) which is the USPS-native term, with the
   understanding that PostGrid will not emit on these names. This is
   cosmetic and not load-bearing — leave as-is unless a future provider
   forces a rename.

Everything else: ship as-is. The vocabulary is provider-neutral enough
that PostGrid can drop in via a translation table without renames.

---

## 4. PostGrid coverage matrix

Cross-reference of PostGrid's webhook event emissions (per
[postgrid-print-mail-api-notes.md §5](postgrid-print-mail-api-notes.md))
against the canonical hq-x vocabulary.

### Clean matches (no mapping logic, just rename suffix)

| PostGrid event                  | hq-x canonical                  |
|---------------------------------|---------------------------------|
| `letter.created`                | `piece.created`                 |
| `letter.in_transit`             | `piece.in_transit`              |
| `letter.in_local_area`          | `piece.in_local_area`           |
| `letter.processed_for_delivery` | `piece.processed_for_delivery`  |
| `letter.delivered`              | `piece.delivered`               |
| `letter.failed`                 | `piece.failed`                  |
| `letter.re_routed`              | `piece.re_routed`               |
| `letter.returned_to_sender`     | `piece.returned`                |
| `postcard.*` / `selfmailer.*`   | (same suffix set as letter)     |

### Close matches (canonical fits with a documented mapping)

| PostGrid event       | hq-x canonical            | mapping note |
|----------------------|---------------------------|--------------|
| `letter.cancelled`   | `piece.canceled`          | spelling difference (PostGrid: en-GB; hq-x: en-US, matching Lob) |
| `letter.ready`       | `piece.rendered_pdf`      | PostGrid's "ready" = printable PDF rendered. After the §3 rename to `piece.rendered`, this becomes a clean match |

### No match (PostGrid emits something hq-x has no canonical name for)

| PostGrid event       | proposed canonical       | gap action                                                |
|----------------------|--------------------------|-----------------------------------------------------------|
| `letter.printing`    | `piece.printing` (new)   | More granular than Lob's `created` → `mailed` jump. Add as a new optional canonical when integrating PostGrid; will not retroactively appear on Lob pieces |

### No PostGrid coverage of hq-x canonicals

These canonicals will simply have zero PostGrid-sourced events; analytics
that depend on them must scope to `provider_slug='lob'`:
- `piece.viewed`
- `piece.informed_delivery.*` (3)
- `piece.certified.*` (9)
- `piece.return_envelope.*` (6)
- `piece.international_exit`
- `piece.rendered_thumbnails` (subsumed under rename)

---

## 5. Conclusion

**The canonical `piece.*` vocabulary is sufficient for a PostGrid drop-in
as-is, with one optional refactor: collapse `piece.rendered_pdf` +
`piece.rendered_thumbnails` into a single `piece.rendered` event.**

That refactor is *cosmetic* (the existing status mapping already collapses
both to `status='rendered'`) and is **not** required before PostGrid lands
— callers reading the canonical event log can switch on either name. The
rename is a 5-minute change when convenient.

Beyond that one optional rename, PostGrid integration is purely a
translation table addition (PostGrid's resource-prefixed event name →
canonical `piece.*`), plus one new canonical `piece.printing` for
PostGrid-only granularity.

No structural changes to the vocabulary are needed. Ship the activation
primitive against the current canonical contract; revisit the rename
when a second provider is actually being integrated.
