import { schedules, wait, logger } from "@trigger.dev/sdk";

/**
 * Control plane — SAM.gov Daily Contract Opportunities (active) bulk ingest.
 *
 * Trigger.dev v4 durable callback. Daily at 12:00 UTC this task:
 *   1. mints a waitpoint token (its `url` is a pre-signed HTTP callback —
 *      no API key; the callbackHash in the URL authenticates),
 *   2. POSTs the Universal Dispatcher (the ONLY Modal endpoint) with the
 *      target worker + that callback url,
 *   3. suspends on `wait.forToken` — the run is checkpointed, consuming no
 *      compute and immune to HTTP timeouts,
 *   4. resumes when the Modal worker POSTs the callback url with its terminal
 *      metadata, and resolves (or fails) the run from that.
 *
 * Trigger owns true end-to-end success/failure state — the external Command
 * Center reads it straight from Trigger's API. No polling, no heartbeat.
 */

// The body Modal POSTs to the waitpoint url becomes this run's output.
interface IngestCallback {
  status: "success" | "error";
  rows: number;
  feed: string;
}

export const samOppsBulkDispatcher = schedules.task({
  id: "sam-opps-bulk-dispatcher",
  cron: { pattern: "0 12 * * *", timezone: "UTC" },
  // Generous cap; the durable wait itself consumes no compute while suspended.
  maxDuration: 3900,
  run: async (_payload, { ctx }) => {
    // 1) Mint the durable callback token.
    const token = await wait.createToken({
      timeout: "1h",
      tags: ["sam-opps-active", "modal-dispatch"],
    });

    // 2) Fire the Universal Dispatcher and return immediately (202). The
    //    worker runs in Modal; this task does not hold the connection.
    const res = await fetch(requireEnv("MODAL_DISPATCHER_URL"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Modal-Key": requireEnv("MODAL_KEY"),
        "Modal-Secret": requireEnv("MODAL_SECRET"),
      },
      body: JSON.stringify({
        app_name: "sam-gov-pipelines",
        function_name: "ingest_sam_opps_bulk",
        kwargs: {},
        trigger_callback_url: token.url,
      }),
    });

    if (!res.ok) {
      const body = await res.text();
      throw new Error(`dispatcher ${res.status}: ${body.slice(0, 500)}`);
    }

    logger.info("Dispatched to Modal; suspending on waitpoint", {
      tokenId: token.id,
      triggerRunId: ctx.run.id,
    });

    // 3) Suspend until Modal POSTs the callback url. 4) Resolve from it.
    const result = await wait.forToken<IngestCallback>(token.id);

    if (!result.ok) {
      throw new Error(`SAM opps ingest timed out before Modal callback (token ${token.id})`);
    }
    if (result.output.status !== "success") {
      throw new Error(`SAM opps ingest failed in Modal: ${JSON.stringify(result.output)}`);
    }

    logger.info("SAM opps ingest complete", { ...result.output });
    return result.output;
  },
});

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required env var: ${name}`);
  }
  return value;
}
