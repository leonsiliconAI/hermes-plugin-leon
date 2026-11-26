"""Keep a Slack conversation going after Hermes restarts in the middle of one of its turns.

Hermes fences a turn its gateway was running when it died as ``unknown`` and pauses that
conversation's queue behind it, because only the person who sent it knows whether to send it again
(#134889 GW-10). Every later message in the conversation is saved but not answered, and the first
gets "⏸️ This conversation is paused … Send /hermes discard …". Automation turns (cron, webhooks)
have nobody to ask, so Hermes settles those itself as interrupted, never replayed
(``gateway/session_unattended_unknown.py``).

In Leon's Slack this pause stranded tasks: on 2026-11-26 the gateway died six times, and LEON-494's
"mark as done" was answered with the pause notice hours later. Nobody types ``/hermes discard``, and
slack22linear already follows up on a turn that never ended (Autolinear's reminder, then Executing's
lease, which hands the issue to Leon). So a lost turn from Slack settles like an automation turn: as
interrupted, never replayed, with a notice in the transcript that tells the next turn what happened.
The next message in the conversation then runs, with the lost request still in its history.

It wraps Hermes's two settling points, ``settle_recovered_unattended`` (boot recovery, before any
queue runs) and ``settle_unattended_unknown`` (a queue that finds a lost turn at its head, which is
what the next message does). Both are imported where they are called, so the wrapped module functions
are the ones Hermes calls. It decides in Python rather than through the policy bridge: boot recovery
runs on a worker thread before the gateway is up. If the seams are missing, it logs a warning and
Hermes pauses as before.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging


logger = logging.getLogger(__name__)

_WRAPPED_ATTR = "_slack_lost_turns_wrapped"
NOTICE = (
    "Hermes restarted while this turn was running, so it stopped before it finished and was not run "
    "again. Some of its actions may already have happened: check what was done before repeating "
    "anything, then finish what is still open."
)


def is_slack_message(row) -> bool:
    """A message from Slack: its principal is ``messaging:`` + ``[profile, platform, chat, thread, user]``."""
    principal = (row or {}).get("principal_id") or ""
    if not principal.startswith("messaging:"):
        return False
    try:
        identity = json.loads(principal[len("messaging:"):])
    except ValueError:
        return False
    return isinstance(identity, list) and len(identity) >= 2 and identity[1] == "slack"


def lost_slack_turns(records) -> list[dict]:
    """The lost (``unknown``) Slack turns among admission records, as Hermes's row dicts."""
    from gateway.session_authority import admission_row

    return [row for row in map(admission_row, records)
            if row is not None and row["status"] == "unknown" and is_slack_message(row)]


def _settled_at_boot(original):
    @functools.wraps(original)
    def settle_recovered(store, epoch):
        settled = original(store, epoch)
        try:
            from gateway.session_ingress_media import release_admission_media
            from persistence.core.errors import AuthorityRefusal

            for row in lost_slack_turns(store.admissions.list_unknown()):
                try:
                    store.admissions.resolve_unknown(epoch=epoch, admission_id=row["admission_id"],
                                                     generation=row["generation"], notice=NOTICE)
                except AuthorityRefusal as exc:
                    logger.warning("slack-lost-turns: lost Slack turn %s left paused: %s",
                                   row["admission_id"], exc.reason)
                    continue
                release_admission_media(store, row["admission_id"])
                settled += 1
                logger.info("slack-lost-turns: lost Slack turn %s on session %s settled at boot "
                            "(not replayed)", row["admission_id"], row["target_session_id"])
        except Exception:
            logger.warning("slack-lost-turns: could not settle lost Slack turns at boot", exc_info=True)
        return settled

    setattr(settle_recovered, _WRAPPED_ATTR, True)
    return settle_recovered


def _settled_in_queue(original):
    @functools.wraps(original)
    async def settle_unattended(authority, ref):
        settled = await original(authority, ref)
        try:
            from gateway.session_persistence import authority_persistence
            from persistence.core.errors import AuthorityRefusal

            rows = await asyncio.to_thread(authority_persistence(authority).admissions.list,
                                           session_id=ref.session_id)
            for row in lost_slack_turns(rows):
                try:
                    await authority.resolve_unknown_as_owner(ref, row["admission_id"], row["generation"],
                                                             notice=NOTICE)
                except AuthorityRefusal as exc:
                    logger.warning("slack-lost-turns: lost Slack turn %s left paused: %s",
                                   row["admission_id"], exc.reason)
                    continue
                settled = True
                logger.info("slack-lost-turns: lost Slack turn %s on session %s settled (not replayed); "
                            "the conversation goes on", row["admission_id"], ref.session_id)
        except Exception:
            logger.warning("slack-lost-turns: could not settle lost Slack turns on session %s",
                           getattr(ref, "session_id", "?"), exc_info=True)
        return settled

    setattr(settle_unattended, _WRAPPED_ATTR, True)
    return settle_unattended


def _wrap(module, name: str, wrapper) -> bool:
    current = getattr(module, name, None)
    if not callable(current):
        return False
    if getattr(current, _WRAPPED_ATTR, False):  # a reload wraps Hermes's own function again
        current = current.__wrapped__
    setattr(module, name, wrapper(current))
    return True


def install() -> bool:
    """Wrap Hermes's settling points. False, with a warning, when they aren't there."""
    try:
        from gateway import session_unattended_unknown as settling
    except Exception:
        logger.warning("slack-lost-turns: Hermes has no session_unattended_unknown; a lost Slack turn "
                       "still pauses its conversation", exc_info=True)
        return False
    seams = ("settle_recovered_unattended", "settle_unattended_unknown")
    if not all(callable(getattr(settling, name, None)) for name in seams):
        logger.warning("slack-lost-turns: Hermes's settling points changed; a lost Slack turn still "
                       "pauses its conversation")
        return False
    _wrap(settling, "settle_recovered_unattended", _settled_at_boot)
    _wrap(settling, "settle_unattended_unknown", _settled_in_queue)
    logger.info("slack-lost-turns: a Slack turn lost in a restart settles as interrupted; its "
                "conversation goes on")
    return True


def register(ctx) -> None:
    install()
