// hqx.voice_callback_reminders — every 5 minutes, ask hq-x to send the
// SMS callback-reminders for any voice_callback_requests rows whose
// preferred_time falls inside the next 20-minute window (§7.6).
//
// hq-x owns the templating, suppression check, and reminder_sent_at
// stamping — this task is a thin scheduler.

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

const CRON_EVERY_5_MIN = "*/5 * * * *";

type RemindersResponse = {
  processed: number;
  sent: number;
  suppressed: number;
  errors: unknown[];
};

export const hqxVoiceCallbackReminders = schedules.task({
  id: "hqx.voice_callback_reminders",
  cron: CRON_EVERY_5_MIN,
  maxDuration: 120,
  run: async (_payload, { ctx }) => {
    const result = await callHqx<RemindersResponse>(
      "/internal/voice/callback/send-reminders",
      { trigger_run_id: ctx.run.id },
    );
    logger.info("hq-x reminder batch processed", result);
    return result;
  },
});
