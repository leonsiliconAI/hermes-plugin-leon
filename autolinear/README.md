# autolinear

Part of the [leon plugin](../README.md), and a Hermes plugin of its own until 2026-11-26.

Hermes plugin that gives each of Leon's Slack messages its own Linear issue and channel the moment it arrives, through [autolinear](https://github.com/leonsilicon/leonsilicon.ai/blob/main/apps/autolinear/README.md), where Hermes handles the request.

## Why

Every message Leon sends to Hermes in his DM or a project channel (`project-…`) gets a new Linear issue in Awaiting Init and its own Slack channel. AutoLinear edits his message to link the channel where Hermes answers and follows up on the work.

## What happens

1. Leon writes to Hermes in his DM or a project channel (`#project-…`), at the top or in a thread.
2. Once the message reaches Hermes and passes dedup and the bot check, the plugin calls `POST /intake` in the background. AutoLinear creates the issue, opens its `leon-<n>-<first words>` channel, and posts a copy of the message there as Leon. Once Slack confirms the copy, AutoLinear replaces the original message with the channel link.
3. Hermes adds 👀 to acknowledge the request. It keeps the message, Slack thread context and file links when AutoLinear moves the request. In an existing issue channel, the handoff uses Leon's conversation there.
4. Hermes handles the request in its own model turn. [slack-flat-replies](../slack_flat_replies/README.md) keeps top-level requests and threaded requests in their respective conversations.
5. slack22linear sees 👀 in the issue channel and moves the issue to Executing. Hermes uses the [Autolinear status commands](../../../packages/ai/leonsilicon/services/slack22linear/README.md#autolinears-commands) as it works. It can handle routine reports from delegated agents silently and sends a command when a report calls for a status change.

## Which messages

A message moves when Hermes answers it, Leon (`AUTOLINEAR_USER_ID`) wrote it, it isn't a command (`/new`, `!stop`) or a reaction-triggered turn, and it's in his DM or a channel whose name starts with `project-`, not in `AUTOLINEAR_SKIP_CHANNELS`. Everywhere else, task channels (`leon-<number>-…`) and `#feed-` channels included, Hermes answers where it is. So does a channel whose name Hermes couldn't read: Hermes caches the channel's ID as its name when Slack's lookup fails, so the plugin drops that cache entry and doesn't guess (Leon 2026-11-26, after Slack timeouts turned four "mark as done" replies in task channels into new issues).

The early start, before Hermes's own gates have run (`_prefilter_inbound`), is only for messages Hermes always answers: also no `/` or `!` at the start and no mention of anyone but Hermes. Anything else that Hermes ends up answering starts when its turn is built (`_build_message_event`, which runs only after the gates), a little later. An early start whose message Hermes then doesn't answer is logged (`made LEON-… but Hermes didn't answer that message`).

A thread reply's thread goes along: Hermes keeps the thread history it read, and slack22linear quotes the thread's first message in the issue. A message in a project's channel files the issue in that project.

## Failure

When AutoLinear declines intake, fails or takes longer than 25 seconds, Hermes handles the request in its original conversation. Intake retries once for a network error or 5xx and deduplicates requests by Slack message.

The copy carries slack-as-leon's `client_msg_id` marker, so [slack-as-leon-guard](https://github.com/leonsilicon/leonsilicon.ai/blob/main/apps/hermes-plugin-leon/slack_as_leon_guard/README.md) drops its live event, and Hermes doesn't answer the message twice. Keep that plugin enabled; this one logs a warning when it isn't. The copy's ts is also claimed, so its unfurl edits start no turn. autolinear's edit of Leon's message starts none either: Hermes claimed that message's ts before its turn was built.

## Settings

In `$HERMES_HOME/.env` (the dashboard's Keys page), from the `#hermes` items of the root [`.env.schema`](https://github.com/leonsilicon/leonsilicon.ai/blob/main/.env.schema). Without the first two, the plugin is off.

| Key                         | Value                                                                                                                                                                                                              |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `AUTOLINEAR_TOKEN`          | autolinear's `AUTOLINEAR_TOKEN`                                                                                                                                                                                    |
| `AUTOLINEAR_USER_ID`        | Leon's Slack user ID                                                                                                                                                                                               |
| `AUTOLINEAR_URL`            | Optional; default `https://autolinear.leonsilicon.ai/intake`                                                                                                                                                       |
| `AUTOLINEAR_CHANNEL_PREFIX` | Optional; default `leon`, slack22linear's channel prefix                                                                                                                                                           |
| `AUTOLINEAR_SKIP_CHANNELS`  | Optional; comma-separated channel IDs whose messages Hermes answers there                                                                                                                                          |
| `AUTOLINEAR_BOT_USER_ID`    | Optional; Autolinear's bot user, whose mention starts Hermes's command line. Without it, the prompt asks for a plain `@Autolinear`. Also add it to `SLACK_ALLOWED_USERS`, or Hermes ignores Autolinear's reminders |

They're read on every message, so removing the token turns the plugin off once Hermes's env reloads.

When [hermes22muse](../hermes22muse/README.md) is active, replies in a thread that relays a Muse handoff stay in that thread. Keep the separate AutoLinear Slack app’s message event subscriptions paused so the gateway decides which messages to copy.

## Install

It comes with the [leon plugin](../README.md#install). After a restart the gateway log shows `autolinear: issue intake for Leon's Slack messages`. Apply the [agent routing policy](../hermes22muse/README.md#agent-routing) in the Hermes Cloud profile. The same status rules apply to normal turns and issue sweeps. A sweep originating in Linear can use a Linear comment with Leon's mention to trigger a turn.

## Test

The tests are `__tests__/test_autolinear.py` in the [leon plugin](../README.md#test), and need a Hermes checkout's environment. They drive the real Slack adapter and verify issue intake, reply routing, deduplication, and the status prompt.
