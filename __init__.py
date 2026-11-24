"""Give every Slack message Hermes processes the 👀 → ✅/❌ reaction lifecycle.

Hermes's Slack adapter registers a message for reactions only when it is a 1:1 DM or @mentions
the bot (``SlackAdapter._handle_slack_message_impl``: ``if (is_one_to_one_dm or is_mentioned)``).
With ``slack.require_mention: false`` Hermes answers unmentioned channel and thread messages too,
but they never get 👀. This plugin wraps ``_build_message_event`` on the live adapter, which runs
only after every gate (auth, channel, mention, bot) has accepted the message, and registers the
message for the adapter's own reaction lifecycle. The adapter then adds 👀 when processing starts
(including queued and redirected follow-ups) and swaps it for ✅ or ❌ when processing ends.

An agent's post in a task thread (slack-as-leon's marker with second group ``b075`` for a Grok bot or
``c0de`` for a Codex session; see the ``slack-as-leon-guard`` plugin) gets no reactions: it is posted as Leon, and slack22linear reads
Hermes's ✅ on Leon's message in an issue channel as "Hermes answered Leon", which can close the
issue or ping him. Only Leon's own messages should do that.

It lives in ``$HERMES_HOME/plugins`` so it survives Hermes Cloud restarts and image updates, and it
fails closed: if the adapter internals it relies on disappear, it logs a warning and does nothing.
"""

from __future__ import annotations

import functools
import logging

logger = logging.getLogger(__name__)

_PATCHED_ATTR = "_slack_always_react_patched"

#: client_msg_id starts on an agent's post through slack-as-leon: a Grok bot's, a Codex session's.
AGENT_POSTS = ("de1e6a7e-b075-", "de1e6a7e-c0de-")


def _is_agent_post(event: dict) -> bool:
    value = event.get("client_msg_id")
    if not value and isinstance(event.get("message"), dict):  # message_changed envelope
        value = event["message"].get("client_msg_id")
    return isinstance(value, str) and value.startswith(AGENT_POSTS)


def _is_real_message(event: dict) -> bool:
    """Synthetic reaction-triggered turns carry the reaction's ts, not a message ts; a Grok bot's
    post is Leon's only in name."""
    return not (event.get("_hermes_reaction") or event.get("_hermes_force_process")
                or _is_agent_post(event))


def wire(native, adapter) -> None:
    """``register_platform_handler`` factory: patch this adapter instance once."""
    if getattr(adapter, _PATCHED_ATTR, False):
        return
    build = getattr(adapter, "_build_message_event", None)
    track = getattr(adapter, "_track_reacting_message", None)
    enabled = getattr(adapter, "_reactions_enabled", None)
    if not (callable(build) and callable(track) and callable(enabled)):
        logger.warning(
            "slack-always-react: Slack adapter internals changed; 👀 stays limited to DMs and mentions")
        return

    @functools.wraps(build)
    async def build_and_track(event, *args, **kwargs):
        msg_event = await build(event, *args, **kwargs)
        try:
            ts = kwargs.get("ts")
            if msg_event is not None and ts and enabled() and _is_real_message(event or {}):
                track(str(kwargs.get("team_id") or ""), ts)
        except Exception:
            logger.warning("slack-always-react: could not track ts=%s", kwargs.get("ts"), exc_info=True)
        return msg_event

    adapter._build_message_event = build_and_track
    setattr(adapter, _PATCHED_ATTR, True)
    logger.info("slack-always-react: 👀 enabled for every Slack message Hermes processes")


def register(ctx) -> None:
    ctx.register_platform_handler("slack", wire)
