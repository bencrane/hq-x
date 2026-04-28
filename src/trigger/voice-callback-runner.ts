// hqx.voice_callback_runner — every minute, ask hq-x to fire any due
// voice_callback_requests rows (§7.7). hq-x atomically claims up to 10
// rows, calls Vapi with assistantOverrides (including voicemailMessage
// when leave_voicemail_on_no_answer=true), and updates the row status.

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

const CRON_EVERY_MIN = "* * * * *";

type RunnerResponse = {
  processed: number;
  started: number;
  failed: number;
  errors: unknown[];
};

export const hqxVoiceCallbackRunner = schedules.task({
  id: "hqx.voice_callback_runner",
  cron: CRON_EVERY_MIN,
  maxDuration: 120,
  run: async (_payload, { ctx }) => {
    const result = await callHqx<RunnerResponse>(
      "/internal/voice/callback/run-due-callbacks",
      { trigger_run_id: ctx.run.id },
    );
    logger.info("hq-x callback runner batch processed", result);
    return result;
  },
});
