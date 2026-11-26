# slack-lost-turns

Part of the [leon plugin](../README.md), since 2026-11-26.

Hermes plugin: when Hermes restarts in the middle of a turn on a Slack message, the conversation goes on. The lost turn is settled as interrupted, never run again, and the next message in the conversation runs as usual.

## Why

Hermes fences a turn whose gateway died under it as `unknown` and pauses its conversation behind it, because only the person who sent it knows whether to send it again. Every later message there is saved but not answered, and the first one gets "⏸️ This conversation is paused: a previous turn did not finish when Hermes restarted … Send /hermes discard to drop the lost turn and continue". Hermes settles automation turns (cron, webhooks) itself, since there's nobody to ask.

In Leon's Slack that pause stranded tasks. On 2026-11-26 the gateway died six times. LEON-494's "mark as done" at 10:08 ET died in the 10:14 crash, and Leon's "mark as done" at 1:54 PM got the pause notice instead of an answer. Nobody types `/hermes discard`, and [slack22linear](../../../packages/ai/leonsilicon/services/slack22linear/README.md) already follows up on a turn that never ended: Autolinear reminds Hermes to pick it up from where it stopped, and Executing's lease hands the issue to Leon.

## What it does

A lost turn from Slack (its sender is `messaging:` with platform `slack`) settles the way Hermes settles a lost automation turn: terminal `interrupted`, never replayed, with a note in the transcript, so the next turn knows. The note says the turn stopped in a restart, that some of its actions may already have happened, and to check what was done before repeating anything.

It wraps Hermes's two settling points in `gateway/session_unattended_unknown.py`:

- `settle_recovered_unattended`, at boot, before any conversation runs (when Hermes has loaded plugins by then);
- `settle_unattended_unknown`, when a conversation's queue finds a lost turn at its head, which is what the next message, or Autolinear's reminder, does.

Turns from other platforms still pause. It decides in Python, not through the policy bridge, because boot recovery runs on a worker thread before the gateway is up. If Hermes's settling points change, it logs a warning and Hermes pauses as before.

## Install

It comes with the [leon plugin](../README.md#install). The gateway log shows `slack-lost-turns: a Slack turn lost in a restart settles as interrupted; its conversation goes on`, and each settled turn logs `slack-lost-turns: lost Slack turn … settled`.

## Test

The tests, `__tests__/test_slack_lost_turns.py` in the [leon plugin](../README.md#test), drive Hermes's real session authority over SQLite and import Hermes's own test fixtures, so they need a Hermes checkout's environment. One pins the stock pause; the others settle a lost Slack turn in the queue and at boot, and leave another platform's turn paused.
