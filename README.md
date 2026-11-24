# @-/com.leonsilicon.hermes.plugins.slack-always-react

Hermes plugin that gives every Slack message Hermes processes the 👀 → ✅/❌ reaction lifecycle.

Stock Hermes (checked through upstream `main` on 2026-11-06) registers a Slack message for reactions only when it is a 1:1 DM or @mentions the bot (`plugins/platforms/slack/adapter.py`, `if (is_one_to_one_dm or is_mentioned) and self._reactions_enabled()`). With `slack.require_mention: false`, Hermes still answers unmentioned channel messages and thread replies, including replies that redirect a running turn, but none of them get 👀.

The plugin hooks `ctx.register_platform_handler("slack", …)`, which hands it the live adapter at connect. It wraps `_build_message_event`, which runs only after the auth, channel, mention and bot gates have accepted a message, and adds that message to the adapter's own reaction tracking. Synthetic reaction-triggered turns are skipped. `slack.reactions: false` still turns everything off. If the adapter internals change, it logs a warning and does nothing.

A Grok bot's post in a task thread (slack-as-leon's marker with second group `b075`, since 2026-11-21) is answered but gets no reactions: it is posted as Leon, and slack22linear reads Hermes's ✅ or ❌ in an issue channel as the end of a turn, which owes Autolinear a command (since 2026-11-23; before then, a ✅ on Leon's message could close the issue or hand it to him).

## Install

```sh
hermes plugins install leonsiliconAI/slack-always-react --enable
```

See [migration steps](https://github.com/leonsilicon/leonsilicon.ai/blob/main/DEVELOPMENT.md#hermes-plugin-mirrors) for an existing install. Restart the gateway.

## Test

The tests run the real Slack adapter from a Hermes checkout. They need `pytest` and `pytest-asyncio` alongside Hermes's own environment:

```sh
uv venv /tmp/sar --python ~/.hermes/tools/python-*/bin/python3
VIRTUAL_ENV=/tmp/sar uv pip install pytest pytest-asyncio
# Put the checkout and its venv site-packages on the path:
printf '%s\n' ~/.hermes/hermes-agent <hermes venv site-packages> > /tmp/sar/lib/python3.14/site-packages/hermes.pth
HERMES_HOME=$(mktemp -d) /tmp/sar/bin/python -m pytest -q __tests__
```
