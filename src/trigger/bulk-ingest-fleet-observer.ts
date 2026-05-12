import { schedules, logger } from "@trigger.dev/sdk";

// Independent fleet sentinel for the bulk-ingest pipeline (FMCSA + every
// source registered in bulk_ingest.feed_schedule_config).
//
// Why this lives in Trigger.dev (in hq-x) and not Modal:
//   The Modal heartbeat is the dispatcher for FMCSA — if Modal cron drops
//   (preempted, queue stuck, account suspended), the heartbeat goes silent
//   and so does anything that depends on it. This observer runs in hq-x's
//   Trigger.dev scheduler, queries DEX (which sits in front of Postgres
//   independently of Modal), and computes drift. A Modal cron drop
//   produces stale last_run_at values; the dispatch view classifies the
//   affected feeds LATE; this task surfaces the alert.
//
// Cross-source by default — reads from /api/v1/bulk-ingest/dispatch-state
// which UNIONs FMCSA's existing ledger with the generic bulk_ingest.*
// primitive. As new sources land, the observer picks them up
// automatically.
//
// Read-only by design. Does not write to Modal, Postgres, or any other
// state. Alerting goes through Trigger.dev's logger — the operator wires
// that to whatever channel they want (Slack, email, dashboard).
//
// Auth: uses DEX_SUPER_ADMIN_API_KEY for the read-only call. The operator
// sets this in Trigger.dev's project env. The flexible-auth chain in DEX
// accepts super-admin API keys (see app/auth/__init__.py).
//
// Cron disabled by default — enable from the Trigger.dev dashboard once
// the operator has confirmed DEX_SUPER_ADMIN_API_KEY is set.

interface DispatchStateRow {
  source_id: string;
  feed_name: string;
  enabled: boolean;
  dispatch_state: string;
  drift_minutes: number | null;
  last_outcome: string | null;
  last_error_class: string | null;
  last_error_message: string | null;
  last_run_id: string | null;
  last_started_at: string | null;
  expected_cadence_minutes: number;
  expected_cadence_jitter_minutes: number;
}

interface DispatchStateResponse {
  data?: { feeds?: DispatchStateRow[] };
  error?: string;
}

const CRITICAL_STATES = new Set(["LATE", "MISSING", "PROBE_ERROR"]);

export const BULK_INGEST_FLEET_OBSERVER_TASK_ID = "bulk-ingest-fleet-observer";

export const bulkIngestFleetObserver = schedules.task({
  id: BULK_INGEST_FLEET_OBSERVER_TASK_ID,
  maxDuration: 60,
  // cron: {
  //   pattern: "*/5 * * * *",
  //   timezone: "UTC",
  // },
  run: async () => {
    const apiUrl = (process.env.DEX_API_BASE_URL ?? "").trim().replace(/\/+$/, "");
    const apiKey = process.env.DEX_SUPER_ADMIN_API_KEY;
    if (!apiUrl) {
      throw new Error("DEX_API_BASE_URL is not set");
    }
    if (!apiKey) {
      throw new Error("DEX_SUPER_ADMIN_API_KEY is not set");
    }

    const authHeaders = {
      Authorization: `Bearer ${apiKey}`,
      Accept: "application/json",
      "Content-Type": "application/json",
    };

    // Phase 0a observability: record this observer run in the DEX ingest ledger.
    // Best-effort — ledger failures never abort the observation logic.
    let obsRunId: string | null = null;
    try {
      const startResp = await fetch(
        `${apiUrl}/api/v1/internal/observability/runs/start`,
        {
          method: "POST",
          headers: authHeaders,
          body: JSON.stringify({
            display_name: "fleet_observer",
            run_metadata: { writer: "bulk-ingest-fleet-observer" },
          }),
        },
      );
      if (startResp.ok) {
        const startPayload = (await startResp.json()) as { run_id?: string; data?: { run_id?: string } };
        obsRunId = startPayload.run_id ?? startPayload.data?.run_id ?? null;
      } else {
        logger.warn("observability/runs/start returned non-2xx", { status: startResp.status });
      }
    } catch (err) {
      logger.warn("observability start failed (non-fatal)", { err: String(err) });
    }

    const url = `${apiUrl}/api/v1/bulk-ingest/dispatch-state`;
    const observedAt = new Date().toISOString();

    let dispatchError: string | null = null;
    let summary: Record<string, unknown> = { observed_at: observedAt };

    try {
      const response = await fetch(url, {
        method: "GET",
        headers: authHeaders,
      });

      if (!response.ok) {
        const body = await response.text().catch(() => "<no body>");
        throw new Error(`dispatch-state fetch failed: ${response.status} — ${body}`);
      }

      const payload = (await response.json()) as DispatchStateResponse;
      const feeds = payload.data?.feeds ?? [];

      const byState = new Map<string, number>();
      const bySource = new Map<string, number>();
      const critical: DispatchStateRow[] = [];
      for (const feed of feeds) {
        const state = feed.dispatch_state ?? "MISSING";
        byState.set(state, (byState.get(state) ?? 0) + 1);
        bySource.set(feed.source_id, (bySource.get(feed.source_id) ?? 0) + 1);
        if (CRITICAL_STATES.has(state)) {
          critical.push(feed);
        }
      }

      summary = {
        observed_at: observedAt,
        total_feeds: feeds.length,
        by_state: Object.fromEntries(byState),
        by_source: Object.fromEntries(bySource),
        critical_count: critical.length,
      };

      if (critical.length > 0) {
        logger.error("bulk-ingest-fleet-observer: critical feeds detected", {
          ...summary,
          critical: critical.map((f) => ({
            source_id: f.source_id,
            feed_name: f.feed_name,
            dispatch_state: f.dispatch_state,
            drift_minutes: f.drift_minutes,
            last_error_class: f.last_error_class,
            last_outcome: f.last_outcome,
            last_run_id: f.last_run_id,
          })),
        });
      } else {
        logger.info("bulk-ingest-fleet-observer: fleet healthy", summary);
      }
    } catch (err) {
      dispatchError = String(err);
      logger.error("bulk-ingest-fleet-observer: dispatch-state fetch failed", { err: dispatchError });
    }

    // Complete the observability run record.
    if (obsRunId) {
      try {
        await fetch(
          `${apiUrl}/api/v1/internal/observability/runs/${obsRunId}/complete`,
          {
            method: "POST",
            headers: authHeaders,
            body: JSON.stringify({
              status: dispatchError ? "failed" : "succeeded",
              error_message: dispatchError,
            }),
          },
        );
      } catch (err) {
        logger.warn("observability complete failed (non-fatal)", { err: String(err) });
      }
    }

    if (dispatchError) {
      throw new Error(dispatchError);
    }

    return summary;
  },
});
