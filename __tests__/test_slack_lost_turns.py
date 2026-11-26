"""Run against a Hermes checkout with its environment (see README): it drives Hermes's real session
authority and its SQLite store, and imports Hermes's own test fixtures."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sqlite3
import sys
from contextlib import closing
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from gateway.session_authority import LiveSession, initialize_session_authority
from gateway.session_contract import SessionRef, Submission
from gateway.session_controls import AuthorityConnection
from tests.fixtures.storage_seed import seed_session

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _feature(folder: str):
    """A fresh copy of one of the leon plugin's features, imported under ``leon`` as Hermes does."""
    if "leon" not in sys.modules:
        root = importlib.util.spec_from_file_location("leon", _ROOT / "__init__.py")
        sys.modules["leon"] = importlib.util.module_from_spec(root)
        root.loader.exec_module(sys.modules["leon"])
    spec = importlib.util.spec_from_file_location(f"leon.{folder}", _ROOT / folder / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


feature = _feature("slack_lost_turns")


def _principal(platform: str, chat: str, user: str) -> str:
    """How Hermes names a messaging sender (``SessionAuthority._admit_native``)."""
    return "messaging:" + json.dumps(["p", platform, chat, None, user], separators=(",", ":"))


SLACK = _principal("slack", "C0ISSUE0001", "U0LEON00001")
TELEGRAM = _principal("telegram", "123", "202")


class Peer:
    def write(self, frame):
        return True


@pytest.fixture(autouse=True)
def settling(monkeypatch):
    """Hermes's settling module, restored after each test (``install`` replaces two functions)."""
    from gateway import session_unattended_unknown as module

    for name in ("settle_recovered_unattended", "settle_unattended_unknown"):
        monkeypatch.setattr(module, name, getattr(module, name))
    return module


async def _restart_after_lost_turn(tmp_path, monkeypatch, principal, *, install_before_boot=False):
    """A turn from ``principal`` started, then its owner died: the next owner finds it ``unknown``."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store.persistence
    executed = []

    async def answer(event):
        from gateway.session_results import execution_result

        executed.append(event.text)
        execution_result.get()["result"] = {"final_response": "ok", "completed": True, "messages": []}
        return "ok"

    runner = SimpleNamespace(_session_db=db, session_store=store, _draining=False,
                             _handle_message=answer, _adapter_for_source=lambda source: None)
    source = SessionSource(platform=Platform.SLACK, chat_id="C0ISSUE0001")
    seed_session(db, "s", "slack")
    first = await initialize_session_authority(runner, profile_id="p", instance_id="owner-1")
    first.sessions["s"] = LiveSession(source, "s")
    ref = SessionRef("p", "s")
    actor = AuthorityConnection(first, Peer(), {"user_id": "human"}).actor
    first._schedule = lambda ref: None
    head = await first.submit(actor, Submission("head", ref, {"text": "mark as done"}, "queue"))
    assert db.admissions.claim(epoch=first.epoch, session_id="s").admission_id == head.admission_id
    with closing(sqlite3.connect(tmp_path / "state.db")) as raw:
        raw.execute("UPDATE session_admissions SET principal_id = ? WHERE admission_id = ?",
                    (principal, head.admission_id))
        raw.commit()
    if install_before_boot:
        assert feature.install()
    authority = await initialize_session_authority(runner, profile_id="p", instance_id="owner-2")
    authority.sessions["s"] = LiveSession(source, "s")
    return SimpleNamespace(store=store, db=db, authority=authority, ref=ref, head=head.admission_id,
                           executed=executed,
                           actor=AuthorityConnection(authority, Peer(), {"user_id": "human"}).actor)


def _status(t, admission_id):
    row = t.db.admissions.get(admission_id=admission_id)
    return row.status.value, getattr(row.outcome, "value", row.outcome)


async def _next_message(t, text="mark as done"):
    """The next message in the conversation, and the queue that runs it."""
    receipt = await t.authority.submit(t.actor, Submission(text, t.ref, {"text": text}, "queue"))
    live = t.authority.sessions["s"]
    if live.task is not None:
        await asyncio.wait_for(live.task, 10)
    return receipt.admission_id


@pytest.mark.asyncio
async def test_stock_hermes_pauses_the_conversation_behind_a_lost_turn(tmp_path, monkeypatch):
    """Pins the upstream behaviour this feature changes (LEON-494, 2026-11-26)."""
    t = await _restart_after_lost_turn(tmp_path, monkeypatch, SLACK)
    try:
        assert _status(t, t.head)[0] == "unknown"
        follower = await _next_message(t)
        assert t.executed == [], "saved, not answered"
        assert _status(t, follower)[0] == "queued"
    finally:
        t.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_a_lost_slack_turn_settles_and_the_next_message_runs(tmp_path, monkeypatch):
    t = await _restart_after_lost_turn(tmp_path, monkeypatch, SLACK)
    try:
        assert feature.install()
        follower = await _next_message(t)
        assert t.executed == ["mark as done"], "the next message runs; the lost one is never replayed"
        assert _status(t, t.head) == ("terminal", "interrupted")
        assert _status(t, follower) == ("terminal", "completed")
    finally:
        t.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_a_lost_slack_turn_settles_at_boot_when_the_plugin_is_loaded_first(tmp_path, monkeypatch):
    t = await _restart_after_lost_turn(tmp_path, monkeypatch, SLACK, install_before_boot=True)
    try:
        assert _status(t, t.head) == ("terminal", "interrupted"), "before any queue runs"
        await _next_message(t)
        assert t.executed == ["mark as done"]
    finally:
        t.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_another_platforms_lost_turn_still_pauses(tmp_path, monkeypatch):
    t = await _restart_after_lost_turn(tmp_path, monkeypatch, TELEGRAM)
    try:
        assert feature.install()
        follower = await _next_message(t)
        assert t.executed == []
        assert _status(t, t.head)[0] == "unknown"
        assert _status(t, follower)[0] == "queued"
    finally:
        t.store.close_all_db_handles()


def test_installing_twice_wraps_hermes_once(settling):
    original = settling.settle_unattended_unknown
    assert feature.install() and feature.install()
    assert settling.settle_unattended_unknown.__wrapped__ is original


def test_only_slack_messages_count():
    assert feature.is_slack_message({"principal_id": SLACK})
    assert not feature.is_slack_message({"principal_id": TELEGRAM})
    assert not feature.is_slack_message({"principal_id": 'auth:v1:["","","human"]'})
    assert not feature.is_slack_message({"principal_id": "messaging:not json"})
    assert not feature.is_slack_message({})
