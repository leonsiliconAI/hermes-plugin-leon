"""Run against a Hermes checkout: ``HERMES_AGENT=~/.hermes/hermes-agent _test`` (see README)."""

from __future__ import annotations

import importlib.util
import sys
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SessionSource
from gateway.platforms.event import MessageType, ProcessingOutcome
from plugins.platforms.slack.adapter import SlackAdapter

_spec = importlib.util.spec_from_file_location(
    "slack_always_react", pathlib.Path(__file__).resolve().parents[1] / "__init__.py")
plugin = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = plugin
_spec.loader.exec_module(plugin)


@pytest.fixture()
def adapter(monkeypatch):
    monkeypatch.setenv("SLACK_REQUIRE_MENTION", "false")
    a = SlackAdapter(PlatformConfig(enabled=True, token="***"))
    a._app = MagicMock()
    a._app.client = AsyncMock()
    a._app.client.users_info = AsyncMock(
        return_value={"user": {"is_bot": False, "profile": {"display_name": "Leon"}}})
    a._app.client.reactions_add = AsyncMock()
    a._app.client.reactions_remove = AsyncMock()
    a._bot_user_id = "U_BOT"
    a._running = True
    a.handle_message = AsyncMock()
    return a


def _event(ts: str, **extra) -> dict:
    return {"text": "download the lecture notes", "user": "U_USER", "channel": "C_UNI",
            "channel_type": "channel", "ts": ts, **extra}


def _msg_event(ts: str) -> MessageEvent:
    source = SessionSource(platform=Platform.SLACK, chat_id="C_UNI", chat_type="group", user_id="U_USER")
    return MessageEvent(text="x", message_type=MessageType.TEXT, source=source, message_id=ts)


@pytest.mark.asyncio
async def test_stock_adapter_skips_unmentioned_channel_message(adapter):
    """Documents the upstream behavior this plugin changes."""
    await adapter._handle_slack_message(_event("1700000000.000001"))
    adapter.handle_message.assert_awaited_once()
    assert not adapter._reacting_message_ids


@pytest.mark.asyncio
async def test_unmentioned_channel_message_gets_eyes_then_check(adapter):
    plugin.wire(None, adapter)
    ts = "1700000000.000002"
    await adapter._handle_slack_message(_event(ts))
    adapter.handle_message.assert_awaited_once()

    await adapter.on_processing_start(_msg_event(ts))
    await adapter.on_processing_complete(_msg_event(ts), ProcessingOutcome.SUCCESS)

    added = [c.kwargs["name"] for c in adapter._app.client.reactions_add.call_args_list]
    removed = [c.kwargs["name"] for c in adapter._app.client.reactions_remove.call_args_list]
    assert added == ["eyes", "white_check_mark"]
    assert removed == ["eyes"]
    assert not adapter._reacting_message_ids


@pytest.mark.asyncio
async def test_unmentioned_thread_reply_is_tracked(adapter):
    plugin.wire(None, adapter)
    ts = "1700000000.000004"
    await adapter._handle_slack_message(_event(ts, thread_ts="1700000000.000003"))
    assert any(ts in marker for marker in map(str, adapter._reacting_message_ids))


@pytest.mark.asyncio
async def test_gated_message_is_not_tracked(adapter, monkeypatch):
    monkeypatch.setenv("SLACK_REQUIRE_MENTION", "true")
    plugin.wire(None, adapter)
    await adapter._handle_slack_message(_event("1700000000.000005"))
    adapter.handle_message.assert_not_awaited()
    assert not adapter._reacting_message_ids


@pytest.mark.asyncio
async def test_reactions_disabled_is_respected(adapter, monkeypatch):
    monkeypatch.setenv("SLACK_REACTIONS", "false")
    plugin.wire(None, adapter)
    await adapter._handle_slack_message(_event("1700000000.000006"))
    assert not adapter._reacting_message_ids


def test_wire_is_idempotent(adapter):
    plugin.wire(None, adapter)
    patched = adapter._build_message_event
    plugin.wire(None, adapter)
    assert adapter._build_message_event is patched


def test_missing_internals_fail_closed(caplog):
    class Bare:
        pass

    bare = Bare()
    plugin.wire(None, bare)
    assert not hasattr(bare, "_build_message_event")
    assert "internals changed" in caplog.text


@pytest.mark.asyncio
async def test_grok_bot_post_is_answered_without_reactions(adapter):
    """A Grok bot's post (slack-as-leon marker, group b075) is Leon's only in name: no 👀/✅."""
    plugin.wire(None, adapter)
    ts = "1700000000.000030"
    await adapter._handle_slack_message(_event(
        ts, thread_ts="1700000000.000003", client_msg_id="de1e6a7e-b075-45d1-94bb-f80359112347"))
    adapter.handle_message.assert_awaited_once()
    assert not any(ts in marker for marker in map(str, adapter._reacting_message_ids))


@pytest.mark.asyncio
async def test_codex_post_is_answered_without_reactions(adapter):
    plugin.wire(None, adapter)
    ts = "1700000000.000031"
    await adapter._handle_slack_message(_event(
        ts, thread_ts="1700000000.000003", client_msg_id="de1e6a7e-c0de-45d1-94bb-f80359112347"))
    adapter.handle_message.assert_awaited_once()
    assert not any(ts in marker for marker in map(str, adapter._reacting_message_ids))
