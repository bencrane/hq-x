# EmailBison API ↔ MCP Coverage

**Date:** 2026-04-29
**EmailBison OpenAPI version:** `1.0.0` (`api-1.json` `info.version`, line 6)
**Instance:** `https://app.outboundsolutions.com` (workspace `main-new`, id 4 — surfaced by `get_active_workspace_info`)
**MCP server:** `emailbison` — 141 tools (23 core, 118 extended) per `discover_tools`
**Scope:** Canonical reference for how hq-x talks to EmailBison. Captures every endpoint in the OpenAPI spec, classifies each MCP tool, identifies coverage gaps, and prescribes a day-one wiring plan plus a recommendation for which surface area hq-x must own internally.

This document is a reference, not an investigation log. Every claim cites either `path/file:line` or the exact MCP call used.

---

## §1. Endpoint inventory

All paths from `/Users/benjamincrane/api-reference-docs-new/emailbison/api-1.json` (lines 110–18922), grouped by the 17 numbered resource folders. Path parameters are wrapped in `{}`; query parameters are listed inline where load-bearing.

### 01-account-management

| Method | Path | Summary | Auth |
|---|---|---|---|
| GET | `/api/users` | Account Details | bearer |
| POST | `/api/users/profile-picture` | Update Profile Picture (multipart `image`) | bearer |
| PUT | `/api/users/password` | Update Password (`current_password`, `password`, `password_confirmation`) | bearer |
| POST | `/api/users/headless-ui-token` | Generate headless UI token (beta) | bearer |

### 02-campaigns

| Method | Path | Summary | Body shape | Auth |
|---|---|---|---|---|
| GET | `/api/campaigns` | List campaigns (q: `search`, `status`, `tag_ids[]`, `page`, `per_page`) | — | bearer |
| POST | `/api/campaigns` | Create a campaign (`name`, `type`) | json | bearer |
| GET | `/api/campaigns/{id}` | Campaign details (returns `settings`, `stats`, `tags`) | — | bearer |
| PATCH | `/api/campaigns/{id}/update` | Update campaign settings (`name`, `max_emails_per_day`, …) | json | bearer |
| PATCH | `/api/campaigns/{campaign_id}/pause` | Pause campaign | — | bearer |
| PATCH | `/api/campaigns/{campaign_id}/resume` | Resume campaign | — | bearer |
| PATCH | `/api/campaigns/{campaign_id}/archive` | Archive campaign | — | bearer |
| POST | `/api/campaigns/{campaign_id}/duplicate` | Duplicate campaign (`name`) | json | bearer |
| GET | `/api/campaigns/{campaign_id}/leads` | Get all leads for campaign (q: `lead_campaign_status`, `tag_ids[]`, …) | — | bearer |
| DELETE | `/api/campaigns/{campaign_id}/leads` | Remove leads from a campaign (`lead_ids[]`) | json | bearer |
| POST | `/api/campaigns/{campaign_id}/leads/attach-leads` | Import leads by IDs (`lead_ids[]`, `allow_parallel_sending`) | json | bearer |
| POST | `/api/campaigns/{campaign_id}/leads/attach-lead-list` | Import leads from existing list | json | bearer |
| POST | `/api/campaigns/{campaign_id}/leads/stop-future-emails` | Stop future emails for leads (`lead_ids[]`) | json | bearer |
| POST | `/api/campaigns/{campaign_id}/leads/move-to-another-campaign` | Move leads to another campaign | json | bearer |
| GET | `/api/campaigns/{campaign_id}/scheduled-emails` | Get all scheduled emails for campaign | — | bearer |
| GET | `/api/campaigns/{campaign_id}/sender-emails` | Get all campaign sender emails | — | bearer |
| POST | `/api/campaigns/{campaign_id}/attach-sender-emails` | Attach sender emails by ID (`sender_email_ids[]`) | json | bearer |
| DELETE | `/api/campaigns/{campaign_id}/remove-sender-emails` | Remove sender emails by ID (`sender_email_ids[]`) | json | bearer |
| POST | `/api/campaigns/{campaign_id}/stats` | Get campaign stats (summary; `start_date`, `end_date`) | json | bearer |
| GET | `/api/campaigns/{campaign_id}/line-area-chart-stats` | Full normalized stats by date | — | bearer |
| GET | `/api/campaigns/{campaign_id}/replies` | Get campaign replies | — | bearer |
| GET | `/api/campaign-events/stats` | Breakdown of events by date (q: `start_date`, `end_date`, `event_type`) | — | bearer |
| DELETE | `/api/campaigns/bulk` | Bulk delete campaigns by ID (`campaign_ids[]`) | json | bearer |
| DELETE | `/api/campaigns/{campaign_id}` | Delete a campaign | — | bearer |

Schedules sub-resource (campaign-scoped):

| Method | Path | Summary |
|---|---|---|
| POST | `/api/campaigns/{campaign_id}/schedule` | Create campaign schedule |
| GET | `/api/campaigns/{campaign_id}/schedule` | View campaign schedule |
| PUT | `/api/campaigns/{campaign_id}/schedule` | Update campaign schedule |
| GET | `/api/campaigns/schedule/templates` | View all schedule templates |
| GET | `/api/campaigns/schedule/available-timezones` | View all available schedule timezones |
| GET | `/api/campaigns/sending-schedules` | Show sending schedules across campaigns |
| GET | `/api/campaigns/{campaign_id}/sending-schedule` | Show sending schedule for campaign |
| POST | `/api/campaigns/{campaign_id}/create-schedule-from-template` | Create schedule from template |

Sequence-step sub-resource (v1 deprecated + v1.1):

| Method | Path | Summary |
|---|---|---|
| GET | `/api/campaigns/{campaign_id}/sequence-steps` | View campaign sequence steps (deprecated) |
| POST | `/api/campaigns/{campaign_id}/sequence-steps` | Create sequence steps (deprecated) |
| PUT | `/api/campaigns/sequence-steps/{sequence_id}` | Update sequence steps (deprecated) |
| PATCH | `/api/campaigns/sequence-steps/{sequence_step_id}/activate-or-deactivate` | Activate/deactivate variant |
| DELETE | `/api/campaigns/sequence-steps/{sequence_step_id}` | Delete sequence step |
| POST | `/api/campaigns/sequence-steps/{sequence_step_id}/test-email` | Send sequence step test email |
| GET | `/api/campaigns/v1.1/{campaign_id}/sequence-steps` | View campaign sequence steps (v1.1) |
| POST | `/api/campaigns/v1.1/{campaign_id}/sequence-steps` | Create sequence steps (v1.1) |
| PUT | `/api/campaigns/v1.1/sequence-steps/{sequence_id}` | Update sequence steps (v1.1) |

### 03-leads

| Method | Path | Summary |
|---|---|---|
| GET | `/api/leads` | Get all leads (q: `search`, `lead_campaign_status`, `tag_ids[]`, `excluded_tag_ids[]`, `verification_statuses[]`, `emails_sent`, `opens`, `replies`, `created_at`, `updated_at`, `page`, `per_page`) |
| POST | `/api/leads` | Create lead (`email`, `first_name`, `last_name`, `company`, `title`, `notes`, `custom_variables[]`) |
| GET | `/api/leads/{lead_id}` | Get single lead |
| PUT | `/api/leads/{lead_id}` | Update lead (full replace) |
| PATCH | `/api/leads/{lead_id}` | Update lead (partial) |
| DELETE | `/api/leads/{lead_id}` | Delete a lead |
| GET | `/api/leads/{lead_id}/replies` | Get all replies for lead |
| POST | `/api/leads/multiple` | Bulk create leads (≤500) |
| POST | `/api/leads/create-or-update/multiple` | Upsert multiple leads (≤500) |
| POST | `/api/leads/create-or-update/{lead_id}` | Upsert single lead |
| PATCH | `/api/leads/{lead_id}/unsubscribe` | Unsubscribe lead |
| POST | `/api/leads/{lead_id}/blacklist` | Add lead to blacklist |
| POST | `/api/leads/bulk/csv` | Bulk create leads using CSV |
| GET | `/api/leads/{lead_id}/scheduled-emails` | Get all scheduled emails for a lead |
| GET | `/api/leads/{lead_id}/sent-emails` | Get all sent emails for a lead |
| PATCH | `/api/leads/{lead_id}/update-status` | Update lead verification status |
| PATCH | `/api/leads/bulk-update-status` | Bulk update lead status |
| DELETE | `/api/leads/bulk` | Bulk delete leads by ID |

### 04-inbox (Replies)

| Method | Path | Summary |
|---|---|---|
| GET | `/api/replies` | Get all replies (q: `folder`, `status`, `read`, `campaign_id`, `lead_id`, `sender_email_id`, `tag_ids[]`, `search`) |
| GET | `/api/replies/{id}` | Get reply |
| GET | `/api/replies/{reply_id}/conversation-thread` | Get reply conversation thread |
| POST | `/api/replies/new` | Compose new email |
| POST | `/api/replies/{reply_id}/reply` | Create new reply |
| POST | `/api/replies/{reply_id}/forward` | Forward reply |
| PATCH | `/api/replies/{reply_id}/mark-as-interested` | Mark as interested |
| PATCH | `/api/replies/{reply_id}/mark-as-not-interested` | Mark as not interested |
| PATCH | `/api/replies/{reply_id}/mark-as-read-or-unread` | Mark as read or unread |
| PATCH | `/api/replies/{reply_id}/mark-as-automated-or-not-automated` | Mark as automated or not automated |
| PATCH | `/api/replies/{reply_id}/unsubscribe` | Unsubscribe contact that replied |
| DELETE | `/api/replies/{reply_id}` | Delete reply |
| POST | `/api/replies/{reply_id}/attach-scheduled-email-to-reply` | Attach scheduled email (links untracked → lead/campaign) |
| POST | `/api/replies/{reply_id}/followup-campaign/push` | Push reply (and lead) to "reply followup campaign" |

### 05-sender-emails

| Method | Path | Summary |
|---|---|---|
| GET | `/api/sender-emails` | List email accounts |
| GET | `/api/sender-emails/{senderEmailId}` | Show email account details |
| PATCH | `/api/sender-emails/{senderEmailId}` | Update sender email |
| DELETE | `/api/sender-emails/{senderEmailId}` | Delete email account |
| GET | `/api/sender-emails/{senderEmailId}/campaigns` | Show campaigns this account is in |
| GET | `/api/sender-emails/{senderEmailId}/replies` | Get sender-email replies |
| GET | `/api/sender-emails/{senderEmailId}/oauth-access-token` | Get OAuth access token (auto-refresh) |
| PATCH | `/api/sender-emails/signatures/bulk` | Bulk update email signatures |
| PATCH | `/api/sender-emails/daily-limits/bulk` | Bulk update daily limits |
| POST | `/api/sender-emails/imap-smtp` | Create IMAP/SMTP account |
| POST | `/api/sender-emails/bulk` | Bulk add sender emails (Google/Microsoft OAuth payload set) |
| POST | `/api/sender-emails/{senderEmailId}/check-mx-records` | Check MX records |
| POST | `/api/sender-emails/bulk-check-missing-mx-records` | Bulk check missing MX records |

### 06-warmup

| Method | Path | Summary |
|---|---|---|
| GET | `/api/warmup/sender-emails` | List email accounts with warmup stats |
| GET | `/api/warmup/sender-emails/{senderEmailId}` | Show single account warmup details |
| PATCH | `/api/warmup/sender-emails/enable` | Enable warmup |
| PATCH | `/api/warmup/sender-emails/disable` | Disable warmup |
| PATCH | `/api/warmup/sender-emails/update-daily-warmup-limits` | Update daily warmup limits |

### 07-tags

| Method | Path | Summary |
|---|---|---|
| GET | `/api/tags` | Get all tags for workspace |
| POST | `/api/tags` | Create tag |
| GET | `/api/tags/{id}` | View tag |
| DELETE | `/api/tags/{tag_id}` | Remove tag |
| POST | `/api/tags/attach-to-campaigns` | Attach tags to campaigns |
| POST | `/api/tags/remove-from-campaigns` | Remove tags from campaigns |
| POST | `/api/tags/attach-to-leads` | Attach tags to leads |
| POST | `/api/tags/remove-from-leads` | Remove tags from leads |
| POST | `/api/tags/attach-to-sender-emails` | Attach tags to email accounts |
| POST | `/api/tags/remove-from-sender-emails` | Remove tags from email accounts |

### 08-blocklist

| Method | Path | Summary |
|---|---|---|
| GET | `/api/blacklisted-emails` | Get all blacklisted emails |
| POST | `/api/blacklisted-emails` | Create blacklisted email |
| GET | `/api/blacklisted-emails/{blacklisted_email_id}` | Get blacklisted email |
| DELETE | `/api/blacklisted-emails/{blacklisted_email_id}` | Remove blacklisted email |
| POST | `/api/blacklisted-emails/bulk` | Bulk create blacklisted emails |
| GET | `/api/blacklisted-domains` | Get all blacklisted domains |
| POST | `/api/blacklisted-domains` | Create blacklisted domain |
| GET | `/api/blacklisted-domains/{blacklisted_domain_id}` | Get blacklisted domain |
| DELETE | `/api/blacklisted-domains/{blacklisted_domain_id}` | Remove blacklisted domain |
| POST | `/api/blacklisted-domains/bulk` | Bulk create blacklisted domains |

### 09-webhooks

| Method | Path | Summary |
|---|---|---|
| GET | `/api/webhook-url` | Get all webhooks |
| POST | `/api/webhook-url` | Create a new webhook (`name`, `url`, `events[]`) |
| GET | `/api/webhook-url/{id}` | Get a single webhook |
| PUT | `/api/webhook-url/{id}` | Update a webhook |
| DELETE | `/api/webhook-url/{webhook_url_id}` | Delete a webhook |
| GET | `/api/webhook-events/event-types` | Get all webhook event types |
| GET | `/api/webhook-events/sample-payload` | Get sample webhook payload (`event_type` in body, GET) |
| POST | `/api/webhook-events/test-event` | Send a test webhook event |

### 10-schedules

(All schedule paths live under the campaigns tree; see 02-campaigns "Schedules sub-resource" above.)

### 11-sequences

(All sequence-step paths live under the campaigns tree; see 02-campaigns "Sequence-step sub-resource" above.)

### 12-reply-templates

| Method | Path | Summary |
|---|---|---|
| GET | `/api/reply-templates` | Get all reply templates |
| POST | `/api/reply-templates` | Create a reply template |
| GET | `/api/reply-templates/{id}` | Reply template details |
| PUT | `/api/reply-templates/{id}` | Update a reply template |
| DELETE | `/api/reply-templates/{reply_template_id}` | Delete reply template |

### 13-custom-tracking-domains

| Method | Path | Summary |
|---|---|---|
| GET | `/api/custom-tracking-domain` | Get all custom tracking domains |
| POST | `/api/custom-tracking-domain` | Create custom tracking domain |
| GET | `/api/custom-tracking-domain/{id}` | Get a custom tracking domain |
| DELETE | `/api/custom-tracking-domain/{custom_tracking_domain_id}` | Remove custom tracking domain |

### 14-custom-variables

| Method | Path | Summary |
|---|---|---|
| GET | `/api/custom-variables` | Get all custom variables |
| POST | `/api/custom-variables` | Create a new custom variable |

### 15-ignore-phrases

| Method | Path | Summary |
|---|---|---|
| GET | `/api/ignore-phrases` | Get all ignore phrases |
| POST | `/api/ignore-phrases` | Create ignore phrase |
| GET | `/api/ignore-phrases/{ignore_phrase_id}` | Get single ignore phrase |
| DELETE | `/api/ignore-phrases/{ignore_phrase_id}` | Remove ignore phrase |

### 16-workspaces

v1 (deprecated) and v1.1 are both present. v1.1 is the recommended surface.

| Method | Path | Summary | Version |
|---|---|---|---|
| GET | `/api/workspaces` | List Workspaces | v1 deprecated |
| POST | `/api/workspaces` | Create Workspace | v1 deprecated |
| POST | `/api/workspaces/switch-workspace` | Switch Workspace | v1 deprecated |
| GET | `/api/workspaces/{team_id}` | Workspace Details | v1 deprecated |
| PUT | `/api/workspaces/{team_id}` | Update Workspace | v1 deprecated |
| POST | `/api/workspaces/invite-members` | Invite Team Member | v1 deprecated |
| POST | `/api/workspaces/accept/{team_invitation_id}` | Accept Workspace Invitation | v1 deprecated |
| PUT | `/api/workspaces/members/{user_id}` | Update Workspace Member | v1 deprecated |
| DELETE | `/api/workspaces/members/{user_id}` | Delete Workspace Member | v1 deprecated |
| GET | `/api/workspaces/v1.1` | List Workspaces | v1.1 |
| POST | `/api/workspaces/v1.1` | Create Workspace | v1.1 |
| GET | `/api/workspaces/v1.1/{team_id}` | Workspace Details | v1.1 |
| PUT | `/api/workspaces/v1.1/{team_id}` | Update Workspace | v1.1 |
| DELETE | `/api/workspaces/v1.1/{team_id}` | Delete Workspace | v1.1 |
| POST | `/api/workspaces/v1.1/users` | Create User (and add to workspace) | v1.1 |
| POST | `/api/workspaces/v1.1/{team_id}/api-tokens` | Create API token for workspace | v1.1 |
| POST | `/api/workspaces/v1.1/switch-workspace` | Switch Workspace | v1.1 |
| POST | `/api/workspaces/v1.1/invite-members` | Invite Team Member | v1.1 |
| POST | `/api/workspaces/v1.1/accept/{team_invitation_id}` | Accept Workspace Invitation | v1.1 |
| DELETE | `/api/workspaces/v1.1/members/{user_id}` | Delete Workspace Member | v1.1 |
| GET | `/api/workspaces/v1.1/master-inbox-settings` | Get Master Inbox Settings | v1.1 |
| PATCH | `/api/workspaces/v1.1/master-inbox-settings` | Update Master Inbox Settings | v1.1 |
| GET | `/api/workspaces/v1.1/stats` | Workspace stats summary | v1.1 |
| GET | `/api/workspaces/v1.1/line-area-chart-stats` | Workspace stats by date | v1.1 |

### 17-scheduled-emails

| Method | Path | Summary |
|---|---|---|
| GET | `/api/scheduled-emails` | Get all scheduled emails (workspace-wide) |
| GET | `/api/scheduled-emails/{id}` | Get scheduled email |

---

## §2. MCP coverage map

The MCP exposes 141 tools across 16 categories. Classification: **typed wrapper** (specific endpoint, schema-typed args), **generic** (escape hatches for any endpoint or analytics roll-ups), **session-state** (no endpoint, just MCP session control). Source: `discover_tools` (each category enumerated below).

### Generic tools

| Tool | Tier | Class | Backing |
|---|---|---|---|
| `discover_tools` | core | generic (discovery) | MCP-internal |
| `search_api_spec` | core | generic (discovery) | MCP-internal |
| `get_api_spec_summary` | extended | generic (discovery) | MCP-internal |
| `call_api` | core | generic (any verb, any path) | direct passthrough |
| `bulk_count` | core | generic (per-resource size probe) | derived from list endpoints |
| `bulk_export` | core | generic (CSV / summary aggregation) | fan-out over list endpoints |
| `export_leads_csv` | core | generic (CSV writer) | wraps `/api/leads` paged |
| `export_replies_csv` | core | generic (CSV writer) | wraps `/api/replies` paged |
| `search_replies` | core | generic (content search) | wraps `/api/replies` + scan |
| `get_leads_analytics` | core | generic (aggregation) | computes from `/api/leads` |
| `get_replies_analytics` | core | generic (aggregation) | computes from `/api/replies` |
| `get_campaign_analytics` | core | generic (cross-campaign aggregation) | fan-outs across `/api/campaigns` and `/api/campaigns/{id}/stats` |

### Session-state tools (workspace scope)

| Tool | Notes |
|---|---|
| `get_active_workspace_info` | Returns `{instance_url, active_workspace, primary_workspace, user, hint}`. Verified live: `{"id":"4","name":"main-new","is_primary":true}`. |
| `validate_workspace_key` | Pre-flight on a candidate `ID|TOKEN` API key. Errored on stub input: `"Invalid API key format. Expected: ID|TOKEN"`. |
| `set_active_workspace` | Side-effect: switches the MCP's per-session workspace. **Forbidden in this directive.** |
| `reset_to_primary_workspace` | Switch back to MCP-config primary. Forbidden unless mismatch detected. |

### Typed wrappers — by category

#### Campaigns (21 tools)

| Tool | Tier | Endpoint | Notes |
|---|---|---|---|
| `list_campaigns` | core | `GET /api/campaigns` | Filters: `status`, `search`, `tag_ids[]`, `page`, `per_page` |
| `get_campaign` | core | `GET /api/campaigns/{id}` | |
| `get_campaign_stats` | core | `POST /api/campaigns/{campaign_id}/stats` | Note: HTTP method is POST despite "get" naming |
| `create_campaign` | core | `POST /api/campaigns` | |
| `update_campaign` | extended | `PATCH /api/campaigns/{id}/update` | |
| `pause_campaign` | extended | `PATCH /api/campaigns/{campaign_id}/pause` | |
| `resume_campaign` | extended | `PATCH /api/campaigns/{campaign_id}/resume` | Confirmation gate (real sends) |
| `archive_campaign` | extended | `PATCH /api/campaigns/{campaign_id}/archive` | Confirmation gate |
| `duplicate_campaign` | extended | `POST /api/campaigns/{campaign_id}/duplicate` | |
| `get_campaign_leads` | extended | `GET /api/campaigns/{campaign_id}/leads` | |
| `import_leads_to_campaign` | extended | `POST /api/campaigns/{campaign_id}/leads/attach-leads` | Auto-paged bulk import |
| `remove_leads_from_campaign` | extended | `DELETE /api/campaigns/{campaign_id}/leads` | |
| `import_leads_from_list` | extended | `POST /api/campaigns/{campaign_id}/leads/attach-lead-list` | |
| `stop_future_emails_for_leads` | extended | `POST /api/campaigns/{campaign_id}/leads/stop-future-emails` | |
| `get_campaign_scheduled_emails` | extended | `GET /api/campaigns/{campaign_id}/scheduled-emails` | |
| `get_campaign_sender_emails` | extended | `GET /api/campaigns/{campaign_id}/sender-emails` | |
| `attach_sender_emails_to_campaign` | extended | `POST /api/campaigns/{campaign_id}/attach-sender-emails` | |
| `remove_sender_emails_from_campaign` | extended | `DELETE /api/campaigns/{campaign_id}/remove-sender-emails` | |
| `get_campaign_stats_by_date` | extended | `GET /api/campaigns/{campaign_id}/line-area-chart-stats` | (alias `get_campaign_line_area_stats` in `other`) |
| `get_campaign_replies` | extended | `GET /api/campaigns/{campaign_id}/replies` | |
| `get_campaign_events_stats` | extended | `GET /api/campaign-events/stats` | |

#### Leads (15 tools)

| Tool | Tier | Endpoint |
|---|---|---|
| `list_leads` | core | `GET /api/leads` |
| `get_lead` | core | `GET /api/leads/{lead_id}` |
| `create_lead` | core | `POST /api/leads` |
| `update_lead` | core | `PATCH /api/leads/{lead_id}` (or `PUT` if `replace_all=true`) |
| `update_lead_status` | extended | `PATCH /api/leads/{lead_id}/update-status` |
| `unsubscribe_lead` | extended | `PATCH /api/leads/{lead_id}/unsubscribe` |
| `get_lead_replies` | extended | `GET /api/leads/{lead_id}/replies` |
| `bulk_create_leads` | extended | `POST /api/leads/multiple` |
| `upsert_multiple_leads` | extended | `POST /api/leads/create-or-update/multiple` |
| `upsert_lead` | extended | `POST /api/leads/create-or-update/{lead_id}` |
| `blacklist_lead` | extended | `POST /api/leads/{lead_id}/blacklist` |
| `get_lead_scheduled_emails` | extended | `GET /api/leads/{lead_id}/scheduled-emails` |
| `get_lead_sent_emails` | extended | `GET /api/leads/{lead_id}/sent-emails` |
| `bulk_update_lead_status` | extended | `PATCH /api/leads/bulk-update-status` |
| `bulk_create_leads_csv` | extended | `POST /api/leads/bulk/csv` |

No typed wrapper for `DELETE /api/leads/{lead_id}` or `DELETE /api/leads/bulk` — both reachable via `call_api`.

#### Inbox / Replies (13 tools)

| Tool | Tier | Endpoint |
|---|---|---|
| `list_replies` | core | `GET /api/replies` |
| `get_reply` | core | `GET /api/replies/{id}` |
| `get_conversation_thread` | extended | `GET /api/replies/{reply_id}/conversation-thread` |
| `mark_reply_interested` | extended | `PATCH /api/replies/{reply_id}/mark-as-interested` |
| `mark_reply_not_interested` | extended | `PATCH /api/replies/{reply_id}/mark-as-not-interested` |
| `mark_reply_read_status` | extended | `PATCH /api/replies/{reply_id}/mark-as-read-or-unread` |
| `mark_reply_automated` | extended | `PATCH /api/replies/{reply_id}/mark-as-automated-or-not-automated` |
| `send_reply` | core | `POST /api/replies/{reply_id}/reply` |
| `forward_reply` | extended | `POST /api/replies/{reply_id}/forward` |
| `compose_new_email` | extended | `POST /api/replies/new` |
| `unsubscribe_reply_contact` | extended | `PATCH /api/replies/{reply_id}/unsubscribe` |
| `attach_scheduled_email_to_reply` | extended | `POST /api/replies/{reply_id}/attach-scheduled-email-to-reply` |
| `push_to_followup_campaign` | extended | `POST /api/replies/{reply_id}/followup-campaign/push` |

No typed wrapper for `DELETE /api/replies/{reply_id}` — reachable via `call_api`.

#### Sender Emails (11 tools)

| Tool | Endpoint |
|---|---|
| `list_sender_emails` | `GET /api/sender-emails` |
| `get_sender_email` | `GET /api/sender-emails/{senderEmailId}` |
| `update_sender_email` | `PATCH /api/sender-emails/{senderEmailId}` |
| `get_sender_email_campaigns` | `GET /api/sender-emails/{senderEmailId}/campaigns` |
| `get_sender_email_replies` | `GET /api/sender-emails/{senderEmailId}/replies` |
| `get_sender_email_oauth_token` | `GET /api/sender-emails/{senderEmailId}/oauth-access-token` |
| `create_imap_smtp_email_account` | `POST /api/sender-emails/imap-smtp` |
| `check_mx_records` | `POST /api/sender-emails/{senderEmailId}/check-mx-records` |
| `bulk_check_missing_mx_records` | `POST /api/sender-emails/bulk-check-missing-mx-records` |
| `bulk_update_signatures` | `PATCH /api/sender-emails/signatures/bulk` |
| `bulk_update_daily_limits` | `PATCH /api/sender-emails/daily-limits/bulk` |

No typed wrapper for `DELETE /api/sender-emails/{senderEmailId}` or `POST /api/sender-emails/bulk` (Google/Microsoft OAuth bulk add) — both reachable via `call_api`.

#### Webhooks (7 tools, all extended)

| Tool | Endpoint |
|---|---|
| `list_webhooks` | `GET /api/webhook-url` |
| `get_webhook` | `GET /api/webhook-url/{id}` |
| `create_webhook` | `POST /api/webhook-url` |
| `update_webhook` | `PUT /api/webhook-url/{id}` |
| `get_webhook_event_types` | `GET /api/webhook-events/event-types` |
| `get_sample_webhook_payload` | `GET /api/webhook-events/sample-payload` |
| `send_test_webhook_event` | `POST /api/webhook-events/test-event` |

No typed wrapper for `DELETE /api/webhook-url/{id}` — reachable via `call_api`.

#### Schedules (6 tools)

| Tool | Endpoint |
|---|---|
| `get_campaign_schedule` | `GET /api/campaigns/{campaign_id}/schedule` |
| `create_campaign_schedule` | `POST /api/campaigns/{campaign_id}/schedule` |
| `update_campaign_schedule` | `PUT /api/campaigns/{campaign_id}/schedule` |
| `get_schedule_templates` | `GET /api/campaigns/schedule/templates` |
| `get_available_timezones` | `GET /api/campaigns/schedule/available-timezones` |
| `create_schedule_from_template` | `POST /api/campaigns/{campaign_id}/create-schedule-from-template` |

No wrappers for `GET /api/campaigns/sending-schedules` or `GET /api/campaigns/{campaign_id}/sending-schedule`.

#### Sequences (4 tools)

| Tool | Endpoint |
|---|---|
| `get_sequence_steps` | `GET /api/campaigns/v1.1/{campaign_id}/sequence-steps` (v1.1 preferred) |
| `create_sequence_steps` | `POST /api/campaigns/v1.1/{campaign_id}/sequence-steps` |
| `update_sequence_steps` | `PUT /api/campaigns/v1.1/sequence-steps/{sequence_id}` |
| `send_sequence_test_email` | `POST /api/campaigns/sequence-steps/{sequence_step_id}/test-email` |

No typed wrappers for `PATCH …/activate-or-deactivate` or `DELETE …/sequence-steps/{sequence_step_id}`.

#### Tags (9 tools)

| Tool | Endpoint |
|---|---|
| `list_tags` | `GET /api/tags` |
| `create_tag` | `POST /api/tags` |
| `get_tag` | `GET /api/tags/{id}` |
| `attach_tags_to_leads` | `POST /api/tags/attach-to-leads` |
| `remove_tags_from_leads` | `POST /api/tags/remove-from-leads` |
| `attach_tags_to_campaigns` | `POST /api/tags/attach-to-campaigns` |
| `remove_tags_from_campaigns` | `POST /api/tags/remove-from-campaigns` |
| `attach_tags_to_email_accounts` | `POST /api/tags/attach-to-sender-emails` |
| `remove_tags_from_email_accounts` | `POST /api/tags/remove-from-sender-emails` |

No typed wrapper for `DELETE /api/tags/{tag_id}`.

#### Blocklist (8 tools)

| Tool | Endpoint |
|---|---|
| `list_blocklisted_emails` | `GET /api/blacklisted-emails` |
| `add_email_to_blocklist` | `POST /api/blacklisted-emails` |
| `bulk_add_emails_to_blocklist` | `POST /api/blacklisted-emails/bulk` |
| `remove_email_from_blocklist` | `DELETE /api/blacklisted-emails/{blacklisted_email_id}` |
| `list_blocklisted_domains` | `GET /api/blacklisted-domains` |
| `add_domain_to_blocklist` | `POST /api/blacklisted-domains` |
| `bulk_add_domains_to_blocklist` | `POST /api/blacklisted-domains/bulk` |
| `remove_domain_from_blocklist` | `DELETE /api/blacklisted-domains/{blacklisted_domain_id}` |

No typed wrappers for `GET /api/blacklisted-emails/{id}` or `GET /api/blacklisted-domains/{id}`.

#### Warmup (5 tools)

| Tool | Endpoint |
|---|---|
| `list_warmup_stats` | `GET /api/warmup/sender-emails` |
| `get_warmup_details` | `GET /api/warmup/sender-emails/{senderEmailId}` |
| `enable_warmup` | `PATCH /api/warmup/sender-emails/enable` |
| `disable_warmup` | `PATCH /api/warmup/sender-emails/disable` |
| `update_warmup_limits` | `PATCH /api/warmup/sender-emails/update-daily-warmup-limits` |

#### Workspace / Account (17 tools)

| Tool | Endpoint |
|---|---|
| `list_workspaces` | `GET /api/workspaces/v1.1` |
| `get_workspace_details` | `GET /api/workspaces/v1.1/{team_id}` |
| `create_workspace` | `POST /api/workspaces/v1.1` |
| `update_workspace` | `PUT /api/workspaces/v1.1/{team_id}` |
| `switch_workspace` | `POST /api/workspaces/v1.1/switch-workspace` |
| `get_workspace_stats` | `GET /api/workspaces/v1.1/stats` |
| `get_workspace_line_area_stats` | `GET /api/workspaces/v1.1/line-area-chart-stats` |
| `invite_team_member` | `POST /api/workspaces/v1.1/invite-members` |
| `accept_workspace_invitation` | `POST /api/workspaces/v1.1/accept/{team_invitation_id}` |
| `create_workspace_user` | `POST /api/workspaces/v1.1/users` |
| `create_api_token` | `POST /api/workspaces/v1.1/{team_id}/api-tokens` |
| `get_master_inbox_settings` | `GET /api/workspaces/v1.1/master-inbox-settings` |
| `update_master_inbox_settings` | `PATCH /api/workspaces/v1.1/master-inbox-settings` |
| `get_account_details` | `GET /api/users` |
| `update_profile_picture` | `POST /api/users/profile-picture` |
| `update_password` | `PUT /api/users/password` |
| `generate_headless_ui_token` | `POST /api/users/headless-ui-token` |

No typed wrapper for `DELETE /api/workspaces/v1.1/{team_id}` or `DELETE /api/workspaces/v1.1/members/{user_id}`.

#### Reply Templates, Tracking Domains, Custom Variables, Ignore Phrases

| Tool | Endpoint |
|---|---|
| `list_reply_templates` | `GET /api/reply-templates` |
| `get_reply_template` | `GET /api/reply-templates/{id}` |
| `create_reply_template` | `POST /api/reply-templates` |
| `update_reply_template` | `PUT /api/reply-templates/{id}` |
| `list_custom_tracking_domains` | `GET /api/custom-tracking-domain` |
| `get_custom_tracking_domain` | `GET /api/custom-tracking-domain/{id}` |
| `create_custom_tracking_domain` | `POST /api/custom-tracking-domain` |
| `list_custom_variables` | `GET /api/custom-variables` |
| `create_custom_variable` | `POST /api/custom-variables` |
| `list_ignore_phrases` | `GET /api/ignore-phrases` |
| `get_ignore_phrase` | `GET /api/ignore-phrases/{ignore_phrase_id}` |
| `create_ignore_phrase` | `POST /api/ignore-phrases` |

No typed wrapper for `DELETE /api/reply-templates/{id}`, `DELETE /api/custom-tracking-domain/{id}`, or `DELETE /api/ignore-phrases/{id}`.

#### Scheduled Emails (workspace-wide)

No typed wrappers exist for `GET /api/scheduled-emails` or `GET /api/scheduled-emails/{id}` — only campaign-scoped (`get_campaign_scheduled_emails`) and lead-scoped (`get_lead_scheduled_emails`) are covered. Reachable via `call_api` and via `bulk_export resource=scheduled_emails`.

---

## §3. Gap analysis

### A. Endpoints with a typed MCP wrapper

All endpoints listed in §2 above with a `Tool → Endpoint` mapping. Count by family:

- Campaigns: 21 of ~25 paths
- Leads: 15 of 18
- Inbox/Replies: 13 of 14
- Sender emails: 11 of 13
- Webhooks: 7 of 8
- Schedules (campaign-scoped): 6 of 8
- Sequences: 4 of 9 (only v1.1 list/create/update + v1 test-email)
- Tags: 9 of 10
- Blocklist: 8 of 10
- Warmup: 5 of 5 (full coverage)
- Workspace v1.1: 13 of 15
- Account: 4 of 4 (full coverage)
- Reply templates: 4 of 5
- Tracking domains: 3 of 4
- Custom variables: 2 of 2 (full coverage)
- Ignore phrases: 3 of 4
- Scheduled emails (workspace-wide): 0 of 2

### B. Endpoints reachable only via `call_api` (no typed wrapper, but in OpenAPI)

| Method | Path |
|---|---|
| DELETE | `/api/campaigns/{campaign_id}` |
| DELETE | `/api/campaigns/bulk` |
| DELETE | `/api/leads/{lead_id}` |
| DELETE | `/api/leads/bulk` |
| DELETE | `/api/replies/{reply_id}` |
| DELETE | `/api/sender-emails/{senderEmailId}` |
| POST | `/api/sender-emails/bulk` (bulk OAuth add) |
| DELETE | `/api/webhook-url/{webhook_url_id}` |
| GET | `/api/campaigns/sending-schedules` |
| GET | `/api/campaigns/{campaign_id}/sending-schedule` |
| PATCH | `/api/campaigns/sequence-steps/{sequence_step_id}/activate-or-deactivate` |
| DELETE | `/api/campaigns/sequence-steps/{sequence_step_id}` |
| GET | `/api/campaigns/{campaign_id}/sequence-steps` (v1, deprecated) |
| POST | `/api/campaigns/{campaign_id}/sequence-steps` (v1, deprecated) |
| PUT | `/api/campaigns/sequence-steps/{sequence_id}` (v1, deprecated) |
| DELETE | `/api/tags/{tag_id}` |
| GET | `/api/blacklisted-emails/{blacklisted_email_id}` |
| GET | `/api/blacklisted-domains/{blacklisted_domain_id}` |
| DELETE | `/api/reply-templates/{reply_template_id}` |
| DELETE | `/api/custom-tracking-domain/{custom_tracking_domain_id}` |
| DELETE | `/api/ignore-phrases/{ignore_phrase_id}` |
| GET | `/api/scheduled-emails` |
| GET | `/api/scheduled-emails/{id}` |
| All `/api/workspaces` v1 deprecated paths |
| DELETE | `/api/workspaces/v1.1/{team_id}` |
| DELETE | `/api/workspaces/v1.1/members/{user_id}` |

### C. Endpoints with no MCP path at all

Cross-checking the full deferred-tool list (loaded via `ToolSearch query="emailbison" max_results=30`) plus every category enumerated by `discover_tools`: every endpoint in `api-1.json` is reachable through at least one MCP tool — either typed or via `call_api`. **There are no API endpoints unreachable from the MCP.** `call_api` is a universal escape hatch.

The inverse direction also matters: there are MCP tools that have **no direct OpenAPI endpoint** because they are derived/synthetic:

- `get_campaign_analytics` (cross-campaign aggregation)
- `get_leads_analytics`
- `get_replies_analytics`
- `search_replies` (content search)
- `bulk_count`, `bulk_export`, `export_leads_csv`, `export_replies_csv` (fan-out + CSV writer)
- `discover_tools`, `search_api_spec`, `get_api_spec_summary`

These are useful for human/agent workflows but should not be treated as API surface to depend on programmatically — they are MCP helpers, not endpoints.

---

## §4. Day-one wiring shortlist

For each capability hq-x needs against EmailBison day one, the recommendation is **direct HTTP** as the production default, with `call_api` as a rapid-iteration fallback inside scripts/agent loops, and the typed MCP wrapper as a developer-aid only. Rationale: see §4b "MCP-as-runtime-dependency risk."

| Capability | EB endpoint(s) | Recommendation | Rationale |
|---|---|---|---|
| Create campaign | `POST /api/campaigns` | Direct HTTP | Idempotency + retry policy lives in hq-x |
| List campaigns | `GET /api/campaigns` | Direct HTTP | Used in reconciliation loops; pagination must be deterministic |
| Update campaign settings | `PATCH /api/campaigns/{id}/update` | Direct HTTP | Deterministic side-effects |
| Pause campaign | `PATCH /api/campaigns/{campaign_id}/pause` | Direct HTTP | Lifecycle, audit-logged on hq-x side |
| Resume campaign | `PATCH /api/campaigns/{campaign_id}/resume` | Direct HTTP | Real sends — hq-x must own the trigger event |
| Attach leads to campaign | `POST /api/campaigns/{campaign_id}/leads/attach-leads` | Direct HTTP | Bulk path, hq-x must own batching + idempotency key |
| Detach leads from campaign | `DELETE /api/campaigns/{campaign_id}/leads` | Direct HTTP (no typed wrapper) | Symmetric with attach |
| Stop future emails for leads | `POST /api/campaigns/{campaign_id}/leads/stop-future-emails` | Direct HTTP | |
| Attach sender emails | `POST /api/campaigns/{campaign_id}/attach-sender-emails` | Direct HTTP | |
| Detach sender emails | `DELETE /api/campaigns/{campaign_id}/remove-sender-emails` | Direct HTTP | |
| Fetch campaign sender list | `GET /api/campaigns/{campaign_id}/sender-emails` | Direct HTTP | |
| Fetch sequence (v1.1) | `GET /api/campaigns/v1.1/{campaign_id}/sequence-steps` | Direct HTTP (v1.1 only) | Pin to v1.1 — v1 is deprecated |
| Update sequence (v1.1) | `PUT /api/campaigns/v1.1/sequence-steps/{sequence_id}` | Direct HTTP | |
| Fetch campaign schedule | `GET /api/campaigns/{campaign_id}/schedule` | Direct HTTP | |
| Update campaign schedule | `PUT /api/campaigns/{campaign_id}/schedule` | Direct HTTP | |
| List replies | `GET /api/replies` | Direct HTTP | Reconciliation source for inbox |
| Fetch single reply (with body) | `GET /api/replies/{id}` | Direct HTTP | |
| Fetch conversation thread | `GET /api/replies/{reply_id}/conversation-thread` | Direct HTTP | |
| Mark reply interested / not / read / automated | `PATCH /api/replies/{reply_id}/mark-as-*` | Direct HTTP | hq-x writes back the user action |
| Send reply | `POST /api/replies/{reply_id}/reply` | Direct HTTP | Mutating |
| Pull campaign stats (snapshot) | `POST /api/campaigns/{campaign_id}/stats` | Direct HTTP | NB: POST verb despite read semantics |
| Pull stats by date | `GET /api/campaigns/{campaign_id}/line-area-chart-stats` | Direct HTTP | For drift charts |
| Cross-event stats | `GET /api/campaign-events/stats` | Direct HTTP | For backfills |
| Webhook subscription mgmt | `POST/PUT/DELETE /api/webhook-url[/{id}]` | Direct HTTP (one-off) | Configured at deploy time; rarely called |
| Verify webhook endpoint | `POST /api/webhook-events/test-event` | `call_api` ad-hoc | Dev tool only |
| Webhook event-type discovery | `GET /api/webhook-events/event-types` | Direct HTTP at deploy time | Cache locally |
| Get sample payload (a fixture) | `GET /api/webhook-events/sample-payload` | Dev-time only | Used to build hq-x receiver fixtures |
| Workspace identity probe | `GET /api/workspaces/v1.1/{team_id}` | Direct HTTP (smoke test) | |
| Account identity probe | `GET /api/users` | Direct HTTP | Health check |

`call_api` is acceptable for: ad-hoc backfills, one-off scripts, agent workflows, and the small set of verbs in §3-B that have no typed wrapper.

The MCP itself is **not** the recommended runtime path for hq-x — it should be a developer / Claude Code tool. Production code should hit `https://app.outboundsolutions.com/api/...` directly with the workspace's `ID|TOKEN` bearer.

---

## §4b. Where hq-x needs its OWN endpoints (not just MCP passthrough)

### Tracking / event capture — hq-x must own a local event store

EmailBison emits 17 webhook event types covering send, open, reply, bounce, unsubscribe, account state, manual sends, untracked replies, tag changes, and warmup auto-disable (`/api/webhook-events/event-types` live response). That is enough to be **system of record for the projection of EB's state into hq-x's domain model** — but only if every event reaches hq-x exactly once.

Webhook coverage is not gap-free for hq-x's purposes:

- **No `email_delivered` event.** `email_sent` fires on dispatch, not on remote-server acceptance. Bounces come back asynchronously as `email_bounced`. There is no "delivered, no bounce within N hours" signal — hq-x must derive deliverability heuristically (sent − bounced − unsubscribed) over a window.
- **No webhook for campaign lifecycle** (pause/resume/archive/complete). hq-x must poll `GET /api/campaigns` or `GET /api/campaigns/{id}` to detect state transitions. Recommend: short-period reconciliation (every 1–5 minutes during business hours).
- **No webhook for sequence-step or schedule edits.** Same: poll-only.
- **No webhook for lead create/update/delete** (only events that *involve* a lead via campaign activity). For hq-x writes to EB, hq-x already knows what it created. For UI-side EB edits, hq-x must reconcile via `GET /api/leads` with `updated_at` filter.
- **No documented webhook signing / HMAC.** Searches via `search_api_spec` for `signature`, `secret`, `hmac`, `redeliver`, `replay`, `idempotency` returned **no matches**. The `POST /api/webhook-url` request body (api-1.json:9918–10068) only accepts `name`, `url`, `events[]` — no `secret`. Implication: hq-x receivers must trust transport (HTTPS only), and if signing is required for production, hq-x must put EB webhooks behind a shared-secret URL path or a fronting auth proxy. This is a real gap.
- **No documented webhook redelivery / replay endpoint.** EB does not appear to expose webhook event history or a redeliver-by-id endpoint. Implication: if the hq-x receiver is down, those events are lost — reconciliation is the only recovery path.

### Reconciliation strategy — recommendation: **option 2, webhook + periodic reconciliation pull**

Pure webhook projection (option 1) is unsafe because EB has no replay. Polling-only (option 3) is non-starter for inbox UX latency: replies must surface within seconds. The middle path:

1. **Webhook is primary**: hq-x ingests every event into a raw `emailbison_webhook_event` log table (immutable, idempotency by `(event_type, scheduled_email.id, campaign_event.id)` triple), then projects into domain tables.
2. **Reconciliation is recovery**: a worker runs every N minutes per campaign, calling `GET /api/campaigns/{id}/line-area-chart-stats`, `GET /api/campaign-events/stats`, `GET /api/campaigns/{id}/replies?since=...`, and `GET /api/leads?updated_at>=...`, computing diffs against the projected state and emitting catch-up events. Frequency: 1 min for active campaigns, 30 min for paused, daily for archived.
3. **Backfill on first connect**: when a new EB workspace is wired, walk every campaign's `line-area-chart-stats` by date and every reply by `id` ascending to seed the local store.

Idempotency is hq-x's responsibility because EB does not expose an idempotency-key header. Use the `(workspace_id, scheduled_email.id, campaign_event.id, event_type)` tuple as the natural primary key on the raw event log. Webhook payloads always include all three (verified live for `email_sent`, `lead_replied`, `email_bounced` — see §5).

### Send-time signals EB doesn't expose — hq-x-only tables

These force hq-x-side persistence regardless of what EB returns:

- **Lead-stage transitions** (cold → contacted → engaged → MQL → SQL → won). EB's `lead_campaign_status` is a coarse 5-value enum (`in_sequence | sequence_finished | sequence_stopped | never_contacted | replied`); hq-x's GTM stages are richer.
- **Channel-campaign rollups** (post-rename: `channel_campaigns` aggregates one or more EB `Campaign`s under a higher-order GTM motion). EB has no concept of grouping campaigns; hq-x stores the mapping.
- **Cross-provider unified inbox** (EB replies + LinkedIn + cold-call dispositions). EB inbox is one provider stream; hq-x must merge.
- **GTM-level attribution** (revenue → which sequence step → which sender → which channel-campaign). EB stats stop at `interested`. Pipeline / closed-won attribution is hq-x's.
- **Per-tenant message log retention beyond EB's window.** hq-x retains for compliance / audit; EB retention is whatever the dedi instance keeps.
- **Variable-resolution audit** (what `{first_name}` value actually rendered for a given send). EB's webhook payload includes `scheduled_email.email_subject` and `email_body` at send time (`email_sent` sample, fields verified) — hq-x should snapshot these for legal / spam-complaint replay.

### Internal hq-x endpoints — minimum surface, justified

| Internal endpoint | Why hq-x must own it |
|---|---|
| `POST /webhooks/emailbison` | Receiver. Validates event shape, dedupes on the (workspace_id, scheduled_email.id, campaign_event.id, event_type) tuple, writes to raw log, ACKs 2xx fast (<1s), returns 5xx on storage failure to invite retry (EB retry behavior is undocumented — assume best-effort). |
| `POST /internal/emailbison/projector/run` | Forward-only consumer of the raw log into domain tables. Idempotent. Cron + on-demand. |
| `POST /internal/emailbison/reconcile/{workspace_id}` | Pull-side recovery. Calls EB list endpoints under `since` filters, emits diff events. Cron per-workspace. |
| `POST /internal/emailbison/backfill/{workspace_id}/{campaign_id}` | One-time deep crawl when a campaign is first registered with hq-x. |
| `POST /internal/emailbison/lead-attach` | hq-x-side wrapper around EB's `attach-leads`. Adds: idempotency key (so repeated calls from upstream queue don't double-attach), batch sizing, retry on transient EB errors, audit row. **This is the only proxy that earns its keep** — the others (campaigns, replies) can be thinner. |
| `GET /internal/emailbison/analytics/*` | Read-back from hq-x's projection, not EB. Decouples UI from EB rate limits and gives hq-x consistent semantics across providers. |

Things hq-x should **not** own as a thick proxy: campaign CRUD, reply CRUD, sender-email CRUD. Those are 1:1 with EB and gain nothing from hq-x mediation; let internal services call EB directly.

### MCP-as-runtime-dependency risk — recommendation

The MCP server is **a developer and agent tool, not a runtime dependency.** Reasons:

- **Auth indirection.** The MCP holds the EB API key in its config (`~/.cursor/mcp.json` per `bulk_export` description). hq-x services should authenticate to EB directly using per-workspace tokens stored in hq-x's secret manager (Doppler / Supabase Vault), not via an MCP layer.
- **Workspace-state side effects.** `set_active_workspace` and `reset_to_primary_workspace` mutate MCP-process-level state. A multi-tenant runtime needs per-request workspace scoping — that requires per-call tokens, not session switches.
- **Error shape stability.** MCP wrappers translate EB responses into MCP-friendly summaries (e.g. `list_campaigns` returns CSV-flavored text, `list_replies` returns a wrapped JSON object — see §6). Direct HTTP returns the canonical `{data: ...}` envelope from the OpenAPI. Production code wants the canonical shape.
- **Rate limits.** The MCP `bulk_export` tool documents EB's rate limit at 3,000 requests/minute. The MCP itself does not buffer or coalesce, so going through the MCP only adds latency without saving budget.
- **Versioning.** OpenAPI is the contract. The MCP tool surface lags new endpoints (verify: `discover_tools` reports 141 tools but several DELETEs and v1.1 paths have no typed wrappers — see §3-B). Coupling production code to the MCP layer means tracking two changelogs.
- **Confirmation gates.** Several typed wrappers (`resume_campaign`, `archive_campaign`, `import_leads_to_campaign`, `unsubscribe_lead`, `blacklist_lead`, `enable_warmup`, blocklist removals) require an interactive confirmation parameter (`discover_tools` descriptions). That is intentional for an agent UX, but blocks programmatic use.

**Final recommendation:** hq-x owns a thin EB HTTP client, the webhook receiver, the projector, the reconciliation worker, and the lead-attach proxy. Everything else is direct HTTP. The MCP stays in dev/agent tooling. Reconciliation strategy: **option 2 (webhook + periodic pull)**, because EB has no documented webhook redelivery, no documented signing, and no incremental-pull primitive better than `updated_at`-filtered list endpoints.

---

## §5. Webhook event ↔ API state alignment

All 17 event types from `GET /api/webhook-events/event-types` (live response) plus their canonical post-event read endpoints. Source events from spec: `09-webhooks/02-create-webhook.md:42-58` and `06-get-webhook-event-types.md`.

| Event type | Triggering action | Canonical state endpoint(s) | Notes |
|---|---|---|---|
| `email_sent` | Sequence step dispatched to lead | `GET /api/scheduled-emails/{scheduled_email.id}` + `GET /api/leads/{lead.id}` | Payload includes full `scheduled_email`, `campaign_event`, `lead`, `campaign`, `sender_email` |
| `manual_email_sent` | Operator-sent reply / forward / new email | `GET /api/replies/{reply.id}` (if surfaced) + `GET /api/sender-emails/{sender_email.id}` | "Manual" = inbox UI / `compose_new_email` / `send_reply` |
| `lead_first_contacted` | First send for lead in campaign | `GET /api/campaigns/{campaign.id}/leads` (`lead_campaign_status=in_sequence`) | Subset of `email_sent` |
| `lead_replied` | Tracked reply received | `GET /api/replies/{reply.id}` (full body) + `GET /api/leads/{lead.id}/replies` | Body sample below |
| `lead_interested` | Reply marked interested (auto or manual) | `GET /api/replies/{reply.id}` (`interested: true`) | Can fire after `lead_replied` |
| `lead_unsubscribed` | Lead clicked unsubscribe / replied with unsub phrase | `GET /api/leads/{lead.id}` (`status` updated) + `GET /api/blacklisted-emails` | |
| `untracked_reply_received` | Inbound mail to a sender that doesn't match any scheduled email | `GET /api/replies/{reply.id}` | Hq-x can call `POST /api/replies/{id}/attach-scheduled-email-to-reply` to bind to lead |
| `email_opened` | Open pixel hit | `GET /api/scheduled-emails/{scheduled_email.id}` (`opens` increments) | Multiple opens per send possible |
| `email_bounced` | Mailer-daemon DSN matched | `GET /api/replies/{reply.id}` (folder=Bounced, type=Bounced) + `GET /api/scheduled-emails/{scheduled_email.id}` | See sample below |
| `email_account_added` | New sender email connected | `GET /api/sender-emails/{sender_email.id}` | |
| `email_account_removed` | Sender email deleted | (none — entity gone) | hq-x must mark its mirror as removed |
| `email_account_disconnected` | OAuth/IMAP auth broke | `GET /api/sender-emails/{sender_email.id}` (`status != connected`) | |
| `email_account_reconnected` | Auth restored | `GET /api/sender-emails/{sender_email.id}` (`status == connected`) | |
| `tag_attached` | Tag attached to campaign / lead / sender | `GET /api/tags/{tag.id}` + per-resource `tags[]` | Polymorphic; payload identifies parent type |
| `tag_removed` | Tag detached | per-resource `tags[]` | |
| `warmup_disabled_receiving_bounces` | Warmup auto-paused (this account is bouncing inbound) | `GET /api/warmup/sender-emails/{sender_email.id}` | |
| `warmup_disabled_causing_bounces` | Warmup auto-paused (this account's outbound bouncing) | `GET /api/warmup/sender-emails/{sender_email.id}` | |

### Sampled real payload — `lead_replied`

Source: `mcp__emailbison__call_api` with `method=GET, endpoint=/api/webhook-events/sample-payload, query={event_type: "lead_replied"}`.

Webhook envelope:

```json
{
  "event": { "type": "LEAD_REPLIED", "name": "Lead Replied",
             "instance_url": "https://dedi.emailbison.com",
             "workspace_id": 1, "workspace_name": "..." },
  "data": { "reply": {...}, "campaign_event": {...}, "lead": {...},
            "campaign": {...}, "scheduled_email": {...}, "sender_email": {...} }
}
```

Key reconciliation fields on the webhook side:

- `data.reply.id`, `data.reply.uuid`, `data.reply.raw_message_id`, `data.reply.parent_id`, `data.reply.date_received`, `data.reply.interested`, `data.reply.automated_reply`, `data.reply.folder`, `data.reply.type`
- `data.campaign_event.id`, `data.campaign_event.type` (=`replied`), `data.campaign_event.created_at`
- `data.scheduled_email.id`, `data.scheduled_email.sequence_step_id`, `data.scheduled_email.raw_message_id`
- `data.lead.id`, `data.lead.email`
- `data.campaign.id`
- `data.sender_email.id`, `data.sender_email.email`

Same fields are returned by `GET /api/replies/{reply.id}` (verified live: `mcp__emailbison__get_reply reply_id=725` returned `id`, `subject`, `from`, `created_at` — abbreviated wrapper) and the canonical full-shape via `call_api GET /api/replies/{id}` (the MCP `get_reply` typed wrapper returns a *trimmed* shape; see §6).

Reconciliation key: `(workspace_id, reply.id)`. Secondary fingerprint for dedup against incoming MIME: `raw_message_id`.

### Sampled real payload — `email_sent` (CampaignEvent variant)

Source: `mcp__emailbison__call_api method=GET endpoint=/api/webhook-events/sample-payload query={event_type:"email_sent"}`.

```json
{
  "event": { "type": "EMAIL_SENT", "name": "Email Sent", "workspace_id": 1, ... },
  "data": {
    "scheduled_email": {
      "id": 4, "lead_id": 1, "sequence_step_id": 2,
      "sequence_step_order": 1, "sequence_step_variant": 2,
      "email_subject": "test subject", "email_body": "<p>test</p>",
      "status": "sent", "scheduled_date_est": "...", "scheduled_date_local": "...",
      "sent_at": "2024-08-02T06:08:38.000000Z",
      "opens": 0, "replies": 0, "raw_message_id": "<...@emailguardalpha.com>"
    },
    "campaign_event": {
      "id": 6, "type": "sent",
      "created_at_local": "...", "local_timezone": "America/New_York",
      "created_at": "..."
    },
    "lead": {"id": 1, "email": "...", "emails_sent": 1, ...},
    "campaign": {"id": 2, "name": "test"},
    "sender_email": {"id": 3, "email": "...", "status": "connected", ...}
  }
}
```

Canonical state endpoint match — `GET /api/campaigns/{id}/scheduled-emails` returns the same `scheduled_email` shape; per-record fetch via `GET /api/scheduled-emails/{id}`.

Reconciliation key for the sent event: `(workspace_id, scheduled_email.id, campaign_event.id)`. The same `scheduled_email.id` is reused across `email_sent`, `email_opened`, `email_bounced`, `lead_replied`, and `lead_interested` — it is the join key for all per-message events.

### Sampled real payload — `email_bounced`

Source: `mcp__emailbison__call_api method=GET endpoint=/api/webhook-events/sample-payload query={event_type:"email_bounced"}`.

Notable fields: `data.reply.type = "Bounced"`, `data.reply.folder = "Bounced"`, `data.reply.automated_reply = true`, `data.reply.from_email_address = "mailer-daemon@googlemail.com"`. The matched `data.scheduled_email.id` is the original send. `data.campaign_event.type = "bounce"`, distinct from `"sent"`/`"replied"`.

Canonical endpoint: `GET /api/replies/{id}` returns the bounce body and headers (DSN); `GET /api/scheduled-emails/{id}` shows the original send remains `status: sent` (the bounce does not flip the send status — hq-x must derive deliverability from the presence of an associated bounce reply).

---

## §6. MCP ergonomics findings (live read-only smoke check)

All calls below executed against `https://app.outboundsolutions.com` workspace `main-new` (id 4, primary). No mutating tools called. No workspace switch.

| Tool | Result shape vs OpenAPI | Notes |
|---|---|---|
| `get_active_workspace_info` | MCP-only shape — not from OpenAPI. Returns `{instance_url, active_workspace, primary_workspace, user, hint}`. | Use to gate every session. |
| `validate_workspace_key` | Errored on stub input with `"Invalid API key format. Expected: ID|TOKEN"`. Confirms token format. | |
| `list_campaigns` (per_page=10) | Returns **CSV-flavored text** (`id,name,status,…` rows + page-summary header), not the OpenAPI JSON `{data:[...], pagination}` envelope. | Divergence. `call_api GET /api/campaigns` returns canonical JSON. |
| `get_campaign(7)` | JSON object: `{id, uuid, name, status, type, settings:{...}, stats:{...}, tags, created_at, updated_at}`. Matches OpenAPI structure. | Note: `stats.unique_opens` returned as string `"0"` — a known type-coercion quirk (also noted in `emailbison-data-model-investigation.md`). |
| `get_campaign_stats(7, 2026-04-01..2026-04-29)` | Returns `{campaign_id, start_date, end_date, stats:{emails_sent, opened_percentage, …, sequence_step_stats:[...]}}`. Underlying call is `POST /api/campaigns/{id}/stats` — note POST verb for what reads like a GET. | Sequence_step_stats includes per-step breakdown. |
| `get_campaign_analytics(include_inactive=true)` | MCP-derived: `{summary:{...}, by_status:{...}, top_by_replies:[...], top_by_open_rate:[...]}`. Not 1:1 with any single endpoint. | Aggregates from `list_campaigns` + `get_campaign_stats` fan-out. `total_opens` returned as the literal string `"00000"` — formatting bug. |
| `list_leads` (per_page=10) | CSV-flavored text again: `id,email,name,company,status,tags,emails_sent,opens,replies` + summary line. | Divergence from OpenAPI `{data:[...], pagination}`. |
| `get_lead(8)` | JSON: `{id, email, first_name, last_name, status, custom_variables:[...], tags:[...], campaigns:[...], stats:{...}, created_at, updated_at}`. Matches spec. | Per-campaign status nested under `campaigns[]`. |
| `list_replies` (per_page=10) | JSON envelope: `{replies:[...], count, pagination:{current_page, per_page, total, last_page, from, to}, has_more, next_page, hint}`. Each item is **trimmed**: `{id, subject, from, from_name, campaign, preview}` only — no body / headers / status. | Divergence from spec, which returns the full reply object. To get bodies, fall through to `get_reply` or `call_api`. |
| `get_reply(725)` | Returned a **stub-shaped** wrapper: `{id, subject, from:{name}, campaign:{}, status:{}, created_at}`. The OpenAPI spec for `GET /api/replies/{id}` returns the full reply object including `html_body`, `text_body`, `raw_body`, `headers`, `attachments`, `from_email_address`, etc. | Significant divergence. For full reply content, use `call_api GET /api/replies/{id}`. |
| `get_account_details` | JSON: `{account:{id, name, email, workspace:{...}, profile_photo_url, …}}`. Matches spec. | |
| `discover_tools` | Returns `{message, stats:{total_tools:141, core_tools:23, extended_tools:118}, categories:[...]}`. With `category=...` returns full per-category tool list. | The 23 "core" tools are what's loaded by name; the rest are reachable via `call_api`. |
| `search_api_spec("redeliver replay idempotency")` | `{found:false, message:"No matching endpoints found", hint:"Try a different search term"}`. | Confirms EB does not document webhook replay or idempotency-key semantics. |
| `search_api_spec("signature secret hmac")` | `{found:false, ...}`. | Confirms EB does not document webhook signing. |

**Pagination behavior**: the MCP wrappers `list_replies` paginate with `{page, per_page, total, last_page, has_more, next_page}` and explicitly return `hint` text instructing `page=2` for the next slice — sensible. The CSV-text wrappers (`list_campaigns`, `list_leads`) include a "Page 1 of 1 (N total)" header but lose the structured pagination object — for programmatic walk, prefer `call_api` against `GET /api/campaigns` and `GET /api/leads`.

**Error shapes**: the `call_api` tool returns `{error: "...", hint: "Use search_api_spec ..."}` on HTTP-method mismatch (verified: `POST /api/webhook-events/sample-payload` returned `"The POST method is not supported for route ... Supported methods: GET, HEAD."`). `get_active_workspace_info` returns `{instance_url, active_workspace, ...}` plus a `hint` field on every call.

**Rate-limit behavior**: not surfaced in any MCP response observed. Documented at 3,000 req/min in the `bulk_export` tool description.

**Webhook signing / replay / idempotency**: confirmed absent from spec. hq-x must implement all three on its own receiver.

---

## How hq-x should call EmailBison — one-page recommendation

1. **Production runtime: direct HTTP to EmailBison.** `https://app.outboundsolutions.com/api/...` with `Authorization: Bearer {WORKSPACE_TOKEN}` (`ID|TOKEN` format). Maintain a thin `emailbison_client` per workspace. Pin v1.1 for sequence-steps and workspaces — the v1 paths are deprecated.

2. **MCP is a developer / agent tool, not a runtime path.** `mcp__emailbison__*` is excellent for Claude-Code-driven exploration, ad-hoc reconciliation runs, ops scripting, and one-off support tasks. It is not an appropriate dependency for hq-x services — the wrappers reshape responses (`list_replies`, `get_reply` return trimmed shapes; `list_campaigns`, `list_leads` return CSV text), the auth model is session-state with a per-process workspace switch, and the typed surface lags new endpoints.

3. **Use `call_api` (in agents) or direct HTTP (in services) for the §3-B endpoints with no typed wrapper.** Notably: every DELETE on campaigns / leads / replies / sender-emails / webhooks / tags / templates / tracking domains / ignore phrases, plus the workspace-wide `/api/scheduled-emails` reads.

4. **Workspace-state management**: pass the workspace token explicitly per call. Do not rely on MCP `set_active_workspace`. For human-driven Claude sessions, `get_active_workspace_info` is the workspace-anchor; refuse to act if it does not match the expected tenant.

5. **Pagination handling**: every list endpoint paginates with `page` + `per_page`. Use `per_page=100` (the documented max for `list_leads`) for service workers, smaller for interactive agents. The canonical envelope is `{data:[...], links:{...}, meta:{current_page, last_page, per_page, total}}` from the OpenAPI spec; the MCP CSV-text wrappers strip this — call EB directly for paged walks.

6. **Webhook + reference-list reconciliation flow**:
   1. Subscribe via `POST /api/webhook-url` with all 17 events. Store the subscription's hq-x-side URL behind a non-guessable path segment; verify health with `POST /api/webhook-events/test-event` at deploy.
   2. Receiver writes raw payloads to an append-only `emailbison_webhook_event` table, keyed on `(workspace_id, event_type, scheduled_email.id, campaign_event.id, reply.id)`. Reject duplicates at the unique-index level.
   3. A projector consumes the raw log into domain tables (`emailbison_campaign_state`, `emailbison_reply`, `emailbison_lead_campaign_status`, etc.). Idempotent.
   4. A reconciler runs every 1 minute for active campaigns, calling `GET /api/campaigns`, `GET /api/campaigns/{id}/replies`, `GET /api/leads?updated_at>=...`, and `GET /api/campaigns/{id}/line-area-chart-stats`. Diff against projected state; emit synthetic events into the raw log so the projector handles them on its own.
   5. On webhook URL change or hq-x downtime detection, run a backfill: walk every campaign by `line-area-chart-stats` for the missed window, every reply by `id` ascending since the last known max, then resume normal reconciliation cadence.

7. **Capabilities that will not come from EmailBison and must be hq-x-side tables**: GTM-stage progression, `channel_campaign` rollups (post-rename), cross-provider unified inbox merge, revenue attribution, send-time variable snapshot for legal replay.

8. **Things to stop doing**: do not use the MCP CSV-text list tools in production code; do not use `get_reply` typed wrapper for body content (use `call_api`); do not assume webhook deliveries are signed, replayable, or idempotent on the EB side — hq-x owns all three properties.
