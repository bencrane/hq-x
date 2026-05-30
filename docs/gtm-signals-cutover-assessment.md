# GTM Signals cutover (PR-4 + dual-run): adversarial assessment

Assessed against live code as of `main` (HEAD), 2026-05-29. Read-only review.
Plan under attack: `gtm-signals-cutover-plan.md`. Companion:
`gtm-signals-to-hqx-plan-of-attack.md`.

## VERDICT: SHIP-WITH-FIXES

The boundary (definition→hq-x, compute→DEX) is sound and PRs 1–3 are on disk and
correct in isolation. But PR-4 as specified will **not** reach byte-for-byte
parity with the live Modal dispatcher, and the **dual-run sequence in the plan is
written for a world that no longer exists** — both live prod signals are already
`webhook_target='test'`, so the plan's "flip Modal to a test sink" step is a
no-op and the actual double-fire/zero-fire risk is mislocated. Six BLOCKER-class
defects below must be fixed before PR-4 ships or the cutover executes. None are
architectural; all are concrete and localized.

Ground truth pulled from the live system (not the plan):

- `GET https://api.dataengine.run/api/v1/gtm/signals` (prod, via `DEX_SERVICE_TOKEN`)
  returns **2** signals, both `is_active=true`, both **`webhook_target='test'`**:
  - `usaspending_net_new_100k` — `time_window_hours=8760`, `min_obligated_usd=100000`,
    4 award types, `action_types=[null]`. Both webhook URLs populated (onrender n8n).
  - `usaspending_test_permissive` — `time_window_hours=1440`, `min_obligated_usd=0`,
    4 award types, **no `action_types` key**. Both webhook URLs populated.
  - The original seed `usaspending_expansion_event` is **gone** (operator-replaced).
- All required secrets exist in `hq-all/prd`: `GTM_MCP_URL`, `GTM_MCP_AUTH_TOKEN`,
  `MANAGED_AGENT_ID_GTM`, `MANAGED_ENVIRONMENT_ID_GTM`, `MANAGED_VAULT_ID_GTM_MCP`,
  `DEX_SERVICE_TOKEN`, `DEX_BASE_URL`, `TRIGGER_SHARED_SECRET`, `BACKEND_X_SERVICE_TOKEN`.
- hq-x `pyproject.toml` **already depends on** `duckdb`, `pyarrow`, `boto3`,
  `pylance`, `fastmcp>=2.0`, `anthropic`, `httpx`. (The plan-of-attack's repeated
  "hq-x acquires zero data-stack deps" premise is already false on disk — not a
  cutover blocker, but the stated invariant is fiction.)
- PR-4 caller code does **not** exist yet: no `gtm_mcp_client.py`, no
  `app/routers/internal/gtm_signals.py`, no `src/trigger/gtm-signals-daily.ts`,
  and `gtm_signals_v1.py` is still the DEX-proxy. The compiler, cohort writer,
  `dex_client.compute_signal_cohort`, the DEX `/compute` executor, and the
  backfill all exist (PRs 1–3). This is a genuine pre-implementation review.

---

## BLOCKER defects

### D1 — The cron/fire/preview callers will silently DROP the SAM join, the ORDER BY, and the scan_filter unless they forward every compiled field. Default behavior = full 107M-row scan with no enrichment.

**What breaks.** `dex_client.compute_signal_cohort(...)` defaults
`select=None, join=None, order_by=None, scan_filter=None`
(`apps/hq-x/app/services/dex_client.py:500-533`). `execute_cohort` treats those
Nones as "no join / no projection / no order / **no scan pushdown → full scan**"
(`apps/data-engine-x/app/services/lance_cohort_exec.py:136-166`: `if scan_filter:`
push to BTREE `else: logger.warning(... "full scan")`). The compiler **produces**
all four (`CompiledCriteria.select/join/order_by/scan_filter`,
`gtm_signal_compiler.py:48-58, 127, 207, 219, 237`), but the plan's PR-4 prose
(plan lines 20-21, plan-of-attack §H/§J) says only "compile →
`compute_signal_cohort` → write_cohort" and **never states that the caller must
pass `compiled.join`, `compiled.select`, `compiled.order_by`, and
`compiled.scan_filter`**. A literal implementation that calls
`compute_signal_cohort(spine_target=…, where_sql=…, bindings=…)` and stops will:
(a) drop the SAM INNER JOIN → cohort rows lose `cage_code` / `legal_business_name`
and, worse, **lose the membership-narrowing the INNER JOIN performs** (rows whose
`recipient_uei` has no SAM match vanish in legacy but survive here → different
cohort); (b) drop ORDER BY → `LIMIT 50000` returns an arbitrary 50K, not top-50K
by obligation; (c) drop scan_filter → DuckDB scans the entire
`transaction_fpds_lance` (the legacy code's whole reason for the pyarrow pushdown,
`gtm_signal_cohort.py:180-185`) → multi-minute scan / memory blowup on the Railway
web container, which the plan-of-attack §R1 itself calls "non-negotiable."

**Code evidence.** `dex_client.py:506-512` (None defaults) →
`lance_cohort_exec.py:158-164` (scan_filter gate) + `:176-191` (join only if
`join`) + `:209-213` (order only if `order_by`). Compiler emits them at
`gtm_signal_compiler.py:127` (scan_filter), `:207` (join), `:219` (order_by).

**Fix.** Make the PR-4 callers (the `/internal/signals/run-daily` cron, `/fire`,
`/preview`, `/run-agent`) pass the full compiled shape:
`compute_signal_cohort(spine_target=c.spine_target, where_sql=c.where_sql,
bindings=c.bindings, select=c.select, join=c.join, order_by=c.order_by,
scan_filter=c.scan_filter, max_rows=…)`. Add an explicit acceptance test that the
cron's outbound DEX body contains a non-null `join`, `order_by`, and `scan_filter`
for `usaspending_net_new_100k`. Belt-and-suspenders: have the executor **hard-fail
(422) instead of warn** when a query targets a known-huge dataset
(`usaspending.transaction_fpds_lance`) with no `scan_filter`, so a dropped pushdown
can never reach a full scan silently.

---

### D2 — ORDER BY changes from numeric to **lexical**, which changes WHICH rows survive the 50K cap → the dispatched cohort is a different set of companies.

**What breaks.** Legacy: the SELECT computes
`TRY_CAST(tx.federal_action_obligation AS DOUBLE) AS federal_action_obligation`
and then `ORDER BY federal_action_obligation DESC NULLS LAST`
(`gtm_signal_cohort.py:208, 214`) — i.e. it orders by the **DOUBLE**. New executor:
`ORDER BY s."federal_action_obligation" DESC NULLS LAST`
(`lance_cohort_exec.py:212`) where `s` is `(SELECT * FROM spine …)` and
`federal_action_obligation` in `transaction_fpds_lance` is stored as **text**
(USAspending obligations are text in Lance — that's exactly why the compiler casts
in the predicate, `gtm_signal_compiler.py:151`). So the new ORDER BY sorts
**lexically**: `"9000" > "100000"`, `"99" > "100000"`. For `usaspending_test_permissive`
(min `$0`, 1440h window) the matched set far exceeds 50K, so the cap bites and
**lexical-top-50K ≠ numeric-top-50K** → the dispatched cohort to n8n is a different
population than the Modal cron sent yesterday. The plan flags this as `R-parity`
(plan line 42) but leaves it unresolved and the shipped executor does NOT fix it.

**Code evidence.** `gtm_signal_cohort.py:208` (cast aliased) + `:214` (orders the
cast) vs `lance_cohort_exec.py:207-213` (orders the raw `s.<col>`; no cast in the
ORDER BY path; the `select`-alias path at `:208` also re-selects the raw column).

**Fix.** The executor must order numerically when the order column is numeric-typed
in intent. Cleanest: extend the compiled `order_by` with an optional
`"cast": "double"` flag (set by the compiler when the criteria's order column is the
same kind it numeric-casts in predicates) and have `execute_cohort` emit
`ORDER BY TRY_CAST(s."col" AS DOUBLE) DESC NULLS LAST`. The compiler already has the
numeric-detection logic (`_is_number`, `gtm_signal_compiler.py:60-61, 151`); reuse
it for order. Add a parity unit test: a fixture with obligations
`["9000","100000","99"]` must rank `100000` first.

---

### D3 — The dispatched n8n payload's per-row **column names diverge** from the legacy payload. n8n consumers keyed on `uei` / `award_type` break.

**What breaks.** Legacy `_dispatch` ships `rows` whose keys are the SELECT
**aliases**: `uei`, `cage_code`, `legal_business_name`, `generated_unique_award_id`,
`piid`, `fain`, `award_type`, `action_type`, `modification_number`, `action_date`,
`federal_action_obligation`, `awarding_toptier_agency_name`,
`awarding_subtier_agency_name` (`gtm_signal_cohort.py:198-210`). The backfill's
`_FPDS_SELECT` instead carries the **raw** column names — `recipient_uei` (not
`uei`), `type_description` (not `award_type`)
(`backfill_gtm_signals_from_dex.py:39-51`). The new executor emits
`s."recipient_uei" AS "recipient_uei"` etc. (`lance_cohort_exec.py:208`), so the
dispatched rows will have `recipient_uei`/`type_description` keys. The plan promises
the payload is preserved "byte-for-byte" (plan line 36, assumption) — it is not. Any
n8n node referencing `$json.uei` or `$json.award_type` silently gets `undefined`.
(Also: legacy emits `action_date` as a `CAST(... AS DATE)` and `federal_action_obligation`
as a DOUBLE; the new path emits whatever the raw Lance types are, JSON-serialized via
`_json_safe` — `action_date` text stays text, obligation stays text — another shape
drift on top of the key-name drift.)

**Code evidence.** `gtm_signal_cohort.py:198-210` (aliases incl. `uei`,
`award_type`) vs `backfill_gtm_signals_from_dex.py:39-51` (raw names) vs
`lance_cohort_exec.py:207-208` (selects/aliases the raw names).

**Fix.** The criteria `select` (and the SAM join `select`) must reproduce the legacy
**aliases**, not the raw columns. The cleanest path is to add per-column aliasing to
the compiler/executor contract: extend `select` entries to support
`{"column": "recipient_uei", "as": "uei"}` and `{"column":"type_description","as":"award_type"}`,
and have the executor emit `s."recipient_uei" AS "uei"`. Update the backfill's
`_FPDS_SELECT`/`_SAM_JOIN.select` to carry those aliases. Also cast `action_date`→DATE
and `federal_action_obligation`→DOUBLE in the projection to match legacy row value
types. Add a golden-payload test that diffs the new dispatched `rows[0]` keyset
against the legacy keyset for `usaspending_net_new_100k`.

---

### D4 — The cron CANNOT compile without a gtm-mcp schema fetch (the compiler *requires* `allowed_columns`), so "skip-and-log on gtm-mcp failure" silently drops EVERY signal when the mcp is down — a strictly worse failure than today's Modal cron.

**What breaks.** `compile_criteria(..., *, allowed_columns: set[str], ...)` is a
required keyword (`gtm_signal_compiler.py:91-97`) and every identifier is checked
`if name not in allowed: raise CompileError` (`:73, :117, :137, :187, :201-205, :215`).
There is no "skip the schema gate" mode. The plan's mitigation (plan line 23,
plan-of-attack §J/§R4) says the cron should "skip-and-log that signal" on a
gtm-mcp schema-fetch failure "since DEX re-validates at execute time anyway."
But you cannot reach DEX execute-time validation **without first compiling**, and
you cannot compile **without** `allowed_columns`. So a gtm-mcp outage at 09:00 UTC
makes the schema fetch fail for **every active signal** → every signal is skipped →
**zero dispatch, logged as success**. The legacy Modal cron has no gtm-mcp
dependency at all (`gtm_usaspending_trigger_app.py` reads Postgres + R2 directly),
so this is a brand-new single point of failure that converts "mcp down" into "silent
zero-fire day." Per-signal isolation does not help when the dependency is shared and
fails identically for all.

**Code evidence.** Required kwarg + raises: `gtm_signal_compiler.py:91-97, 64-74`.
gtm-mcp `get_polaris_schema` is a remote Lance open over R2 (`polaris_server.py:255-275`)
— a real network/R2 dependency that can fail.

**Fix.** Decouple the cron from gtm-mcp. Two acceptable options:
1. **Cache the schema in hq-x.** Persist the per-spine `allowed_columns` set on the
   `business.gtm_signals` row (or a sibling table) at authoring/backfill time; the
   cron compiles against the cached set and only refreshes from gtm-mcp opportunistically.
   DEX execute-time re-validation (`lance_cohort_exec.py:136-145`) remains the security
   gate. mcp down ⇒ cron still fires off the last-known-good schema.
2. **Add a `validate_identifiers=False` compile mode** for cron runs that skips the
   `in allowed` membership check (keeps the regex belt) and trusts DEX's execute-time
   422. Then a gtm-mcp outage degrades to "compile from criteria, DEX validates" —
   never zero-fire.
   Either way, "skip-and-log" must be replaced; as written it is a silent-data-loss
   mitigation. Also fail the Trigger task (throw) if **all** signals skipped, so a
   total-skip day pages instead of going green.

---

### D5 — The dual-run sequence is written for a state that no longer exists: both live signals are ALREADY `webhook_target='test'`. The plan's "flip Modal to a test sink" step is a no-op, and the real double-fire / zero-fire window is mislocated.

**What breaks.** The plan's cutover (plan §c step 2, plan-of-attack §K3) is:
"for ONE day, PATCH DEX `ops.gtm_signals.webhook_target='test'` so the Modal cron
dispatches nowhere-real; only hq-x dispatches to prod." This assumes the Modal cron
is **currently firing prod**. It is not — live prod shows **both** signals at
`webhook_target='test'` (verified above). The Modal cron right now POSTs to the
**test** onrender URLs (`gtm_usaspending_trigger_app.py:222-243` selects the URL by
`webhook_target`). Consequences:
- The "flip Modal to test" step is already satisfied → it does nothing.
- "Enable hq-x cron; only hq-x dispatches to prod" is **false** unless someone
  *also* flips the **hq-x** copy of the signals to `webhook_target='prod'`. Since
  the backfill copies `webhook_target` verbatim (`backfill_gtm_signals_from_dex.py:114`),
  the hq-x rows will ALSO be `'test'` → during dual-run **both crons fire the TEST
  sink and nothing fires prod** → the "cutover" validates nothing about prod dispatch.
- If, to actually exercise prod, the operator flips hq-x signals to `'prod'` while
  the Modal cron is still enabled, and the Modal rows ever get flipped to `'prod'`
  too (or were, for any signal), you get the classic **double-fire to prod** — the
  very thing the plan claims to prevent.
- The genuine knob that mutes Modal is **`is_active=false`** on the DEX
  `ops.gtm_signals` rows (cron filter: `WHERE is_active = true`,
  `gtm_usaspending_trigger_app.py:86`) or stopping the Modal app — **not**
  `webhook_target`. The plan's claim that "PATCH webhook_target=test mutes Modal"
  (attack #4) is only accidentally true today because prod is already test; it is
  **not** a reliable mute.

**Code evidence.** Live API readout (2 rows, both `webhook_target=test`). Modal URL
selection by target: `gtm_usaspending_trigger_app.py:224-229`. Modal cron active
filter is `is_active`, not target: `:86`. Backfill copies target verbatim:
`backfill_gtm_signals_from_dex.py:114`.

**Fix.** Rewrite the cutover to mute Modal by the field it actually reads. Corrected
sequence in the "Corrected cutover" section below. In short: dual-run with **hq-x
firing the TEST sink** (parity-diff against DEX preview), then mute Modal by
**`is_active=false` on DEX rows OR `modal app stop`** (not `webhook_target`), then
flip hq-x to `prod` as the single authority. Never have both crons at `prod`
simultaneously for the same slug.

---

### D6 — `/run-agent` rewire feeds `_format_initial_user_message` a dict whose keys do not match what the legacy preview produced. The agent's seed message loses `matched_count` / `limited`.

**What breaks.** `_format_initial_user_message` consumes exactly these keys:
`rows`, `matched_count`, `limited`, `target`, `criteria`, `spine_target`
(`apps/hq-x/app/routers/gtm_signals_v1.py:194-199`). The legacy
`preview_signal_cohort` returns precisely that shape — including `matched_count`
and **`limited`** (`gtm_signal_cohort.py:284-295`). The replacement,
`dex_client.compute_signal_cohort` → DEX `/compute`, returns a **different**
envelope: `{spine_target, matched_count, row_count, **truncated**, columns, rows,
sql_elapsed_ms}` (`lance_cohort_exec.py:232-240`; dex_client docstring
`dex_client.py:514-517`). It has **`truncated`, not `limited`**, has **no
`criteria`**, has **no `target`**. So a naive rewire that passes the `/compute`
result straight into `_format_initial_user_message` yields `limited=None`
(message drops the "cohort truncated" annotation, `gtm_signals_v1.py:211`),
`target=None`, `criteria={}` → the agent's seed prompt loses the signal's intent and
truncation flag. The plan says "build the preview-shaped dict
`_format_initial_user_message` expects" (plan line 20, plan-of-attack §H) but does
not enumerate the key remap, and the consumed keys do not line up with the new
producer.

**Code evidence.** Consumer keys: `gtm_signals_v1.py:194-199`. Legacy producer
(matches): `gtm_signal_cohort.py:284-295`. New producer (mismatch): `truncated` not
`limited`, no `criteria`/`target`: `lance_cohort_exec.py:232-240`.

**Fix.** In the rewired `/run-agent`, build the dict explicitly from the hq-x
signal row + the `/compute` result:
`{"rows": res["rows"], "matched_count": res["matched_count"],
"limited": res["truncated"], "target": payload.target,
"criteria": signal["criteria"], "spine_target": signal["spine_target"]}`. Add a
router test asserting the seeded `initial_message` contains `matched_count:` and the
truncation annotation when `truncated=True`.

---

## HIGH defects

### D7 — Prod backfill `display_name` is always empty AND `webhook_target` is copied as `'test'`, so even after backfill the hq-x rows can't dispatch prod and have no human label.

`ops.gtm_signals` has **no `name`/`display_name` column** (DEX migration
`20260525170000_ops_gtm_signals.sql:9-17`; the DEX list endpoint returns no such
field, `apps/data-engine-x/app/routers/gtm_signals_v1.py:54-72`). The backfill reads
`sig.get("name") or sig.get("display_name")` (`backfill_gtm_signals_from_dex.py:107`)
— both always absent → `display_name` falls back to the slug. Harmless but means the
plan-of-attack's `POST /api/v1/signals` "display_name" authoring field starts blank
for migrated rows. More importantly, `webhook_target` is copied verbatim
(`:114`) = `'test'` for both live signals, reinforcing D5: post-backfill the hq-x
cron, if enabled, fires the **test** sink. **Fix:** decide the intended prod-dispatch
posture explicitly during backfill (this is the cutover's actual prod-enable knob)
rather than inheriting `'test'`; and drop the dead `name`/`display_name` lookups or
seed a real label.

### D8 — `webhook_prod_url` IS returned by DEX `list_gtm_signals`, so attack #8's worst case (no prod URL field) does NOT occur — but the backfill's `sig.get("webhook_url")` fallback is dead code that masks a real bug if the field name ever changes.

Confirmed: DEX list returns `webhook_test_url` + `webhook_prod_url` (router
`_row_to_dict`, `apps/data-engine-x/app/routers/gtm_signals_v1.py:61-72`; live API
readout shows both populated). So the cohort CAN dispatch — attack #8's failure mode
is not present. However `backfill_gtm_signals_from_dex.py:111-113` falls back to a
non-existent `webhook_url` key; if a future DEX rename drops `webhook_prod_url`, the
backfill would silently write empty URLs instead of failing loudly. **Fix:** assert
`webhook_prod_url`/`webhook_test_url` presence in the backfill and remove the
`webhook_url` fallback.

### D9 — Hand-rolling MCP streamable-HTTP is unnecessary and the planned `get_polaris_schema(...) -> set[str]` must PARSE a markdown string, not read JSON.

The plan says build `gtm_mcp_client.py` as a "thin httpx MCP streamable-HTTP caller
(initialize → tools/call)" (plan line 19, plan-of-attack §F). Two problems:
1. **The mcp SDK is already available in hq-x** — `fastmcp>=2.0` is a declared dep
   (`pyproject.toml`), which bundles the `mcp` client (`mcp.client.streamable_http` +
   `ClientSession`). Hand-rolling the JSON-RPC `initialize`/`notifications/initialized`/
   `tools/call` handshake + SSE session-id header dance is error-prone (the
   plan's own `R-mcp-protocol`, plan line 45) and duplicative. Use the SDK client.
2. **`get_polaris_schema` returns a formatted markdown string, not structured data**
   — `polaris_server.py:255-275` returns `"# ns.dataset\n# URI…\n# Rows: N\n# Columns: K\n\n  colname: type\n  …"`,
   and on a bad dataset returns a **string starting with `ERROR …` with HTTP 200**
   (`:259-260`), not an exception. The planned `-> set[str]` must parse the indented
   `  name: type` lines AND detect the `ERROR ` sentinel, or it will (a) silently
   return an empty/garbage column set → the compiler rejects every identifier as
   "unknown," or (b) treat an error string as a schema. **Fix:** use the bundled mcp
   SDK `ClientSession.call_tool`, and parse the text body defensively: split lines,
   take tokens before the first `:` on indented lines, and raise if the body starts
   with `ERROR ` or yields zero columns.

### D10 — Deploy ordering: merging PR-4 redeploys hq-x and repoints the LIVE `/api/v1/signals/*` surface at empty `business.gtm_signals` in prod unless the prod backfill ran first.

PR-4 converts `gtm_signals_v1.py` from DEX-proxy to hq-x-native reads of
`business.gtm_signals` (plan §b.2, plan-of-attack §E). The plan states prod
`business.gtm_signals` is **empty** (plan line 9) and the migration + backfill are
"DEV only" so far. Railway auto-deploys hq-x on merge to `main`. So the instant PR-4
merges, the live `GET /api/v1/signals`, `/run-agent`, `/fire`, `/preview` for prod
read an **empty table** → list returns `[]`, `/run-agent` and `/fire` 404 on every
slug — a live regression of the BFF surface — until the operator manually runs the
prod migration + backfill (plan §a, an out-of-band manual step). The window is
"merge → operator notices → runs backfill," unbounded. The plan's `R-prod-deploy-order`
(plan line 46) names this but the step ordering in §c still has "(a) prod readiness"
as a *separate* phase the operator must remember to do **before** merging PR-4, with
no enforcement. **Fix:** make prod migration + backfill a hard gate **before** PR-4
merges (run §a, verify `SELECT count(*) FROM business.gtm_signals = 2` in prd, THEN
merge PR-4). Better: keep the DEX-proxy fallback in `gtm_signals_v1.py` for the read
paths behind a feature check until the backfill is confirmed, so an empty table
degrades to the old behavior instead of 404s.

---

## MEDIUM / LOWER

### D11 — `count_only` matched_count and the truncated-path `count(*)` re-run double the R2 scan cost; acceptable but note for the wide-window signal.
When truncated, the executor runs a second `SELECT count(*)` over the same
`base_from` (`lance_cohort_exec.py:222`) — a second full predicate evaluation on an
already-materialized Arrow table (cheap, in-memory) so this is fine. No fix needed;
flagging because the plan-of-attack §G implies a separate count query and a reviewer
might think it re-scans R2 (it does not — `spine_tbl` is already in memory).

### D12 — `where_sql` `;`-rejection is the only statement-stacking guard, and it lives only in the executor, not the compiler.
`execute_cohort` rejects `;` in `where_sql` (`lance_cohort_exec.py:122-123`); the
compiler never emits `;` and validates identifiers, so this is defense-in-depth and
adequate **for operator-authored criteria**. If criteria authoring is ever exposed
beyond the operator (the plan-of-attack §R2 anticipates this), note that the value
bindings are safe (`?` params) but a future compiler bug that interpolates a value
into `where_sql` would bypass the `?` path. No change required now; keep the
execute-time identifier re-validation (`lance_cohort_exec.py:136-145`) as the real gate.

### D13 — Rollback of the dual-run is clean EXCEPT `modal app stop` and any `ops.gtm_signals.is_active` mutation.
Per plan §c: reverting PR-4 restores the DEX-proxy router + leaves the Modal cron
intact **only if** the Modal cron was not already stopped. `modal app stop
data-engine-x-gtm-usaspending-trigger` (plan §c step 3) is reversible
(`modal deploy` re-creates the scheduled function), so that is fine. The
**irreversible-by-omission** risk is: if the operator muted Modal via
`is_active=false` on DEX rows (the correct mute per D5) and then reverts PR-4, the
DEX cron is now muted AND hq-x is reverted → **both dark**. **Fix:** rollback runbook
must re-set `is_active=true` on the DEX rows as the first step of any PR-4 revert,
and must not `modal app stop` until after a full clean hq-x cycle (the plan already
says this, keep it). `TRUNCATE business.gtm_signals` (plan line 16) is a safe
reversal of the backfill only while nothing reads it — true pre-PR-4-deploy, false
after; sequence accordingly (see D10).

### D14 — `between` op numeric coercion requires BOTH bounds numeric; a mixed `[0, "100000"]` silently compares as text. Not in the live seeds, but a sharp authoring edge.
`gtm_signal_compiler.py:160` sets `numeric = _is_number(lo) and _is_number(hi)`; a
criteria with one numeric and one string bound drops the `TRY_CAST` for both →
lexical comparison. Live signals use `gte`/`in` only, so no current impact. **Fix
(optional):** cast per-bound, or raise on mixed-type `between`.

---

## Answers to the specific attack prompts

1. **PARITY** — NO, not byte-for-byte. Divergences: SAM INNER JOIN dropped unless
   forwarded (D1); ORDER BY numeric→lexical changes cap membership (D2); payload row
   **key names** change `uei`/`award_type` → `recipient_uei`/`type_description` (D3);
   value types (`action_date` DATE→text, obligation DOUBLE→text) change (D3). WHERE
   semantics + the `action_type IS NULL` OR-branch DO match
   (`gtm_signal_compiler.py:164-177` ≈ `gtm_signal_cohort.py:141-153`), and numeric
   `TRY_CAST` in predicates matches (`:151` ≈ `:132`).
2. **CRON SCHEMA DEPENDENCY** — The cron **cannot compile without a schema fetch**
   (compiler requires `allowed_columns`, D4); "skip-and-log" silently zero-fires on a
   shared-dependency outage. Must cache schema or add a no-validate compile mode.
3. **ORDERING / DEPLOY HAZARDS** — Merging PR-4 redeploys hq-x and points the live
   surface at an empty prod table → 404s until the manual backfill runs (D10). Prod
   backfill must be a hard pre-merge gate.
4. **DISPATCH CUTOVER** — `PATCH webhook_target=test` does **not** reliably mute
   Modal; the cron mutes on `is_active`, and both live signals are **already** at
   `webhook_target='test'`, so the plan's flip is a no-op and the real double-fire
   risk is at the `webhook_target='prod'` flip on hq-x while Modal is still active
   (D5).
5. **RUN-AGENT** — The new `/compute` envelope keys (`truncated`, no `criteria`, no
   `target`) do not match the keys `_format_initial_user_message` reads (`limited`,
   `criteria`, `target`) → seed message loses data unless explicitly remapped (D6).
6. **gtm_mcp_client** — Hand-rolling is unnecessary (`fastmcp`/`mcp` SDK already a
   dep) and the response is a **markdown string with an `ERROR ` sentinel at HTTP
   200**, so `-> set[str]` must parse + error-detect (D9).
7. **ROLLBACK** — Clean except: muting Modal via `is_active=false` then reverting
   PR-4 leaves both dark; `modal app stop` is reversible; `TRUNCATE` is safe only
   pre-deploy (D13).
8. **WEBHOOK PAYLOAD** — `webhook_prod_url`/`webhook_test_url` **are** returned by
   DEX and **are** populated live, and the backfill preserves them, so the cohort
   CAN dispatch (attack #8's failure mode is absent). The dead `webhook_url` fallback
   is a latent masking bug (D8).

---

## Corrected cutover step-ordering

**Pre-PR-4 (hard gates, in order):**
0. Land the parity fixes D1, D2, D3, D6 in PR-4's code, plus D4 (schema decoupling)
   and D9 (use the mcp SDK + parse defensively). These are code, not ops.
1. Apply migration `20260529T193000_gtm_signals.sql` to **prod** hq-x
   (`HQX_DB_URL_DIRECT`, `scripts/migrate`).
2. Run `backfill_gtm_signals_from_dex` against **prod**, but set the intended
   dispatch posture explicitly: backfill rows with **`webhook_target='test'`**
   (so an accidentally-enabled hq-x cron fires the test sink, not prod). Verify
   `SELECT count(*) FROM business.gtm_signals` = 2 and both `webhook_prod_url`
   non-empty (D7/D8).
3. **Only now** merge PR-4 (cron deployed PAUSED). hq-x live surface now reads a
   populated table; no 404 window (D10).

**Dual-run (the corrected dispatch cutover — D5):**
4. With the hq-x cron still **paused**, manually `POST /internal/signals/run-daily`
   once. Because hq-x rows are `webhook_target='test'`, this fires the **test** sink.
   Diff the persisted cohort against DEX `/preview` for each slug → assert row-count
   AND membership AND payload-key parity (validates D1/D2/D3 fixes).
5. **Enable the hq-x cron** at `0 9 * * *`. For one day BOTH crons run; both target
   **test** (Modal already `test`, hq-x backfilled `test`). Confirm n8n test sink gets
   exactly one hq-x payload per signal and that hq-x cohorts match Modal cohorts.
6. **Mute Modal by the field it reads** — set `is_active=false` on the DEX
   `ops.gtm_signals` rows **or** `modal app stop data-engine-x-gtm-usaspending-trigger`.
   (Do NOT rely on `webhook_target` to mute.) Confirm the Modal cron no longer fires.
7. **Flip hq-x to prod**: PATCH the hq-x `business.gtm_signals` rows to
   `webhook_target='prod'`. hq-x is now the **sole** dispatcher AND the sole one at
   `prod` — no double-fire possible (Modal is muted via is_active/stop). Confirm n8n
   prod sink receives exactly one payload per signal.
8. Rollback at any step: re-PATCH hq-x rows to `test` (mute hq-x prod), re-set DEX
   `is_active=true` (un-mute Modal) BEFORE reverting PR-4, and only `modal app stop`
   after step 7 is confirmed stable. `TRUNCATE business.gtm_signals` only valid
   before step 3.

**PR-5 (retire)** unchanged from the plan, but the retire list must also drop the
DEX `gtm_signals_v1` CRUD/preview/fire router + registration, the Modal
`fire_one_signal` + `MODAL_*` secret note (DEX CLAUDE.md §Environment), and the hq-x
`dex_client` signal methods — the plan-of-attack §I already enumerates these; the
shorter cutover-plan §"PR 5" must not lose them.
