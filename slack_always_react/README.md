# slack-always-react

Part of the [leon plugin](../README.md), and a Hermes plugin of its own until 2026-11-26.

Hermes plugin that gives every Slack message Hermes processes the 👀 → ✅/❌ reaction lifecycle.

Stock Hermes (checked through upstream `main` on 2026-11-06) registers a Slack message for reactions only when it is a 1:1 DM or @mentions the bot (`plugins/platforms/slack/adapter.py`, `if (is_one_to_one_dm or is_mentioned) and self._reactions_enabled()`). With `slack.require_mention: false`, Hermes still answers unmentioned channel messages and thread replies, including replies that redirect a running turn, but none of them get 👀.

The plugin hooks `ctx.register_platform_handler("slack", …)`, which hands it the live adapter at connect. It wraps `_build_message_event`, which runs only after the auth, channel, mention and bot gates have accepted a message, and adds that message to the adapter's own reaction tracking. Synthetic reaction-triggered turns are skipped. `slack.reactions: false` still turns everything off. If the adapter internals change, it logs a warning and does nothing.

A Grok bot's post in a task thread (slack-as-leon's marker with second group `b075`, since 2026-11-21) is answered but gets no reactions: it is posted as Leon, and slack22linear reads Hermes's ✅ or ❌ in an issue channel as the end of a turn, which owes Autolinear a command (since 2026-11-23; before then, a ✅ on Leon's message could close the issue or hand it to him).

## Install

It comes with the [leon plugin](../README.md#install).

## Test

The tests, `__tests__/test_slack_always_react.py` in the [leon plugin](../README.md#test), run the real Slack adapter from a Hermes checkout.
