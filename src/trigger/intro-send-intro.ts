// intro.send_intro — durable executor for one Leg 3 intro send.
//
// Triggered per-positive-reply by the `intro.dispatch_pending_positives`
// schedule (or by manual curl). Calls hq-x's
// /internal/customer-activation/fire-intro endpoint, which mints the
// intro email_messages row + marks the classification fired.
//
// Trigger.dev task-level retry policy handles transient infra failures.
// Validation failures (bad email_message_id, classification not positive,
// already fired) are 4xx from hq-x; we surface them as `error` without
// re-raising so they don't burn retries.

import { logger, task } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

type FireIntroResult = {
  intro_email_message_id?: string;
  classification_id?: string;
  rendered_subject?: string | null;
  rendered_body_text?: string | null;
  error?: string;
  message?: string;
};

export const introSendIntro = task({
  id: "intro.send_intro",
  // One intro per run. EB send + DB writes; 5 min is plenty.
  maxDuration: 300,
  run: async (
    {
      leg2_initiative_id,
      email_message_id,
      source,
    }: {
      leg2_initiative_id: string;
      email_message_id: string;
      source?: string;
    },
    { ctx },
  ) => {
    const result = await callHqx<FireIntroResult>(
      `/internal/customer-activation/fire-intro`,
      {
        leg2_initiative_id,
        email_message_id,
        source: source ?? "schedule",
      },
    );
    logger.info("intro.send_intro completed", {
      ...result,
      trigger_run_id: ctx.run.id,
    });
    return result;
  },
});
