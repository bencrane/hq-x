# Modal retry policy

> **Source of truth.** When a new `@app.function` is added or an existing one's retry shape changes, the agent reads this file. Ratchet test `apps/data-engine-x/tests/test_modal_retry_audit.py` fails CI if any `@app.function` decorator lacks an explicit `# retry-policy: <class>` comment in the decorator block.

## Place retries on the Modal function decorator, NOT in script loops

Retries on the Modal function decorator (`retries=modal.Retries(...)`) spawn a **fresh container**, which means a **fresh egress IP**, a **fresh Python interpreter**, and a **fresh observability trace**. Script-level retry loops do none of these — they hold the same container, same egress IP, same stale TLS session.

The 2026-05-24 USAspending Lance cron post-mortem proved this empirically: an 8-attempt × 128s-ceiling script-level retry loop was the wrong shape for the persistent F5 BotDefense throttle. Inside the failing IP, every retry hit the same wall; the script burned ~255s/stuck-award of dead wall-clock. The right shape was `modal.Retries(max_retries=3, backoff_coefficient=2.0)` on a per-batch worker — fresh container per retry = fresh egress IP = bypasses F5 throttling naturally.

## Budgets per failure-mode class

The retry budget MUST match the dominant failure mode. Budgets shaped for transient blips are wrong for persistent failures (post-mortem fact).

| Failure mode | Detection signal | Retry budget | Backoff | Comment shape |
|---|---|---|---|---|
| Transient httpx blip (1 in 1000) | `ConnectError`, single `ReadTimeout` | 3 attempts on decorator | 2.0× exp | `# retry-policy: modal-retries-transient` |
| Government-IP rate limit (F5 BotDefense, AWS WAF, etc.) | `RemoteProtocolError` pattern | 3 attempts on decorator, fresh container per attempt | 2.0× exp | `# retry-policy: modal-retries-ip-throttle` |
| Upstream 5xx | `HTTPStatusError 500/502/503` | 3 attempts on decorator, longer ceiling | 4.0× exp | `# retry-policy: modal-retries-upstream-5xx` |
| Upstream 429 with Retry-After | `HTTPStatusError 429` | obey `Retry-After`, max 5 attempts on decorator | server-driven | `# retry-policy: modal-retries-honor-retry-after` |
| Upstream 4xx (not 429) | `HTTPStatusError 400/403/404` | 0 attempts (fail fast) | n/a | `# retry-policy: no-retry-fail-fast` |
| Auth failure | `HTTPStatusError 401` | 0 attempts (operator must fix) | n/a | `# retry-policy: no-retry-auth-fail` |
| Cron is fast + idempotent | n/a | 0 attempts on decorator | n/a | `# retry-policy: no-retry` |
| Long-running orchestrator (>5min) | n/a | 0 attempts at orchestrator level; retries belong on worker functions | n/a | `# retry-policy: no-retry-orchestrator` |

## Comment placement

Every `@app.function(...)` decorator MUST have a `# retry-policy: <class>` comment in the line immediately preceding the decorator OR within the decorator's kwargs block. Examples:

```python
# retry-policy: modal-retries-ip-throttle
@app.function(
    image=image,
    secrets=WORKER_SECRETS,
    retries=modal.Retries(max_retries=3, backoff_coefficient=2.0),
    timeout=300,
)
def fetch_award_batch(award_ids: list[str]) -> dict[str, dict]:
    ...
```

```python
@app.function(
    # retry-policy: no-retry-orchestrator (Stage 2 fan-out workers own the retry budget)
    image=image,
    secrets=ORCHESTRATOR_SECRETS,
    timeout=3600,
)
def run_contracts_lance_daily(...) -> dict[str, Any]:
    ...
```

## Anti-pattern: script-level retry loop INSIDE a Modal-retried function

If your `@app.function` decorator already has `retries=modal.Retries(max_retries=3)`, do **NOT** add a `for attempt in range(8):` loop inside. The two compose to 3 × 8 = 24 attempts with un-controlled total wall-clock, and the inner loop doesn't get the fresh-container benefit.

Pick one level. The decorator level is correct for any persistent-IP-class failure mode (the entire point is the fresh-container reset).

## Pre-existing script-level retry loops (FIXME)

Apps that still carry a script-level retry loop pre-dating this policy are tagged `# retry-policy: script-loop-FIXME-<reason>`. They surface in CI via the ratchet test. Migration order:

1. The post-mortem already removed the loop from `usaspending_api_daily_contracts_lance_app.py`'s old `_fetch_one_award` (PR #708).
2. Remaining script-level retry loops are tracked as P1-2-followup; sweep separately.

## How the ratchet test enforces this

`apps/data-engine-x/tests/test_modal_retry_audit.py` parses every `apps/data-engine-x/modal/*.py` file via AST, finds every `@app.function(...)` decorator, and asserts that a `# retry-policy: <class>` comment exists within the decorator block (the line above the decorator, or inside the decorator's argument list, or the first line of the function body). The comment value MUST come from the canonical class list above; unknown values fail the test.

This is the same shape as `test_modal_secrets_scoped.py` — declarative gate enforced at CI time, no judgment in the loop.
