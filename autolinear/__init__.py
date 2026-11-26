"""Give each of Leon's Slack messages its own Linear issue and channel the moment it arrives.

Leon's rule (2026-11-21, narrowed 2026-11-26): every message he sends Hermes in his DM or a project
channel (``project-…``) becomes a new Linear issue in Awaiting Init (the team's default status) with its own channel, at once; his
message is edited to link the channel, and Hermes answers, and keeps the conversation going, there.

autolinear (autolinear.leonsilicon.ai) does the work: it creates the issue and the channel, edits
Leon's message, and posts the message again in the channel as Leon, marked like slack-as-leon's
posts, then answers with the channel and the copy's ts. The plugin wraps two seams of the live Slack adapter:

- ``_prefilter_inbound``: the moment one of Leon's messages has passed dedup and the bot check, it
  starts the request in the background, before Hermes reads the thread or downloads files. Only
  messages Hermes is sure to answer start this early: Leon's, in his DM or a project channel, not a
  command, mentioning no one but Hermes. Replies in task channels, and messages in any channel
  whose name Hermes couldn't read, stay where they are.
- ``_build_message_event``: runs only after every gate (auth, channel, mention, bot) has accepted a
  message, so it sees exactly the messages Hermes answers. It waits for the request started early,
  or starts it then, and builds the turn as if Leon had written the copy: same text, files and
  thread history, in the new channel.

Hermes decides how to handle the work in its own turn. In an issue channel, it ends ordinary turns with an explicit Autolinear command. Reports from delegated agents can be
handled silently and only get a command when the report calls for a status change.

The copy's own event carries slack-as-leon's marker, so ``slack-as-leon-guard`` drops it and Hermes
doesn't answer the message twice. That plugin must be enabled too.

When autolinear can't complete intake, a channel request can hand off in place. If the adapter
internals change, the plugin logs a warning and leaves the adapter alone.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .._bridge import acall, acall_with_host, call, on_host

logger = logging.getLogger(__name__)

_PATCHED_ATTR = "_autolinear_patched"
_PENDING_ATTR = "_autolinear_pending"
_CLAIMS_ATTR = "_autolinear_claims"
_warned_guard = False

#: Settings, from Hermes's environment ($HERMES_HOME/.env). Without the first two, the plugin is off.
TOKEN_ENV = "AUTOLINEAR_TOKEN"
USER_ENV = "AUTOLINEAR_USER_ID"  # Leon's Slack user ID
URL_ENV = "AUTOLINEAR_URL"
PREFIX_ENV = "AUTOLINEAR_CHANNEL_PREFIX"
SKIP_ENV = "AUTOLINEAR_SKIP_CHANNELS"
#: Autolinear's bot user, whose mention starts Hermes's command line. Without it, the prompt asks for a
#: plain "@Autolinear", which Autolinear reads too.
BOT_ENV = "AUTOLINEAR_BOT_USER_ID"

DEFAULT_URL = "https://autolinear.leonsilicon.ai/intake"
DEFAULT_PREFIX = "leon"
TIMEOUT_SECONDS = 20.0
RETRY_DELAY_SECONDS = 1.0
#: How long building a turn waits for its intake before answering in place.
WAIT_SECONDS = 25.0
REACTION_TIMEOUT_SECONDS = 2.0
#: How long an intake nobody waited for is kept, in case its turn is still being built.
KEEP_SECONDS = 120.0

# Thread bookkeeping that belongs to where the message was, not to its copy at the top of a channel.
_THREAD_KEYS = (
    "thread_ts", "parent_user_id", "reply_count", "reply_users", "reply_users_count",
    "latest_reply", "replies", "subscribed", "last_read", "edited", "_slack_changed_event_ts",
)


def _settings() -> Optional[dict[str, Any]]:
    """Read on every message, so a change to Hermes's env applies without rewiring."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    user = os.environ.get(USER_ENV, "").strip()
    if not token or not user:
        return None
    prefix = os.environ.get(PREFIX_ENV, "").strip() or DEFAULT_PREFIX
    return {
        "token": token,
        "user": user,
        "url": os.environ.get(URL_ENV, "").strip() or DEFAULT_URL,
        "prefix": prefix,
        "skip": {c.strip() for c in os.environ.get(SKIP_ENV, "").split(",") if c.strip()},
    }


def _post_json(url: str, token: str, payload: dict, timeout: float) -> tuple[int, dict]:
    """One POST. Returns the status and JSON body; raises on a network failure."""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            # Cloudflare refuses some default user agents on this zone.
            "user-agent": "hermes-autolinear/1.0",
        })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        body = error.read() or b""
        try:
            return error.code, json.loads(body)
        except ValueError:
            return error.code, {"error": body[:200].decode(errors="replace")}


async def _request_intake(settings: dict, payload: dict) -> Optional[dict]:
    """Asks autolinear for the issue. Retries once on a network error or a 5xx: an intake runs
    once per message, so a retry gets the same issue. None when it declines; raises on failure."""
    network_error = None

    async def post():
        nonlocal network_error
        try:
            status, body = await asyncio.to_thread(
                _post_json, settings["url"], settings["token"], payload, TIMEOUT_SECONDS)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            network_error = exc
            return {"status": 0}
        return {"status": status, "body": body}

    result = await acall(__file__, "requestIntake", {
        "post": on_host(post), "retry": on_host(lambda: asyncio.sleep(RETRY_DELAY_SECONDS)),
    })
    if result.get("networkError") and network_error is not None:
        raise network_error
    if "error" in result:
        raise RuntimeError(result["error"])
    if "accepted" in result:
        return result["accepted"]
    logger.info(
        "autolinear: left %s:%s alone (%s); answering it there",
        payload["channel"], payload["ts"], result.get("skipped"))
    return None


def _first_delivery(adapter, team_id: str, channel_id: str, ts: str) -> bool:
    """False for a second event of one of Leon's messages that Hermes accepted within KEEP_SECONDS.

    Slack sends a link's unfurl as a ``message_changed`` edit with an event ts of its own, a second
    or two after the message. Hermes drops the edit once it has claimed the message's ts, but it
    claims the ts only after its gates, which take longer than that while Hermes Cloud's CPU is
    throttled, or when a reconnect delivers both at once. Then both pass (LEON-540, 2026-11-26):
    they became two turns on one message id, Hermes refused the second, whose text had the unfurl
    in it, and its refusal took the place of the answer on the task card. Only messages Hermes is
    sure to answer are claimed, so an ignored message can't keep a later edit that mentions Hermes
    from being answered. The check and the claim run with no await between them, so of two
    concurrent events exactly one goes on."""
    claims = getattr(adapter, _CLAIMS_ATTR, None)
    if claims is None:
        claims = {}
        setattr(adapter, _CLAIMS_ATTR, claims)
    now = time.monotonic()
    for key, at in list(claims.items()):
        if now - at > KEEP_SECONDS:
            del claims[key]
    key = (team_id, channel_id, ts)
    if key in claims:
        return False
    claims[key] = now
    return True


def _pending(adapter) -> dict:
    pending = getattr(adapter, _PENDING_ATTR, None)
    if pending is None:
        pending = {}
        setattr(adapter, _PENDING_ATTR, pending)
    return pending


async def _react_during_intake(adapter, channel_id, ts, team_id, intake) -> None:
    """Show receipt on the source while the issue channel is being opened."""
    try:
        added = await asyncio.wait_for(
            adapter._add_reaction(channel_id, ts, "eyes", team_id), REACTION_TIMEOUT_SECONDS)
        if not added:
            return
        result = await asyncio.shield(intake)
        if result is not None:
            await asyncio.wait_for(
                adapter._remove_reaction(channel_id, ts, "eyes", team_id), REACTION_TIMEOUT_SECONDS)
    except Exception:
        logger.debug("autolinear: could not show intake receipt for %s:%s", channel_id, ts, exc_info=True)


async def _start(adapter, settings: dict, event: dict, channel_id: str, team_id: str,
                 receipt_tasks: set):
    """Starts the intake for a message in the background, unless it's in an issue channel."""
    pending = _pending(adapter)
    ts = str(event.get("ts") or "")
    key = (channel_id, ts)
    if key in pending:
        return pending[key]
    if channel_id in settings["skip"]:
        return None
    if _in_muse_thread(adapter, event, team_id, channel_id, ts):
        return None
    name = str(await adapter._resolve_channel_name(channel_id, team_id=team_id) or "")
    if not channel_id.startswith("D") and name in ("", channel_id):
        # Hermes caches the channel's ID as its name when Slack's lookup fails (timeouts while
        # Hermes Cloud's CPU is starved, 2026-11-26), so this channel could well be a task channel.
        _forget_channel_name(adapter, team_id, channel_id)
        logger.warning("autolinear: no name for %s; answering %s there", channel_id, ts)
        return None
    if not call(__file__, "intakeChannel", channel_id, name):
        return None
    global _warned_guard
    if not getattr(adapter, "_slack_as_leon_guard_patched", False) and not _warned_guard:
        _warned_guard = True
        logger.warning(
            "autolinear: slack-as-leon-guard isn't wired, so Hermes may answer the copy of Leon's "
            "message a second time; enable that plugin")
    payload = call(__file__, "intakePayload", event, channel_id, ts, name)
    task = asyncio.ensure_future(_request_intake(settings, payload))
    pending[key] = task
    if call(__file__, "shouldReact", event, ts) and adapter._reactions_enabled():
        receipt = asyncio.create_task(_react_during_intake(adapter, channel_id, ts, team_id, task))
        receipt_tasks.add(receipt)
        receipt.add_done_callback(receipt_tasks.discard)

    def forget(_task) -> None:
        # Kept a while for a turn still being built, then dropped; one nobody took is logged.
        def drop() -> None:
            if pending.pop(key, None) is not None and not _task.cancelled():
                result = _task.exception() is None and _task.result()
                if result:
                    logger.warning(
                        "autolinear: made %s for %s:%s, but Hermes didn't answer that message",
                        result["issue"]["identifier"], channel_id, ts)
        asyncio.get_running_loop().call_later(KEEP_SECONDS, drop)

    task.add_done_callback(forget)
    return task


def _forget_channel_name(adapter, team_id: str, channel_id: str) -> None:
    """Drop the ID Hermes cached as the channel's name, so its next lookup asks Slack again."""
    cache = getattr(adapter, "_channel_name_cache", None)
    if not isinstance(cache, dict):
        return
    teams = {str(team_id or ""), str(getattr(adapter, "_channel_team", {}).get(channel_id, "") or "")}
    for team in teams:
        if cache.get((team, channel_id)) in ("", channel_id):
            cache.pop((team, channel_id), None)


def _in_muse_thread(adapter, event: dict, team_id: str, channel_id: str, ts: str) -> bool:
    """A reply in a thread that relays a hermes22muse handoff stays there, so Hermes passes it on."""
    thread = str(event.get("thread_ts") or "")
    handoff_thread = getattr(adapter, "_hermes22muse_handoff_thread", None)
    if not thread or thread == ts or not callable(handoff_thread):
        return False
    try:
        return bool(handoff_thread(team_id, channel_id, thread))
    except Exception:
        logger.warning("autolinear: could not check hermes22muse's thread %s:%s", channel_id, thread,
                       exc_info=True)
        return True


def _answers_for_sure(adapter, settings: dict, event: dict, team_id: str) -> bool:
    """True for a message Hermes always answers: worth starting on before its gates have run."""
    bot = getattr(adapter, "_team_bot_user_ids", {}).get(team_id) or getattr(
        adapter, "_bot_user_id", None)
    return call(__file__, "answersForSure", event, settings["user"], bot)


def _note(result: dict) -> str:
    return call(__file__, "issueNote", result)


def _add_task_prompt(msg_event, *, in_issue_channel: bool, event: dict) -> None:
    result = call(__file__, "applyPrompt", msg_event.channel_prompt, in_issue_channel,
                  event, os.environ.get(BOT_ENV, "").strip())
    if result["silent"]:
        msg_event.reply_expected = False
    msg_event.channel_prompt = result["prompt"]


def _remember(adapter, team_id: str, channel_id: str, name: str) -> None:
    """Warm the adapter's caches for the new channel, so building the turn needs no lookups."""
    try:
        if team_id:
            adapter._remember_channel_team(channel_id, team_id)
        cache = getattr(adapter, "_channel_name_cache", None)
        if isinstance(cache, dict) and name:
            cache[(str(team_id or ""), channel_id)] = name
    except Exception:
        logger.debug("autolinear: could not warm caches for %s", channel_id, exc_info=True)


async def _move(adapter, settings: dict, event: dict, kwargs: dict, receipt_tasks: set):
    """``(event, kwargs)`` for the copy in the issue's channel, or None to answer in place."""
    channel_id = str(kwargs.get("channel_id") or "")
    ts = str(kwargs.get("ts") or "")
    team_id = str(kwargs.get("team_id") or "")
    if not channel_id or not ts:
        return None
    task = _pending(adapter).get((channel_id, ts)) or await _start(
        adapter, settings, event, channel_id, team_id, receipt_tasks)
    if task is None:
        return None
    result = await asyncio.wait_for(asyncio.shield(task), WAIT_SECONDS)
    _pending(adapter).pop((channel_id, ts), None)
    if result is None:
        return None
    new_channel, copy_ts = result["channel"]["id"], result["ts"]
    _remember(adapter, team_id, new_channel, result["channel"].get("name", ""))
    moved = call(__file__, "copiedMessage", event, result, channel_id, ts, list(_THREAD_KEYS))
    moved_kwargs = dict(kwargs)
    moved_kwargs.update(
        channel_id=new_channel, ts=copy_ts, is_dm=False,
        thread_ts=adapter._session_thread_ts(moved, copy_ts, False, {}),
        # Leon wrote it to Hermes, wherever he wrote it.
        reply_expected=True,
        text=f"{_note(result)}\n\n{kwargs.get('text') or ''}".rstrip())
    logger.info(
        "autolinear: %s:%s is %s (%s); Hermes answers in #%s", channel_id, ts,
        result["issue"]["identifier"], result.get("ms"), result["channel"].get("name", new_channel))
    return moved, moved_kwargs


def wire(native, adapter) -> None:
    """``register_platform_handler`` factory: patch this adapter instance once."""
    if getattr(adapter, _PATCHED_ATTR, False):
        return
    build = getattr(adapter, "_build_message_event", None)
    prefilter = getattr(adapter, "_prefilter_inbound", None)
    seams = (build, prefilter, getattr(adapter, "_resolve_channel_name", None),
             getattr(adapter, "_session_thread_ts", None),
             getattr(adapter, "_track_reacting_message", None),
             getattr(adapter, "_reactions_enabled", None), getattr(adapter, "_add_reaction", None),
             getattr(adapter, "_remove_reaction", None))
    if not all(callable(seam) for seam in seams):
        logger.warning(
            "autolinear: Slack adapter internals changed; Hermes answers Leon's messages where he "
            "sends them")
        return
    tasks: set[asyncio.Task] = set()

    @functools.wraps(prefilter)
    async def prefilter_and_start(event, *args, **kwargs):
        accepted = await prefilter(event, *args, **kwargs)
        settings = _settings()
        if accepted is None or settings is None:
            return accepted
        try:
            normalized, team_id, channel_id = accepted
            if not isinstance(normalized, dict):
                return accepted
            team, ts = str(team_id or ""), str(normalized.get("ts") or "")
            edit = bool(normalized.get("_slack_changed_event_ts"))
            # An edit is judged as the message it changes: an unfurl leaves Leon's text as it was.
            sure = _answers_for_sure(adapter, settings, {
                key: value for key, value in normalized.items() if key != "_slack_changed_event_ts"
            }, team)
            if sure and ts and not _first_delivery(adapter, team, str(channel_id or ""), ts):
                logger.info("autolinear: %s:%s came in twice at once (with its unfurl edit); "
                            "Hermes answers it once", channel_id, ts)
                return None
            if sure and not edit:
                await _start(adapter, settings, normalized, str(channel_id or ""), team, tasks)
        except Exception:
            logger.warning("autolinear: could not start early", exc_info=True)
        return accepted

    @functools.wraps(build)
    async def build_in_issue_channel(event, *args, **kwargs):
        settings = _settings()
        target = msg_event = None
        sender = kwargs.get("user_id")
        async def move():
            nonlocal target
            try:
                target = await _move(adapter, settings, event, kwargs, tasks)
            except Exception:
                logger.warning("autolinear: no issue for %s:%s; answering it there",
                               kwargs.get("channel_id"), kwargs.get("ts"), exc_info=True)
            return target is not None

        async def build_variant(mode):
            nonlocal msg_event
            moved = mode == "moved"
            message, options = target if moved else (event, kwargs)
            try:
                msg_event = await build(message, *args, **options)
            except Exception:
                if not moved:
                    raise
                logger.warning("autolinear: could not build the turn in %s; answering in place",
                               options["channel_id"], exc_info=True)
                return False
            if msg_event is not None and not options.get("is_command_text"):
                in_issue = moved or (settings is not None and call(
                    __file__, "issueChannel", msg_event.source.chat_name or "", settings["prefix"],
                ))
                _add_task_prompt(msg_event, in_issue_channel=in_issue, event=event)
            if moved:
                copy_ts, team_id = options["ts"], str(options.get("team_id") or "")
                try:
                    # Edits keep the native reaction lifecycle. Mark the copy before an unfurl edit.
                    if msg_event is not None and adapter._reactions_enabled():
                        adapter._track_reacting_message(team_id, copy_ts)
                    adapter._remember_processed_message_ts(copy_ts)
                except Exception:
                    logger.warning("autolinear: could not track %s", copy_ts, exc_info=True)
            return True

        await acall_with_host(__file__, "buildReply", {"move": move, "build": build_variant},
            event if isinstance(event, dict) else None, sender, settings["user"] if settings else None,
            kwargs.get("is_command_text"))
        return msg_event

    adapter._prefilter_inbound = prefilter_and_start
    adapter._build_message_event = build_in_issue_channel
    setattr(adapter, _PATCHED_ATTR, True)
    logger.info(
        "autolinear: issue intake for Leon's Slack messages%s",
        "" if _settings() else f" (off until {TOKEN_ENV} and {USER_ENV} are set)")


def register(ctx) -> None:
    ctx.register_platform_handler("slack", wire)
