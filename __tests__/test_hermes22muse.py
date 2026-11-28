import asyncio
from contextlib import asynccontextmanager, suppress
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call as mock_call, patch

_ROOT = Path(__file__).resolve().parents[1]


def _feature(folder: str, name: str | None = None):
    """A fresh copy of one of the leon plugin's features, imported under ``leon`` as Hermes does."""
    if "leon" not in sys.modules:
        root = importlib.util.spec_from_file_location("leon", _ROOT / "__init__.py")
        sys.modules["leon"] = importlib.util.module_from_spec(root)
        root.loader.exec_module(sys.modules["leon"])
    spec = importlib.util.spec_from_file_location(f"leon.{name or folder}", _ROOT / folder / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _feature("hermes22muse")

from leon.hermes22muse.muse import Message, MuseChat, MuseError, STAMP, history_messages
from leon.hermes22muse import museweb
from leon.hermes22muse import relay as relay_module
from leon.hermes22muse.relay import Relay, Slack, Store, operation_id
from leon.hermes22muse.schedules import TRIGGER_PROMPT, schedule_request
from leon.hermes22muse.turns import STALE, Turns

TEAM, CHANNEL, OWNER = "T001", "C001", "U001"
BOT_TOKEN = "xoxb-muse-test"
REQUEST, LATER = "1795000000.000001", "1795000002.000001"
JOB = operation_id(TEAM, CHANNEL, REQUEST)


class FakeMuse:
    def __init__(self):
        self.chats = {}
        self.registrations = []
        self.sends = []
        self.lose_receipt = False
        self.busy = False
        # muse-mcp prefixes sends with its provenance stamp unless the server strips it.
        self.stamp = STAMP
        self.approvals = 0
        self.approval_checks = 0

    @asynccontextmanager
    async def connect(self):
        yield self

    async def ensure_registered(self, chat_id):
        self.registrations.append(chat_id)

    async def exists(self, chat_id):
        return chat_id in self.chats

    async def send(self, chat_id, text):
        self.sends.append((chat_id, text))
        message_id = f"muse-{len(self.sends)}"
        self.chats.setdefault(chat_id, []).append(Message(message_id, "user", self.stamp + text))
        if self.lose_receipt:
            self.lose_receipt = False
            raise TimeoutError
        return message_id

    async def history(self, chat_id):
        return self.chats[chat_id], False, self.busy

    async def pending_approvals(self, chat_id):
        self.approval_checks += 1
        return self.approvals


def fake_adapter():
    posts = iter(range(1, 1000))
    client = SimpleNamespace(chat_postMessage=AsyncMock(
        side_effect=lambda **kw: {"ok": True, "ts": f"1795000100.{next(posts):06d}", "channel": kw["channel"]},
    ), chat_update=AsyncMock(side_effect=lambda **kw: {
        "ok": True, "ts": kw["ts"], "channel": kw["channel"],
    }))
    sent = []

    async def send(chat_id, content, reply_to=None, metadata=None):
        """What reaches Hermes's own Slack send, past the plugin."""
        sent.append((chat_id, content, reply_to, metadata))
        return SendResult(success=True, message_id=f"1795000300.{len(sent):06d}")

    async def send_final_ledgered(event, session_key, text, metadata, *, reply_to, is_ephemeral_response=False):
        # The gateway's ledger sends through whatever adapter.send is now, the plugin's wrapper included.
        return await adapter.send(CHANNEL, text, reply_to=reply_to, metadata=metadata), adapter

    prompts = []

    async def post_interactive_blocks(chat_id, text, blocks, metadata, **kwargs):
        """What reaches the Slack adapter's poster of approval and question cards."""
        prompts.append((chat_id, text, blocks, metadata, kwargs))
        return {"ok": True, "ts": f"1795000400.{len(prompts):06d}"}

    adapter = SimpleNamespace(
        _outbound_blocked=lambda channel, action: False,
        _get_client=lambda channel, team_id: client,
        _team_clients={TEAM: client},
        _running=False,
        disconnect=AsyncMock(),
        send=send,
        send_final_ledgered=send_final_ledgered,
        format_message=lambda text: text,
        sent=sent,
        _post_interactive_blocks=post_interactive_blocks,
        prompts=prompts,
    )
    return adapter, client


class SendResult(SimpleNamespace):
    def __init__(self, success, message_id=None, error=None):
        super().__init__(success=success, message_id=message_id, error=error)


# The one gateway import the plugin makes when it wires delivery.
GATEWAY = {"gateway": SimpleNamespace(), "gateway.platforms": SimpleNamespace(),
           "gateway.platforms.base": SimpleNamespace(SendResult=SendResult)}


def posts(client):
    return [(call.kwargs.get("thread_ts"), call.kwargs["text"]) for call in client.chat_postMessage.await_args_list]


class RelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = Store(self.directory)
        self.adapter, self.client = fake_adapter()
        self.muse = FakeMuse()
        _, self.muse_client = fake_adapter()
        self.muse_client.auth_test = AsyncMock(return_value={
            "ok": True, "team_id": TEAM, "user_id": "UMUSE", "bot_id": "BMUSE",
        })
        self.slack = Slack(self.adapter, BOT_TOKEN)
        self.slack._new_muse_client = lambda: self.muse_client
        self.relay = Relay(self.store, self.slack, self.muse, OWNER)

    def create(self, channel=CHANNEL):
        return self.store.create(TEAM, channel, REQUEST, REQUEST, OWNER, "Task:\nFind a recipe",
                                 "Everything sent to Muse:\n\nTask:\nFind a recipe")

    async def tick(self):
        with self.store.connection() as db:
            db.execute("UPDATE handoffs SET due=0")
        await self.relay.tick(self.store.due(OWNER)[0])

    def settle(self):
        with self.store.connection() as db:
            db.execute(f"UPDATE handoffs SET changed=changed-{relay_module.QUIET}")

    async def test_thread_under_request_holds_task_answer_and_follow_up(self):
        self.assertEqual(self.create(), JOB)
        self.assertIsNone(self.create(), "a second handoff for the same message")
        self.adapter._register_mentioned_thread = register = Mock()
        await self.tick()
        self.assertEqual(posts(self.client), [(REQUEST, "Everything sent to Muse:\n\nTask:\nFind a recipe")])
        self.assertEqual(register.call_args_list, [mock_call(REQUEST, TEAM)])
        self.assertEqual(self.muse.registrations, [JOB])
        self.assertTrue(self.muse.sends[0][1].endswith("Task:\nFind a recipe"))
        self.assertEqual(self.store.answer(JOB), ("working", []))

        self.muse.chats[JOB].append(Message("answer", "assistant", "Do you want soup?"))
        self.muse.busy = True
        await self.tick()
        self.settle()
        self.assertEqual(self.store.answer(JOB)[0], "working", "Muse is still writing")
        self.muse.busy = False
        await self.tick()
        self.assertEqual(self.store.answer(JOB)[0], "working", "not quiet yet")
        self.settle()
        await self.tick()
        self.assertEqual(self.store.answer(JOB), ("answered", [{"from": "muse", "text": "Do you want soup?"}]))

        # A restarted gateway keeps the handoff; Hermes follows up and Leon also writes in Muse.
        self.store = Store(self.directory)
        self.relay = Relay(self.store, self.slack, self.muse, OWNER)
        self.store.follow_up(JOB, "Vegetarian, please", "Hermes → Muse\nVegetarian, please")
        await self.tick()
        self.assertEqual(self.muse.sends[1][1], "Vegetarian, please")
        self.assertEqual(self.store.answer(JOB), ("working", []), "the earlier answer no longer counts")
        self.muse.chats[JOB] += [Message("direct", "user", "No mushrooms"), Message("final", "assistant", "Lentil soup")]
        await self.tick()
        self.settle()
        await self.tick()
        self.assertEqual(self.store.answer(JOB), ("answered", [
            {"from": "leon_in_muse", "text": "No mushrooms"}, {"from": "muse", "text": "Lentil soup"},
        ]))
        self.assertEqual(posts(self.client)[1:], [
            (REQUEST, "Hermes → Muse\nVegetarian, please"), (REQUEST, "You in Muse\nNo mushrooms"),
        ])
        self.assertEqual(posts(self.muse_client), [
            (REQUEST, "Do you want soup?"), (REQUEST, "Lentil soup"),
        ])
        self.muse_client.auth_test.assert_awaited_once()
        event = SimpleNamespace(
            source=SimpleNamespace(platform="slack", scope_id=TEAM, user_id="UMUSE"), raw_message={},
        )
        self.assertTrue(self.slack.is_muse_event(event))
        event.source.user_id = OWNER
        self.assertFalse(self.slack.is_muse_event(event))
        self.assertIsNone(self.store.next_inbound(JOB))

    async def test_receipt_lost_after_muse_acceptance_recovers_in_same_chat(self):
        self.create()
        self.muse.lose_receipt = True
        await self.tick()
        await self.tick()
        self.assertIsNone(self.store.next_inbound(JOB))
        self.assertEqual(len(self.muse.sends), 1)

    async def test_a_reply_stored_cut_off_is_completed_and_posted_in_full(self):
        self.create()
        await self.tick()
        self.muse.chats[JOB].append(Message("answer", "assistant", "**Bottom line:"))
        await self.tick()
        self.muse.chats[JOB][-1] = Message("answer", "assistant", "**Bottom line:** no, it doesn't count.")
        await self.tick()
        await self.tick()
        self.settle()
        await self.tick()
        self.assertEqual(self.store.answer(JOB), ("answered", [
            {"from": "muse", "text": "**Bottom line:** no, it doesn't count."},
        ]))
        self.assertEqual(posts(self.client)[-1], (REQUEST, "Muse (full reply)\n**Bottom line:** no, it doesn't count."))

    async def test_relay_measures_pending_approvals_only_when_asked(self):
        self.create()
        await self.tick()
        self.assertEqual(self.muse.approval_checks, 0)
        self.muse.approvals = 1
        self.store.want_approvals(JOB)
        await self.tick()
        approvals = self.store.approvals(JOB)
        self.assertEqual((approvals["count"], self.muse.approval_checks), (1, 1))
        self.assertGreaterEqual(approvals["checked"], approvals["wanted"])
        await self.tick()
        self.assertEqual(self.muse.approval_checks, 1, "one measurement per request")

    async def test_unstamped_send_reads_as_leons_own_and_survives_a_lost_receipt(self):
        self.muse.stamp = ""
        self.create()
        self.muse.lose_receipt = True
        await self.tick()
        await self.tick()
        self.assertIsNone(self.store.next_inbound(JOB))
        self.assertEqual(self.muse.sends, [(JOB, "Task:\nFind a recipe")])
        await self.tick()
        self.assertNotIn("You in Muse", " ".join(text for _, text in posts(self.client)))

    async def test_handoffs_from_1x_are_not_relayed_again(self):
        with self.store.connection() as db:
            db.execute("INSERT INTO handoffs(id, team, channel, request, actor, root, activity) "
                       "VALUES (?, ?, ?, ?, ?, ?, 0)", (JOB, TEAM, CHANNEL, REQUEST, OWNER, LATER))
            db.execute("INSERT INTO outbound(handoff, event, part, text) VALUES (?, 'old', 0, NULL)", (JOB,))
            db.execute("INSERT INTO outbound(handoff, event, part, text) VALUES (?, 'legacy', 0, ?)",
                       (JOB, "Muse\nPreviously queued"))
        self.muse.chats[JOB] = [Message("old", "assistant", "Already in Slack"), Message("new", "assistant", "New")]
        await self.tick()
        await self.tick()
        self.assertEqual(posts(self.client), [])
        self.assertEqual(posts(self.muse_client), [(LATER, "Previously queued"), (LATER, "New")])

    async def test_muse_delivery_waits_for_its_workspace_while_hermes_can_follow_up(self):
        self.create()
        await self.tick()
        self.muse.chats[JOB].append(Message("answer", "assistant", "Soup"))
        await self.tick()
        self.store.follow_up(JOB, "Vegetarian, please", "Hermes → Muse\nVegetarian, please")
        self.muse_client.auth_test.return_value["team_id"] = "TOTHER"
        await self.tick()
        self.assertEqual(posts(self.muse_client), [])
        self.assertEqual(posts(self.client)[-1], (REQUEST, "Hermes → Muse\nVegetarian, please"))
        self.assertEqual([row["text"] for row in self.store.pending_output(JOB)], ["Muse\nSoup"])
        self.assertTrue(self.muse.sends[-1][1].endswith("Vegetarian, please"))

        self.muse_client.auth_test.return_value["team_id"] = TEAM
        self.slack = Slack(self.adapter, BOT_TOKEN)
        self.slack._new_muse_client = lambda: self.muse_client
        self.relay = Relay(self.store, self.slack, self.muse, OWNER)
        await self.tick()
        self.assertEqual(posts(self.muse_client), [(REQUEST, "Soup")])
        self.assertEqual(self.store.pending_output(JOB), [])

    async def test_muse_dm_links_to_the_handoff_and_preserves_its_thread_after_restart(self):
        self.muse_client.conversations_open = AsyncMock(return_value={"ok": True, "channel": {"id": "DMUSE"}})
        job = self.create(channel="D001")
        await self.tick()
        self.muse.chats[job].append(Message("answer", "assistant", "Soup"))
        await self.tick()
        self.settle()
        await self.tick()
        self.assertEqual(self.store.answer(job), ("answered", [{"from": "muse", "text": "Soup"}]))
        self.assertEqual(len(posts(self.client)), 1)
        opening = self.muse_client.chat_postMessage.await_args.kwargs
        self.assertEqual(opening["channel"], "DMUSE")
        self.assertIsNone(opening["thread_ts"])
        self.assertEqual(opening["text"], "Soup\n\nOriginal Slack thread: https://app.slack.com/archives/D001/p1795000000000001")

        self.store = Store(self.directory)
        self.relay = Relay(self.store, self.slack, self.muse, OWNER)
        self.muse.chats[job].append(Message("second", "assistant", "Lentil soup"))
        await self.tick()
        await self.tick()
        reply = self.muse_client.chat_postMessage.await_args.kwargs
        self.assertEqual(reply["channel"], "DMUSE")
        self.assertEqual(reply["thread_ts"], "1795000100.000001")
        self.assertEqual(reply["text"], "Lentil soup")
        self.muse_client.conversations_open.assert_awaited_once_with(users=OWNER)
        self.assertEqual(self.store.pending_output(job), [])

    async def test_idle_handoffs_back_off_then_stop(self):
        self.create()
        await self.tick()
        for idle, delay in ((10, 5), (600, 60), (7200, 600), (2 * 86400, relay_module.SLEEP)):
            with self.store.connection() as db:
                db.execute("UPDATE handoffs SET activity=?", (relay_module.time.time() - idle,))
            job = self.store.handoff(JOB)
            self.store.schedule(job)
            self.assertAlmostEqual(self.store.handoff(JOB)["due"] - relay_module.time.time(), delay, delta=2)

    async def test_history_windows_and_slack_escaping(self):
        self.create()
        message = Message("answer", "assistant", "<!channel> & " + "x" * 4000)
        self.store.collect(JOB, [message], True)
        self.store.collect(JOB, [message], True)
        parts = [item["text"] for item in self.store.pending_output(JOB)]
        self.assertEqual(len(parts), 4)
        self.assertIn("earlier messages may be missing", parts[1])
        self.assertIn("&lt;!channel&gt; &amp; ", parts[2])

    async def test_muse_mcp_decodes_unstructured_json_tool_content(self):
        result = SimpleNamespace(
            is_error=False, structured_content=None,
            content=[SimpleNamespace(type="text", text='{"ok": true, "exists": false}')],
        )
        session = SimpleNamespace(call_tool=AsyncMock(return_value=result))
        self.assertFalse(await MuseChat(session).exists(JOB))
        session.call_tool.assert_awaited_once_with(
            "muse_chat_exists", arguments={"chat": JOB, "from": "hermes22muse"},
        )

    async def test_muse_mcp_pending_approvals_ignores_other_chats_on_unpatched_server(self):
        async def count(live):
            result = SimpleNamespace(
                is_error=False, structured_content={"ok": True, "live": live}, content=[],
            )
            session = SimpleNamespace(call_tool=AsyncMock(return_value=result))
            return await MuseChat(session).pending_approvals(JOB)

        # Unpatched muse-mcp counts Muse's account-wide approvals.snapshot; ignore when this chat isn't waiting.
        self.assertEqual(await count({
            "pending_approval_count": 2, "agent_activity_code": "working", "task_status": "running",
        }), 0)
        self.assertEqual(await count({
            "pending_approval_count": 1, "agent_activity_code": "online", "task_status": "completed",
        }), 0)
        self.assertEqual(await count({
            "pending_approval_count": 1, "agent_activity_code": "needs_approval", "task_status": "running",
        }), 1)
        self.assertEqual(await count({
            "pending_approval_count": 1, "agent_activity_code": "working",
            "task_status": "pending_user_confirmation",
        }), 1)
        # A server that scopes approvals.snapshot to the chat is trusted directly.
        self.assertEqual(await count({
            "pending_approval_count": 2, "pending_approvals_scoped": True, "agent_activity_code": "working",
        }), 2)
        self.assertEqual(await count({"pending_approval_count": 0}), 0)
        self.assertIsNone(await count({"pending_approval_count": None}))
        self.assertIsNone(await count({"pending_approval_count": True}))

    async def test_relay_pending_approvals_ignores_other_chats_and_main_chat_via_muse_app(self):
        gateway = FakeGateway()
        mixed = {"pending": [
            {"approval_id": "other-side", "status": "pending", "reason": "Other side chat",
             "runtime_request_trace": {"thread_id": "other-chat"}},
            {"approval_id": "main-chat", "status": "pending", "reason": "Main chat",
             "scope": {"requester_source": "runtime"}},
            {"approval_id": "cron-job", "status": "pending", "reason": "Scheduled task",
             "scope": {"scope_type": "schedule", "schedule_id": "morning"}},
        ]}
        gateway.answers[("GET", "/approvals")] = (200, "application/json", json.dumps(mixed).encode())
        web = museweb.MuseWeb(self.directory, gateway)
        web.login.update(museweb.parse_cookies("hatch_sess=first"))
        relay = Relay(self.store, self.slack, self.muse, OWNER, web)
        self.create()
        self.store.want_approvals(JOB)
        await self.tick()
        with self.store.connection() as db:
            db.execute("UPDATE handoffs SET due=0")
        self.store.want_approvals(JOB)
        await relay.tick(self.store.due(OWNER)[0])
        self.assertEqual(self.store.approvals(JOB)["count"], 0)


    async def test_relay_sends_muses_routines_to_linear_with_autolinears_token(self):
        gateway = FakeGateway()
        schedules = {"schedules": [
            {"id": "morning-briefing", "title": "Morning briefing", "schedule_key": "daily@08:00",
             "mode": "task", "enabled": True, "cadence": {"kind": "daily"}},
            {"id": "heartbeat", "title": None, "schedule_key": "interval@30m", "mode": "heartbeat",
             "enabled": True, "is_heartbeat": True},
        ], "invalid": []}
        gateway.answers[("GET", "/tasks")] = (200, "application/json", json.dumps(schedules).encode())
        web = museweb.MuseWeb(self.directory, gateway)
        relay = Relay(self.store, self.slack, self.muse, OWNER, web)
        sent = []

        async def post(url, payload, headers):
            sent.append((url, payload, headers))

        with patch.dict(os.environ, {"AUTOLINEAR_TOKEN": ""}):
            self.assertIsNone(await relay.push_routines(post))
        with patch.dict(os.environ, {"AUTOLINEAR_TOKEN": "secret"}):
            self.assertIsNone(await relay.push_routines(post))  # no muse.ai login yet
            web.login.update(museweb.parse_cookies("hatch_sess=first"))
            self.assertEqual(await relay.push_routines(post), 2)
        url, payload, headers = sent[0]
        self.assertEqual(url, "https://autolinear.leonsilicon.ai/routines")
        self.assertEqual(headers, {"Authorization": "Bearer secret"})
        self.assertEqual([s["id"] for s in payload["schedules"]], ["morning-briefing", "heartbeat"])
        self.assertEqual(payload["invalid"], [])
        self.assertIsInstance(payload["at"], int)
        self.assertEqual(len(sent), 1)

        gateway.answers[("GET", "/tasks")] = (200, "application/json", b'{"nothing": []}')
        with patch.dict(os.environ, {"AUTOLINEAR_TOKEN": "secret"}):
            with self.assertRaises(ValueError):
                await relay.push_routines(post)

    async def test_schedule_runs_survive_a_missing_one_time_schedule_and_a_failed_slack_send(self):
        schedules = [
            {"id": "alarm", "title": "Hermes handoff: Bring the tripod", "schedule_key": "runonce@09:00"},
            {"id": "heartbeat", "title": "Hermes handoff: Heartbeat", "mode": "heartbeat"},
            {"id": "legacy", "title": "Still does its own work"},
        ]
        runs = [
            {"job_id": job, "run_id": run, "scheduled_for_utc": due, "trigger_reason": reason}
            for job, run, due, reason in [
                ("alarm", "scheduled-run", 110, "scheduled"), ("alarm", "retry-run", 110, "retry"),
                ("alarm", "old-run", 90, "scheduled"), ("heartbeat", "heartbeat-run", 110, "scheduled"),
                ("legacy", "legacy-run", 110, "scheduled"),
            ]
        ]
        responses = Mock(side_effect=[
            [{"body": {"schedules": schedules}}, {"body": {"runs": []}}],
            [{"body": {"schedules": schedules[1:]}}, {"body": {"runs": runs}}],
            [{"body": {"schedules": schedules[1:]}}, {"body": {"runs": runs}}],
        ])
        web = SimpleNamespace(signed_in=lambda: True, calls=responses)
        slack = SimpleNamespace(schedule_feed=AsyncMock(return_value=(TEAM, CHANNEL)),
                                post_schedule=AsyncMock(side_effect=[TimeoutError, REQUEST]))
        relay = Relay(self.store, slack, self.muse, OWNER, web)
        await relay.poll_schedule_runs(now=100)
        await relay.poll_schedule_runs(now=120)
        with self.assertRaises(TimeoutError):
            await relay.publish_schedule_runs()
        restarted = Relay(Store(self.directory), slack, self.muse, OWNER, web)
        await restarted.poll_schedule_runs(now=130)
        await restarted.publish_schedule_runs()
        await restarted.publish_schedule_runs()
        self.assertEqual(slack.post_schedule.await_count, 2)
        posted = slack.post_schedule.await_args.args[2]
        self.assertEqual(posted["text"], "Muse reminder due: Bring the tripod\nMuse schedule: alarm\nMuse run: scheduled-run")
        self.assertEqual(slack.post_schedule.await_args_list[0].args[2]["run_id"], posted["run_id"])

    def test_schedule_setup_replaces_the_entire_prompt_and_preserves_existing_timing(self):
        new = schedule_request("   Bring the tripod   ", "tomorrow at 9 AM America/Toronto")
        self.assertIn("Hermes handoff: Bring the tripod", new)
        self.assertIn(TRIGGER_PROMPT, new)
        self.assertIn("Do not run it now", new)
        updated = schedule_request("Bring the tripod", schedule_id="alarm")
        self.assertIn("existing scheduled task with ID \"alarm\" in place", updated)
        self.assertIn("Keep its ID, timing, time zone, enabled state, and recurrence", updated)
        with self.assertRaises(ValueError):
            schedule_request("Bring the tripod", "tomorrow", "alarm")


    def quiet_for(self, seconds):
        with self.store.connection() as db:
            db.execute("UPDATE handoffs SET changed=changed-?", (seconds,))

    async def answered(self, *messages):
        self.muse.chats[JOB] += list(messages)
        await self.tick()
        self.settle()
        await self.tick()
        self.quiet_for(relay_module.ANSWER_GRACE)
        self.assertEqual(self.store.answer(JOB)[0], "answered")

    async def test_unread_muse_answers_wake_hermes_through_autolinear_once(self):
        sent, clock = [], [1000.0]

        async def post(url, payload, headers):
            sent.append((url, payload, headers))
            return 200

        turns = Turns(clock=lambda: clock[0])
        self.relay = Relay(self.store, self.slack, self.muse, OWNER, turns=turns, post=post)
        self.create()
        await self.tick()
        await self.answered(Message("answer", "assistant", "Test your lipids."))
        with patch.dict(os.environ, {"AUTOLINEAR_TOKEN": "token"}):
            # A turn running in the channel may still read it, and gets a moment after it ends.
            turns.start("session", "turn", CHANNEL)
            await self.relay.report_unread()
            turns.end("session", "turn")
            await self.relay.report_unread()
            self.assertEqual(sent, [])
            clock[0] += relay_module.ANSWER_GRACE
            await self.relay.report_unread()
            await self.relay.report_unread()
        [(url, payload, headers)] = sent
        self.assertEqual(url, "https://autolinear.leonsilicon.ai/muse-answer")
        self.assertEqual(headers, {"Authorization": "Bearer token"})
        latest = self.store.handoff(JOB)["nudged"]
        self.assertEqual(payload, {
            "id": f"{JOB}:{latest}", "channel": CHANNEL, "thread": REQUEST,
            "handoff": JOB, "sideChat": f"https://muse.ai/thread/{JOB}",
        })

        # Muse adds to its answer: that is unread too. Without a token nothing is sent.
        await self.answered(Message("more", "assistant", "Also check ApoB."))
        await self.relay.report_unread()
        self.assertEqual(len(sent), 1)
        with patch.dict(os.environ, {"AUTOLINEAR_TOKEN": "token"}):
            await self.relay.report_unread()
        self.assertEqual(len(sent), 2)

        # An answer a tool returned to Hermes is read.
        await self.answered(Message("last", "assistant", "That's all."))
        self.store.mark_collected(JOB)
        with patch.dict(os.environ, {"AUTOLINEAR_TOKEN": "token"}):
            await self.relay.report_unread()
        self.assertEqual(len(sent), 2)

    async def test_autolinear_outages_are_retried_a_minute_later(self):
        codes = [503, 200]

        async def post(url, payload, headers):
            return codes.pop(0)

        self.relay = Relay(self.store, self.slack, self.muse, OWNER, turns=Turns(), post=post)
        self.create()
        await self.tick()
        await self.answered(Message("answer", "assistant", "Test your lipids."))
        with patch.dict(os.environ, {"AUTOLINEAR_TOKEN": "token"}):
            await self.relay.report_unread()
            await self.relay.report_unread()
            self.assertEqual(codes, [200], "waits before trying again")
            self.relay._unread_retry.clear()
            await self.relay.report_unread()
        self.assertEqual(codes, [])
        self.assertEqual(self.store.unread_answers(OWNER, 0), [])

    async def test_muse_answering_leon_in_the_side_chat_wakes_nobody(self):
        sent = []

        async def post(url, payload, headers):
            sent.append(payload)
            return 200

        self.relay = Relay(self.store, self.slack, self.muse, OWNER, turns=Turns(), post=post)
        self.create()
        await self.tick()
        await self.answered(Message("direct", "user", "Never mind"), Message("ok", "assistant", "OK"))
        with patch.dict(os.environ, {"AUTOLINEAR_TOKEN": "token"}):
            await self.relay.report_unread()
            await self.relay.report_unread()
        self.assertEqual(sent, [])
        self.assertEqual(self.store.unread_answers(OWNER, 0), [])

    async def test_running_turns_are_reported_for_channels_only(self):
        sent = []

        async def post(url, payload, headers):
            sent.append((url, payload))
            return 200

        turns = Turns()
        self.relay = Relay(self.store, self.slack, self.muse, OWNER, turns=turns, post=post)
        url = "https://autolinear.leonsilicon.ai/turns"
        with patch.dict(os.environ, {"AUTOLINEAR_TOKEN": "token"}):
            await self.relay.report_turns()
            turns.start("a", "1", "D0DM")
            await self.relay.report_turns()
            # No turn in a channel still goes out: autolinear trusts a missing channel only while reports come.
            self.assertEqual(sent, [(url, {"channels": []})] * 2)
            turns.start("b", "1", "C0B")
            turns.start("c", "1", "G0A")
            await self.relay.report_turns()
        self.assertEqual(sent[-1], (url, {"channels": ["C0B", "G0A"]}))

    def test_answers_from_before_the_upgrade_count_as_read(self):
        import sqlite3

        directory = self.directory / "old"
        directory.mkdir()
        with sqlite3.connect(directory / "relay.db") as db:
            db.executescript("""
                CREATE TABLE handoffs (
                    id TEXT PRIMARY KEY, team TEXT NOT NULL, channel TEXT NOT NULL,
                    request TEXT NOT NULL, actor TEXT NOT NULL, root TEXT,
                    root_attempt REAL, due REAL NOT NULL DEFAULT 0, activity REAL NOT NULL,
                    failures INTEGER NOT NULL DEFAULT 0, busy INTEGER NOT NULL DEFAULT 0,
                    polled REAL NOT NULL DEFAULT 0, changed REAL NOT NULL DEFAULT 0,
                    UNIQUE(team, channel, root)
                );
                CREATE TABLE transcript (
                    seq INTEGER PRIMARY KEY, handoff TEXT NOT NULL, id TEXT NOT NULL,
                    role TEXT NOT NULL, text TEXT NOT NULL, UNIQUE(handoff, id)
                );
            """)
            db.execute("INSERT INTO handoffs(id, team, channel, request, actor, root, activity, changed) "
                       "VALUES ('old', ?, ?, ?, ?, ?, 0, 1e12)", (TEAM, CHANNEL, REQUEST, OWNER, REQUEST))
            db.executemany("INSERT INTO transcript(handoff, id, role, text) VALUES ('old', ?, ?, ?)",
                           [("q", "user", "Question"), ("a", "assistant", "Answer")])
        store = Store(directory)
        self.assertEqual(store.handoff("old")["collected"], 2)
        self.assertEqual(store.unread_answers(OWNER, 0), [])


class TurnsTests(unittest.TestCase):
    def test_a_turn_runs_from_its_first_hook_until_it_ends_or_goes_quiet(self):
        clock = [0.0]
        turns = Turns(clock=lambda: clock[0])
        self.assertEqual(turns.idle(CHANNEL), float("inf"), "no turn since the gateway started")
        turns.start("session", "turn-1", CHANNEL)
        turns.start(None, "turn", "C0IGNORED")
        self.assertEqual(turns.running(), {CHANNEL})
        self.assertIsNone(turns.idle(CHANNEL))
        clock[0] = STALE - 1
        turns.seen("session")
        clock[0] = STALE + 10
        self.assertEqual(turns.running(), {CHANNEL}, "a tool call kept it running")
        turns.end("session", "turn-0")
        self.assertEqual(turns.running(), {CHANNEL}, "another turn of the session ended")
        turns.end("session", "turn-1")
        self.assertEqual(turns.running(), set())
        clock[0] += 5
        self.assertEqual(turns.idle(CHANNEL), 5)

        turns.start("other", "turn", CHANNEL)
        clock[0] += STALE + 1
        self.assertEqual(turns.running(), set(), "its end was never reported")
        self.assertEqual(turns.idle(CHANNEL), 0)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.tools, self.hooks, self.tool_options = {}, {}, {}
        ctx = SimpleNamespace(
            register_tool=lambda name, handler, **kw: (self.tools.__setitem__(name, handler),
                                                       self.tool_options.__setitem__(name, kw)),
            register_platform_handler=lambda platform, factory: setattr(ctx, "wire", factory),
            register_hook=lambda name, callback: self.hooks.__setitem__(name, callback),
        )
        env = {
            "HERMES22MUSE_SLACK_USER_ID": OWNER,
            "HERMES22MUSE_MCP_URL": "http://127.0.0.1:7899/mcp",
            "HERMES22MUSE_MCP_SECRET": "x" * 32,
        }
        self.env = env
        with patch.dict(os.environ, env):
            plugin.register(ctx)
        self.adapter, self.client = fake_adapter()
        self.build = self.adapter._build_message_event = AsyncMock(side_effect=lambda event, **kw: SimpleNamespace(
            source=SimpleNamespace(platform="slack", scope_id=TEAM, chat_id=CHANNEL, chat_name=kw["name"],
                                   user_id=event["user"], message_id=event["ts"], thread_id=event.get("thread_ts")),
            raw_message=event, reply_to_message_id=None,
        ))
        self.adapter._register_mentioned_thread = Mock()
        constants = SimpleNamespace(get_hermes_home=lambda: self.directory)
        with patch.dict(sys.modules, {"hermes_constants": constants, **GATEWAY}), \
                patch.object(plugin, "keep_crash_log"):
            ctx.wire(None, self.adapter)
        self.addAsyncCleanup(self.adapter.disconnect)
        self.session = {"platform": "slack", "scope_id": TEAM, "chat_id": CHANNEL, "thread_id": "",
                        "message_id": REQUEST, "user_id": OWNER}
        patcher = patch.object(plugin, "session", lambda: dict(self.session))
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("WAIT", 0),):
            patcher = patch.object(plugin, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = Store(self.directory / "hermes22muse")

    def call(self, name, **args):
        return json.loads(self.tools[name](args))

    def register_again(self, wire=True):
        """What `hermes plugins install` does to the running gateway: the plugin registers again and
        Hermes wires the new copy on the live adapter, or (as on 2026-11-26) doesn't."""
        tools, hooks = {}, {}
        ctx = SimpleNamespace(
            register_tool=lambda name, handler, **kw: tools.__setitem__(name, handler),
            register_platform_handler=lambda platform, factory: setattr(ctx, "wire", factory),
            register_hook=lambda name, callback: hooks.__setitem__(name, callback),
        )
        with patch.dict(os.environ, self.env):
            plugin.register(ctx)
        if wire:
            constants = SimpleNamespace(get_hermes_home=lambda: self.directory)
            with patch.dict(sys.modules, {"hermes_constants": constants}), patch.object(plugin, "keep_crash_log"):
                ctx.wire(None, self.adapter)
        return tools, hooks

    async def build_turn(self, ts=REQUEST, name="leon-1-test", **raw):
        return await self.adapter._build_message_event({"ts": ts, "user": OWNER, **raw}, name=name)

    async def test_handoff_threads_under_leons_message_with_everything_sent(self):
        guide = self.hooks["pre_llm_call"](platform="slack")["context"]
        self.assertIn(f"mention <@{OWNER}>", guide)
        self.assertIn("muse_handoff", guide)
        self.assertIn("Handle routine work you can complete yourself", guide)
        self.assertIn("Send only the browser steps through grok_bot_send", guide)
        self.assertIn("Use Muse for research and complex work", guide)
        self.assertIn("Set up every Muse reminder and routine with muse_schedule", guide)
        self.assertIn("A watcher is a project in the Watcher status", guide)
        self.assertIn("Silicon Dot", guide)
        self.assertIn("target desktop", guide)
        result = self.call("muse_handoff", task="Summarize LEON-1 and its open questions")
        self.assertEqual(result["status"], "not_delivered")
        self.assertEqual(result["handoff"], JOB)
        self.assertEqual(result["side_chat"], f"https://muse.ai/thread/{JOB}")
        job = self.store.handoff(JOB)
        self.assertEqual(job["root"], REQUEST)
        self.assertIn("first person", guide)
        sent = self.store.next_inbound(JOB)["text"]
        self.assertEqual(sent, "Summarize LEON-1 and its open questions")
        [thread_post] = [row["text"] for row in self.store.pending_output(JOB)]
        self.assertIn(f"Side chat: https://muse.ai/thread/{JOB}", thread_post)
        self.assertTrue(thread_post.endswith("\n\n" + sent))
        self.assertIn("already belongs", self.call("muse_handoff", task="again")["error"])
        # AutoLinear leaves Leon's replies in this thread where they are.
        self.assertTrue(self.adapter._hermes22muse_handoff_thread(TEAM, CHANNEL, REQUEST))
        self.assertFalse(self.adapter._hermes22muse_handoff_thread(TEAM, CHANNEL, LATER))

        # Leon writes in the thread; Hermes forwards it.
        self.session.update(thread_id=REQUEST, message_id=LATER)
        self.assertIn(JOB, self.hooks["pre_llm_call"](platform="slack")["context"])
        self.assertEqual(self.call("muse_reply", text="Only the blockers")["handoff"], JOB)
        self.assertEqual(self.store.pending_output(JOB)[-1]["text"], "Sent to Muse as you\nOnly the blockers")
        self.assertEqual(self.store.next_inbound(JOB)["text"], "Summarize LEON-1 and its open questions")
        self.assertEqual(self.call("muse_wait")["status"], "not_delivered")

    async def test_a_tool_that_returns_muses_answer_marks_it_read(self):
        import time

        self.call("muse_handoff", task="Is keto bad for my LDL?", wait_seconds=0)
        now = time.time()
        with self.store.connection() as db:
            db.execute("UPDATE inbound SET receipt='sent' WHERE handoff=?", (JOB,))
            db.executemany("INSERT INTO transcript(handoff, id, role, text) VALUES (?, ?, ?, ?)",
                           [(JOB, "sent", "user", "Is keto bad for my LDL?"),
                            (JOB, "reply", "assistant", "Test your lipids.")])
            # Muse is quiet and the relay found no approval waiting since it answered.
            db.execute("UPDATE handoffs SET busy=0, polled=?, changed=?, approvals=0, approvals_wanted=?, "
                       "approvals_at=? WHERE id=?", (now, now - 30, now - 2, now - 1, JOB))
        self.assertEqual([row["id"] for row in self.store.unread_answers(OWNER, now - 3600)], [JOB])
        self.assertEqual(self.call("muse_wait")["status"], "answered")
        self.assertEqual(self.store.unread_answers(OWNER, now - 3600), [])

    async def test_a_live_plugin_reload_keeps_using_the_running_relay(self):
        # LEON-527 (2026-11-25): after a reload the new copy had no relay, so Leon's request got no guide.
        root = (await self.build_turn()).source.thread_id
        tools, hooks = self.register_again()
        relay = self.adapter._hermes22muse_relay
        self.session.update(thread_id=root)
        guide = hooks["pre_llm_call"](platform="slack", session_id="s", turn_id="t")["context"]
        self.assertTrue(guide.startswith("[Task progress]"))
        self.assertIn("[Agent routing]", guide)
        # The running relay reports the reloaded copy's turns to autolinear.
        self.assertIn(CHANNEL, relay.turns.running())
        # Looking the tool up isn't turned away; the first other tool is.
        self.assertIsNone(hooks["pre_tool_call"](tool_name="tool_describe", args={"names": ["slack_task_progress"]}))
        self.assertEqual(hooks["pre_tool_call"](tool_name="terminal", args={})["action"], "block")
        result = json.loads(tools["slack_task_progress"]({"quick_answer": "I'll check your calendar."}))
        self.assertEqual(result["quick_answer"], "I'll check your calendar.")
        self.assertIs(self.adapter._hermes22muse_relay, relay)

    async def test_a_reload_hermes_never_wires_still_uses_the_running_relay(self):
        # 2026-11-26: Hermes reloaded the plugin and never wired the new copy, whose hooks then fed a
        # turn list no relay reported, so autolinear reminded Hermes about a turn still running (LEON-533).
        root = (await self.build_turn()).source.thread_id
        relay = self.adapter._hermes22muse_relay
        tools, hooks = self.register_again(wire=False)
        self.session.update(thread_id=root)
        guide = hooks["pre_llm_call"](platform="slack", session_id="s", turn_id="t")["context"]
        self.assertTrue(guide.startswith("[Task progress]"))
        self.assertIn(CHANNEL, relay.turns.running())
        self.assertEqual(hooks["pre_tool_call"](tool_name="terminal", args={})["action"], "block")
        result = json.loads(tools["slack_task_progress"]({"quick_answer": "I'll check your calendar."}))
        self.assertEqual(result["quick_answer"], "I'll check your calendar.")

    async def test_a_stopped_relay_is_never_reused(self):
        worker = self.adapter._hermes22muse_worker
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        self.assertIsNone(plugin.live_relay())
        tools, hooks = self.register_again(wire=False)
        self.assertIsNone(hooks["pre_llm_call"](platform="slack", session_id="s", turn_id="t"))

    async def test_turn_hooks_never_get_in_hermess_way(self):
        self.assertIsNotNone(self.hooks["pre_llm_call"](platform="slack", session_id="s", turn_id="t"))
        self.assertIsNone(self.hooks["post_tool_call"](session_id="s", tool_name="web_search"))
        self.assertIsNone(self.hooks["on_session_end"](session_id="s", turn_id="t", completed=True))
        with patch.object(plugin, "session", side_effect=RuntimeError("no session")):
            self.hooks["on_session_end"]()
            self.hooks["post_tool_call"]()

    def test_a_gateway_crash_leaves_every_threads_stack_in_the_log(self):
        import faulthandler

        with patch.object(plugin, "_crash_log", None), patch.object(faulthandler, "enable") as enable:
            plugin.keep_crash_log(self.directory)
            log = plugin._crash_log
            self.addCleanup(log.close)
        self.assertEqual(Path(log.name), self.directory / "logs" / "gateway_faulthandler.log")
        enable.assert_called_once_with(file=log, all_threads=True)


    async def test_tools_are_always_listed(self):
        # Hermes evaluates a check_fn before the Slack adapter wires the relay and keeps the result.
        self.assertEqual(sorted(self.tools), [
            "muse_app", "muse_app_methods", "muse_approvals", "muse_approve", "muse_file",
            "muse_handoff", "muse_reply", "muse_schedule", "muse_wait", "slack_task_progress"])
        self.assertTrue(all("check_fn" not in options for options in self.tool_options.values()))

    async def test_peer_messages_reach_muse_once_and_hermes_updates_one_channel_message(self):
        opening, follow_up = await asyncio.gather(self.build_turn(), self.build_turn(LATER))
        self.client.chat_postMessage.assert_awaited_once()
        post = self.client.chat_postMessage.await_args.kwargs
        self.assertNotIn("thread_ts", post)
        self.assertEqual(post["text"], "Thinking…")
        root = opening.source.thread_id
        self.assertNotEqual(root, REQUEST)
        self.assertEqual(opening.source.message_id, REQUEST)
        self.assertEqual(opening.reply_to_message_id, root)
        self.assertEqual(follow_up.source.thread_id, root)
        self.assertTrue(self.adapter._hermes22muse_handoff_thread(TEAM, CHANNEL, root))
        self.adapter._register_mentioned_thread.assert_called_with(root, TEAM)
        self.assertEqual(await Store(self.store.directory).progress.open_thread(
            Slack(self.adapter, BOT_TOKEN), TEAM, CHANNEL), root)
        self.client.chat_postMessage.assert_awaited_once()

        self.session.update(thread_id=root)
        self.call("muse_handoff", task="Research the plan with Codex and Grok Bot", wait_seconds=0)
        self.assertEqual(self.store.handoff(JOB)["root"], root)
        route = (TEAM, CHANNEL, root)
        event = SimpleNamespace(source=SimpleNamespace(
            platform="slack", scope_id=TEAM, chat_id=CHANNEL, user_id=OWNER), channel_prompt=None,
            raw_message={"user": OWNER, "thread_ts": root, "ts": "1795000200.000001",
                         "client_msg_id": "de1e6a7e-c0de-4000-8000-000000000001",
                         "text": f":robot_face: *Task slack:C001/{root}*: To Muse: Which constraints apply?"})
        receive = self.hooks["pre_gateway_dispatch"]
        receive(event=event)
        self.assertIn("already delivered to the Muse queue", event.channel_prompt)
        receive(event=event)

        event.raw_message.update(ts="1795000200.000002", text="Browser check found requirement A.",
                                 client_msg_id="de1e6a7e-b075-4b22-8000-000000000001")
        receive(event=event)
        event.raw_message.update(text="Browser check confirmed requirement A.",
                                 _slack_changed_event_ts="1795000200.000003")
        self.adapter._hermes_task_report_edit(TEAM, CHANNEL, event.raw_message)
        with self.store.connection() as db:
            queued = db.execute("SELECT ts, text FROM inbound WHERE handoff=? ORDER BY rowid", (JOB,)).fetchall()
        self.assertEqual(len(queued), 2)
        self.assertIn("Slack collaboration from Codex", queued[1]["text"])
        self.assertIn("Which constraints apply?", queued[1]["text"])

        progress = self.store.progress
        publisher = Slack(self.adapter, BOT_TOKEN)
        await progress.flush(publisher)
        self.client.chat_postMessage.assert_awaited_once()
        update = self.client.chat_update.await_args.kwargs
        self.assertEqual(update["ts"], root)
        progress_quote = (
            "> *Progress*\n"
            f"> • *Codex* · To Muse: Which constraints apply? <https://app.slack.com/archives/{CHANNEL}/"
            "p1795000200000001|Open reply>\n"
            f"> • *Grok Bot* · Browser check confirmed requirement A. <https://app.slack.com/archives/{CHANNEL}/"
            "p1795000200000002|Open reply>"
        )
        self.assertEqual(update["text"], progress_quote)
        [block] = update["blocks"]
        self.assertEqual(block["text"], {"type": "mrkdwn", "text": progress_quote, "verbatim": True})
        self.assertEqual(progress.read(route)["card"], root)

        # A Muse reply goes into the same Slack thread and is recorded with its Slack receipt.
        with self.store.connection() as db:
            self.store._add_output(db, JOB, "final", ["Muse\nTo Codex: The limit is 20."])
        _, muse_client = fake_adapter()
        muse_client.auth_test = AsyncMock(return_value={
            "ok": True, "team_id": TEAM, "user_id": "UMUSE", "bot_id": "BMUSE",
        })
        publisher._new_muse_client = lambda: muse_client
        await Relay(self.store, publisher, FakeMuse(), OWNER).flush_slack(self.store.handoff(JOB))
        self.assertEqual(posts(muse_client), [(root, "To Codex: The limit is 20.")])
        self.assertEqual(self.client.chat_postMessage.await_args.kwargs["thread_ts"], root)

        task_progress = self.call("slack_task_progress", summary="Verified requirement A. Codex is applying the limit.")
        self.assertEqual(task_progress["thread_ts"], task_progress["progress_ts"])
        self.assertTrue(task_progress["queued"])
        self.assertEqual({item["agent"] for item in task_progress["reports"]}, {"Codex", "Grok Bot", "Muse"})
        await Store(self.store.directory).progress.flush(publisher)
        update = self.client.chat_update.await_args.kwargs
        self.assertEqual(update["ts"], root)
        [block] = update["blocks"]
        quote = block["text"]["text"]
        self.assertTrue(quote.startswith("> *Progress*\n> Verified requirement A."))
        self.assertIn("\n> • *Muse* · To Codex: The limit is 20.", quote)
        self.assertIn((root, "Verified requirement A. Codex is applying the limit."), posts(self.client))
        self.assertFalse(self.call("slack_task_progress")["queued"])

        progress.finish(route, "Verified the result.\n\n@Autolinear move done")
        with self.store.connection() as db:
            progress.report(db, route, "Codex", "1795000201.000001", "1795000201.000001", "Confirmed.")
        await Store(self.store.directory).progress.flush(publisher)
        update = self.client.chat_update.await_args.kwargs
        self.assertEqual(update["ts"], root)
        self.assertIn("Confirmed.", update["text"])
        self.assertIn("Verified the result.\n\n@Autolinear move done", update["text"])
        self.assertEqual(update["blocks"][-1]["text"]["text"],
                         "*Update from Hermes*\nVerified the result.\n\n@Autolinear move done")
        self.assertEqual(self.call("slack_task_progress")["latest_answer"],
                         "Verified the result.\n\n@Autolinear move done")

    async def test_a_new_task_shows_hermes_quick_answer_before_any_work(self):
        root = (await self.build_turn()).source.thread_id
        route = (TEAM, CHANNEL, root)
        check = self.hooks["pre_tool_call"]
        publisher = Slack(self.adapter, BOT_TOKEN)

        # Leon's follow-up in the thread doesn't owe a quick answer; only the request that opened the card does.
        self.session.update(thread_id=root, message_id=LATER)
        self.assertNotIn("[Task progress]", self.hooks["pre_llm_call"](platform="slack")["context"])
        self.assertIsNone(check(tool_name="terminal", args={}))

        self.session.update(message_id=REQUEST)
        guide = self.hooks["pre_llm_call"](platform="slack")["context"]
        self.assertTrue(guide.startswith("[Task progress]"))
        self.assertIn("Before any other tool, call slack_task_progress with quick_answer", guide)
        self.assertIn("give your best answer now and say what you'll check", guide)
        self.assertIsNone(check(tool_name="slack_task_progress", args={}))
        blocked = check(tool_name="terminal", args={"command": "gcal list"})
        self.assertEqual(blocked["action"], "block")
        self.assertIn("Call slack_task_progress with quick_answer", blocked["message"])
        # Asked once: Hermes can go on working without a quick answer.
        self.assertIsNone(check(tool_name="terminal", args={"command": "gcal list"}))

        self.assertIn("error", self.call("slack_task_progress", quick_answer="  "))
        result = self.call("slack_task_progress", quick_answer="Probably 12 events, every Thursday since "
                                                                 "September. I'll delete them all and check that "
                                                                 "none are left.\n\n<!channel>")
        self.assertTrue(result["queued"])
        self.assertTrue(result["quick_answer"].startswith("Probably 12 events"))
        await self.store.progress.flush(publisher)
        update = self.client.chat_update.await_args.kwargs
        self.assertEqual(update["ts"], root)
        quick = ("Probably 12 events, every Thursday since September. "
                 "I'll delete them all and check that none are left.")
        self.assertEqual(update["text"], quick + "\n&lt;!channel&gt;")
        [quick_block] = update["blocks"]
        self.assertEqual(quick_block["elements"][0]["elements"], [
            {"type": "text", "text": quick + "\n<!channel>"}])
        self.assertNotIn("[Task progress]", self.hooks["pre_llm_call"](platform="slack")["context"])
        self.assertIn("post brief progress replies", self.hooks["pre_llm_call"](platform="slack")["context"])

        # A queued assignment is not an agent reply and does not show a Progress heading.
        with self.store.connection() as db:
            self.store.progress.report(db, route, "Muse", "", REQUEST, "Assigned. Waiting for the first reply.")
        await self.store.progress.flush(publisher)
        self.assertEqual(self.client.chat_update.await_args.kwargs["text"], quick + "\n&lt;!channel&gt;")

        # Hermes can post a checked milestone while working. Retry only once and keep it after a restart.
        note = "Found the recurring series. The calendar identifies all 12 dates."
        self.assertTrue(self.call("slack_task_progress", summary=note)["queued"])
        send = self.client.chat_postMessage.side_effect
        self.client.chat_postMessage.side_effect = TimeoutError
        await self.store.progress.flush(publisher)
        attempt = self.client.chat_postMessage.await_args.kwargs
        self.assertEqual(attempt["thread_ts"], root)
        self.assertEqual(attempt["text"], note)
        self.assertEqual(attempt["blocks"][0]["block_id"], f"hermes-task-progress-note/{attempt['client_msg_id']}")
        self.assertEqual(attempt["blocks"][0]["text"], {"type": "plain_text", "text": note})
        with self.store.connection() as db:
            db.execute("UPDATE progress_notes SET due=0")
        self.client.chat_postMessage.side_effect = send
        await Store(self.store.directory).progress.flush(publisher)
        self.assertEqual(self.client.chat_postMessage.await_args.kwargs["client_msg_id"], attempt["client_msg_id"])
        self.assertFalse(self.call("slack_task_progress", summary=note)["queued"])
        await self.store.progress.flush(publisher)
        self.assertEqual(posts(self.client).count((root, note)), 2)  # Initial request and retry only.
        update = self.client.chat_update.await_args.kwargs
        progress_quote = "> *Progress*\n> " + note
        self.assertEqual(update["text"], quick + "\n&lt;!channel&gt;\n\n" + progress_quote)
        self.assertEqual(update["blocks"][1]["text"]["text"], progress_quote)

        # The full answer joins the quick one; slack22linear still reads only the answer after its heading.
        self.store.progress.finish(route, "Removed all 12 events.\n\n@Autolinear move done")
        await self.store.progress.flush(publisher)
        text = self.client.chat_update.await_args.kwargs["text"]
        self.assertTrue(text.startswith(quick))
        self.assertNotIn(progress_quote, text)
        heading = "\n\n*Update from Hermes*\n"
        self.assertEqual(text.count(heading), 1)
        self.assertEqual(text[text.index(heading) + len(heading):],
                         "Removed all 12 events.\n\n@Autolinear move done")

        # A session that started before 2.13.0 still has the plan wording.
        result = self.call("slack_task_progress", plan="I'll check the calendar again.")
        self.assertEqual(result["quick_answer"], "I'll check the calendar again.")

    async def test_long_final_answers_fit_slack_blocks_and_keep_the_command_text(self):
        root = (await self.build_turn()).source.thread_id
        first = "Results:\n" + "A verified result.\n" * 140
        second = "The last result.\n\n@Autolinear move done"
        answer = first + "Another verified result.\n" * 40 + second
        self.adapter.truncate_message = Mock(return_value=[first, answer[len(first):]])
        self.store.progress.finish((TEAM, CHANNEL, root), answer)
        await self.store.progress.flush(Slack(self.adapter, BOT_TOKEN))
        update = self.client.chat_update.await_args.kwargs
        self.adapter.truncate_message.assert_called_once_with(answer, 2900)
        self.assertEqual(update["text"], "*Update from Hermes*\n" + answer)
        self.assertEqual(len(update["blocks"]), 2)
        self.assertTrue(all(len(block["text"]["text"]) <= 3000 for block in update["blocks"]))

    async def test_only_the_task_progress_thread_changes_the_card(self):
        """Leon, 2026-11-26: "after i ask a follow up, hermes should NOT edit the original message. the original
        message should only be edited based on new inputs in that message thread"."""
        opening, follow_up = await asyncio.gather(self.build_turn(), self.build_turn(LATER))
        root = opening.source.thread_id
        route, progress = (TEAM, CHANNEL, root), self.store.progress
        # The follow-up still runs in the card thread's conversation, so Hermes keeps its context.
        self.assertEqual(follow_up.source.thread_id, root)
        self.assertTrue(progress.is_follow_up(TEAM, CHANNEL, LATER))
        await self.build_turn()  # Slack delivering the opening request twice doesn't make it a follow-up.
        self.assertFalse(progress.is_follow_up(TEAM, CHANNEL, REQUEST))
        thread = {"thread_id": root, "slack_team_id": TEAM}
        event = SimpleNamespace(is_command=lambda: False)
        sent = self.adapter.sent

        first = "Probably the agent that runs your browser work. I'll check.\n\n@Autolinear stay"
        await self.adapter.send_final_ledgered(event, "session", first, {**thread, "notify": True}, reply_to=REQUEST)
        self.assertEqual(sent, [])
        self.assertEqual(progress.read(route)["answer"], first)
        version = progress.read(route)["version"]

        # Leon's follow-up at the top of the channel: answered there, the card keeps its answer.
        self.session.update(thread_id=root, message_id=LATER)
        guide = self.hooks["pre_llm_call"](platform="slack")["context"]
        self.assertTrue(guide.startswith("[Task progress] Leon wrote this message at the top of the issue channel"))
        self.assertIn("only messages in that thread change Task progress",
                      self.call("slack_task_progress", quick_answer="It's Grok Bot.")["error"])
        self.assertTrue(self.call("slack_task_progress", summary="Grok Bot is the browser agent.")["queued"])
        await progress.flush(Slack(self.adapter, BOT_TOKEN))
        self.assertIn((root, "Grok Bot is the browser agent."), posts(self.client))
        self.assertEqual(self.call("slack_task_progress")["latest_answer"], first)
        draft = await self.adapter.send(CHANNEL, "The browser", metadata={
            **thread, "expect_edits": True, "reply_to_message_id": LATER})
        self.assertFalse(draft.success)  # Drafts still wait for the completed answer.
        answer = "The browser agent is Grok Bot.\n\n@Autolinear stay"
        result, _ = await self.adapter.send_final_ledgered(event, "session", answer, {**thread, "notify": True},
                                                           reply_to=LATER)
        self.assertEqual(result.message_id, "1795000300.000001")
        self.assertEqual(sent, [(CHANNEL, answer, None, {"slack_team_id": TEAM, "notify": True})])
        # The streaming consumer's final names the message it answers in its metadata (also after a redirect).
        await self.adapter.send(CHANNEL, "Streamed answer.", metadata={
            **thread, "notify": True, "reply_to_message_id": LATER})
        self.assertEqual(sent[1], (CHANNEL, "Streamed answer.", None,
                                   {"slack_team_id": TEAM, "notify": True, "reply_to_message_id": LATER}))
        # A message for an agent still goes into the thread.
        await self.adapter.send(CHANNEL, "To Muse: which agent did you use?", reply_to=LATER,
                                metadata={**thread, "notify": True, "_hermes_task_answer": True})
        self.assertEqual(sent[2][3]["thread_id"], root)
        self.assertEqual(progress.read(route)["answer"], first)
        self.assertEqual(progress.read(route)["version"], version)
        # Hermes's notice about a message it turned away is no answer to the task (LEON-540): it goes
        # into the thread like any reply, and the card keeps its answer.
        notice = ("⚠️ Hermes already received a different message under this message id, so this "
                  "version was not saved. Send it again as a new message to continue.")
        await self.adapter.send_final_ledgered(event, "session", notice, {**thread, "notify": True},
                                               reply_to=root)
        self.assertEqual((sent[3][1], sent[3][3]["thread_id"]), (notice, root))
        self.assertEqual(progress.read(route)["answer"], first)
        self.assertEqual(progress.read(route)["version"], version)
        del sent[3]

        # Leon's reply in the card's thread changes the card.
        in_thread = "1795000003.000001"
        self.session.update(message_id=in_thread)
        self.assertNotIn("[Task progress]", self.hooks["pre_llm_call"](platform="slack")["context"])
        self.assertNotIn("error", self.call("slack_task_progress", summary="Checking with Grok Bot."))
        await self.adapter.send_final_ledgered(event, "session", "It's Grok Bot.\n\n@Autolinear move done",
                                               {**thread, "notify": True}, reply_to=in_thread)
        self.assertEqual(len(sent), 3)
        await progress.flush(Slack(self.adapter, BOT_TOKEN))
        update = self.client.chat_update.await_args.kwargs
        self.assertEqual(update["ts"], root)
        self.assertTrue(update["text"].endswith("*Update from Hermes*\nIt's Grok Bot.\n\n@Autolinear move done"))
        self.assertNotIn("The browser agent is Grok Bot.", update["text"])

        # A gateway update is a new reply in the original thread and carries the progress marker.
        interim = {**thread, "_interim_send": True, "notify": True, "reply_to_message_id": in_thread}
        commentary = "Grok Bot confirmed the active account."
        result = await self.adapter.send(CHANNEL, commentary, reply_to=in_thread, metadata=interim)
        self.assertTrue(result.success)
        self.assertIsNotNone(result.message_id)
        self.assertEqual(len(sent), 3)
        post = self.client.chat_postMessage.await_args.kwargs
        self.assertEqual(post["thread_ts"], root)
        self.assertEqual(post["text"], commentary)
        self.assertEqual(post["blocks"][0]["block_id"], f"hermes-task-progress-note/{post['client_msg_id']}")
        self.assertEqual(progress.read(route)["answer"], "It's Grok Bot.\n\n@Autolinear move done")

        # The gateway's fallback status path only provides the task route.
        status = "I found the account overview."
        await self.adapter.send(CHANNEL, status, metadata=thread)
        post = self.client.chat_postMessage.await_args.kwargs
        self.assertEqual((post["thread_ts"], post["text"]), (root, status))
        self.assertIn("hermes-task-progress-note/", post["blocks"][0]["block_id"])

        # A directed reply still goes to the agent as its own Slack message in the thread.
        instruction = "To Grok Bot: check the expiration date."
        await self.adapter.send(CHANNEL, instruction, reply_to=in_thread, metadata=interim)
        self.assertEqual(sent[-1], (CHANNEL, instruction, in_thread, interim))

    async def test_a_button_prompt_in_the_card_thread_goes_to_the_channel_and_pings_leon(self):
        """LEON-586 (2026-11-27 and 11-28): both of Hermes's SOUL.md approvals waited in Task progress's thread,
        where Slack notified no one, until they timed out; the second while Leon was in the channel."""
        root = (await self.build_turn()).source.thread_id
        approval = [{"type": "section", "text": {"type": "mrkdwn", "text": ":warning: *Approval needed*"}},
                    {"type": "actions", "elements": [{"type": "button", "action_id": "hermes_approve_once"},
                                                     {"type": "button", "action_id": "hermes_deny"}]}]
        result = await self.adapter._post_interactive_blocks(
            CHANNEL, "Approval needed", approval, {"thread_id": root, "slack_team_id": TEAM})
        self.assertEqual(result["ts"], "1795000400.000001")
        line = f"<@{OWNER}> Hermes is waiting for your approval. Tap a button below: a typed reply doesn't approve it."
        self.assertEqual(self.adapter.prompts[0], (
            CHANNEL, f"{line}\nApproval needed",
            [{"type": "context", "elements": [{"type": "mrkdwn", "text": line}]}, *approval],
            {"slack_team_id": TEAM}, {}))
        # A question waits for Leon too.
        question = [{"type": "section", "text": {"type": "mrkdwn", "text": "Which calendar?"}},
                    {"type": "actions", "elements": [{"type": "button", "action_id": "hermes_clarify_0"}]}]
        await self.adapter._post_interactive_blocks(
            CHANNEL, "Which calendar?", question, {"thread_ts": root, "thread_id": root, "slack_team_id": TEAM},
            sanitize=False)
        text, blocks, metadata, options = self.adapter.prompts[1][1:]
        self.assertEqual((text, metadata, options), (f"<@{OWNER}> Hermes is waiting for your answer below.\n"
                                                     "Which calendar?", {"slack_team_id": TEAM}, {"sanitize": False}))
        self.assertEqual(blocks[1:], question)
        # Prompts anywhere else stay where Hermes put them.
        other = {"thread_id": "1795000009.000001", "slack_team_id": TEAM}
        await self.adapter._post_interactive_blocks(CHANNEL, "Approval needed", approval, other)
        await self.adapter._post_interactive_blocks(CHANNEL, "Approval needed", approval, None)
        self.assertEqual(self.adapter.prompts[2:], [(CHANNEL, "Approval needed", approval, other, {}),
                                                    (CHANNEL, "Approval needed", approval, None, {})])

    async def test_an_answer_that_needs_leon_is_a_new_message_in_the_channel(self):
        """Leon, 2026-11-27: "if hermes needs my response on something, instead of just updating the original
        message, it should send a new one in the task channel so the ping actually reaches me instead of an edit"."""
        root = (await self.build_turn()).source.thread_id
        route, progress = (TEAM, CHANNEL, root), self.store.progress
        thread = {"thread_id": root, "slack_team_id": TEAM}
        event = SimpleNamespace(is_command=lambda: False)
        publisher = Slack(self.adapter, BOT_TOKEN)
        sent = self.adapter.sent

        # A mention in code notifies nobody, so that answer still goes onto the card.
        quoted = f"Hermes writes `<@{OWNER}>` to mention you.\n\n@Autolinear stay"
        await self.adapter.send_final_ledgered(event, "session", quoted, {**thread, "notify": True}, reply_to=REQUEST)
        self.assertEqual(sent, [])
        self.assertEqual(progress.read(route)["answer"], quoted)

        question = f"<@{OWNER}> The newer order is #9884. Approve this email?\n\n@Autolinear move leon"
        result, _ = await self.adapter.send_final_ledgered(event, "session", question, {**thread, "notify": True},
                                                           reply_to=REQUEST)
        self.assertEqual(result.message_id, "1795000300.000001")
        self.assertEqual(sent, [(CHANNEL, question, None, {"slack_team_id": TEAM, "notify": True})])
        await progress.flush(publisher)
        update = self.client.chat_update.await_args.kwargs
        self.assertEqual(update["ts"], root)
        note = f"Asked you in <https://app.slack.com/archives/{CHANNEL}/p1795000300000001|a new message> in this channel."
        self.assertTrue(update["text"].endswith("*Update from Hermes*\n" + note))
        # The card shows neither the mention nor the command, which Autolinear reads from the new message.
        self.assertNotIn(OWNER, update["text"])
        self.assertNotIn("@Autolinear", update["text"])

        # The same holds for an answer to Leon's reply in the card's thread, a streamed one included.
        in_thread = "1795000003.000001"
        await self.adapter.send(CHANNEL, f"Which card, <@{OWNER}|leon>?", metadata={
            **thread, "notify": True, "reply_to_message_id": in_thread})
        self.assertEqual(sent[1], (CHANNEL, f"Which card, <@{OWNER}|leon>?", None,
                                   {"slack_team_id": TEAM, "notify": True, "reply_to_message_id": in_thread}))
        self.assertIn("p1795000300000002|a new message", progress.read(route)["answer"])

        # A mention of someone else stays on the card.
        await self.adapter.send_final_ledgered(event, "session", "Sent it to <@U002>.", {**thread, "notify": True},
                                               reply_to=in_thread)
        self.assertEqual(len(sent), 2)
        self.assertEqual(progress.read(route)["answer"], "Sent it to <@U002>.")

    async def test_cards_from_before_plans_keep_working(self):
        with self.store.connection() as db:
            db.execute("DROP TABLE progress")
            db.execute("CREATE TABLE progress (team TEXT, channel TEXT, root TEXT, version INTEGER NOT NULL "
                       "DEFAULT 0, published INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0, card TEXT, "
                       "summary TEXT NOT NULL DEFAULT '', PRIMARY KEY(team, channel, root))")
            db.execute("INSERT INTO progress(team, channel, root, card) VALUES (?, ?, ?, ?)",
                       (TEAM, CHANNEL, REQUEST, REQUEST))
        progress = Store(self.store.directory).progress
        self.assertFalse(progress.needs_plan((TEAM, CHANNEL, REQUEST), REQUEST))
        self.assertEqual(progress.read((TEAM, CHANNEL, REQUEST))["plan"], "")

    async def test_keeps_existing_slack_threads_and_retries_a_failed_open(self):
        self.assertIsNone((await self.build_turn(name="general")).source.thread_id)
        self.assertEqual((await self.build_turn(LATER, thread_ts=REQUEST)).source.thread_id, REQUEST)
        self.assertIsNone((await self.build_turn(user="UOTHER")).source.thread_id)
        self.client.chat_postMessage.assert_not_awaited()

        send = self.client.chat_postMessage.side_effect
        self.client.chat_postMessage.side_effect = TimeoutError
        self.assertIsNone((await self.build_turn()).source.thread_id)
        self.assertEqual(self.store.progress.recent(TEAM, CHANNEL), [])
        self.client.chat_postMessage.side_effect = send
        self.assertIsNotNone((await self.build_turn()).source.thread_id)
        self.assertEqual(self.client.chat_postMessage.await_args_list[0].kwargs["client_msg_id"],
                         self.client.chat_postMessage.await_args_list[1].kwargs["client_msg_id"])

    async def test_existing_issue_reuses_its_original_collaboration_thread(self):
        route = (TEAM, CHANNEL, REQUEST)
        self.store.progress.update(route, "Work is underway.")
        await self.store.progress.flush(Slack(self.adapter, BOT_TOKEN))
        self.assertIn("Collaboration thread", self.client.chat_postMessage.await_args.kwargs["text"])
        self.assertEqual((await self.build_turn(LATER)).source.thread_id, REQUEST)
        self.client.chat_postMessage.assert_awaited_once()

    async def test_ignores_unmarked_or_outside_participants(self):
        receive = self.hooks["pre_gateway_dispatch"]
        event = SimpleNamespace(source=SimpleNamespace(
            platform="slack", scope_id=TEAM, chat_id=CHANNEL, user_id="UOTHER"), channel_prompt=None,
            raw_message={"user": "UOTHER", "thread_ts": REQUEST, "ts": LATER,
                         "client_msg_id": "de1e6a7e-c0de-4000-8000-000000000001", "text": "To Muse: test"})
        receive(event=event)
        event.source.user_id = event.raw_message["user"] = OWNER
        event.raw_message["client_msg_id"] = "unmarked"
        receive(event=event)
        self.assertIsNone(self.store.progress.read((TEAM, CHANNEL, REQUEST)))

    async def test_waiting_tools_report_a_pending_approval(self):
        self.assertEqual(self.call("muse_handoff", task="Book the usual table for Friday")["status"], "not_delivered")
        now = relay_module.time.time()
        with self.store.connection() as db:
            db.execute("UPDATE handoffs SET approvals=1, approvals_at=?, approvals_wanted=?", (now, now - 1))
        result = self.call("muse_wait")
        self.assertEqual((result["status"], result["approvals_pending"]), ("waiting_for_approval", 1))
        self.assertIn("!muse approval-allow <code>", result["next"])
        self.assertIn("Payments, purchases, money transfers", result["next"])

    async def test_tools_and_guide_ignore_other_people_and_platforms(self):
        for change in ({"user_id": "UOTHER"}, {"platform": "telegram"}, {"scope_id": ""}):
            with self.subTest(change=change):
                saved = dict(self.session)
                self.session.update(change)
                self.assertIn("error", self.call("muse_handoff", task="Do it"))
                self.assertIsNone(self.hooks["pre_llm_call"](platform=self.session["platform"]))
                self.assertIsNone(self.hooks["pre_tool_call"](tool_name="terminal", args={}))
                self.session = saved
        self.assertIn("error", self.call("muse_handoff", task="  "))
        self.assertIn("error", self.call("muse_reply", text="hi", handoff="unknown"))
        self.assertTrue(self.adapter._hermes22muse_owns_request(OWNER, TEAM, CHANNEL, REQUEST))


class ApprovalDecisionTests(unittest.TestCase):
    def decide(self, status, waited=0, now=1000, **approvals):
        state = {"count": None, "checked": 0, "wanted": 0, "changed": 0, **approvals}
        return plugin.call(plugin.__file__, "approvalDecision", state, status, now, waited)

    def test_working_handoffs_are_measured_after_20_s_and_every_30_s(self):
        self.assertEqual(self.decide("working", waited=5), "wait")
        self.assertEqual(self.decide("working", waited=25), "check")
        self.assertEqual(self.decide("working", waited=40, wanted=990, checked=995, count=0), "wait")
        self.assertEqual(self.decide("working", waited=60, wanted=960, checked=965, count=0), "check")
        self.assertEqual(self.decide("working", waited=25, wanted=995), "wait", "a measurement is outstanding")

    def test_an_answer_is_measured_once_before_it_is_returned(self):
        self.assertEqual(self.decide("answered", changed=990), "check")
        self.assertEqual(self.decide("answered", changed=990, wanted=995), "wait")
        self.assertEqual(self.decide("answered", changed=990, wanted=995, checked=998, count=0), "clear")
        self.assertEqual(self.decide("answered", changed=990, wanted=970), "clear", "a lost measurement")

    def test_a_fresh_pending_count_is_reported(self):
        self.assertEqual(self.decide("answered", changed=990, wanted=995, checked=998, count=2), "pending")
        self.assertEqual(self.decide("working", wanted=995, checked=998, count=1), "pending")
        self.assertEqual(self.decide("working", waited=10, wanted=900, checked=910, count=1), "wait", "stale")


class HistoryTests(unittest.TestCase):
    @staticmethod
    def event(role, identifier, text, ready=True, **payload):
        # Rows shaped like the Link's GET /chat/history, as recorded on a paired Gadget.
        return {
            "event_name": f"message.{role}", "role": role, "message_id": identifier,
            "display_text": text, "display_text_ready": ready, "seq": 1,
            "payload": {
                "session_id": JOB, "message_id": identifier, "role": role, "content": text,
                "display_text": text, "display_text_ready": ready, **payload,
            },
        }

    def response(self, *events):
        return {"ok": True, "status": 200, "scoped_to_chat": "Hermes test", "response": {
            "ok": True, "result": {"channel": "app", "has_more": False, "chat_events": list(events)}}}

    def test_only_completed_messages_from_linked_chat_are_projected(self):
        event = self.event
        self.assertEqual(history_messages(self.response(
            event("assistant", "foreign", "private", session_id="main", status="completed"),
            event("user", "input", "A direct Muse message"),
            {"event_name": "delta.presentation", "payload": {}},
            event("assistant", "final", "Done", status="completed"),
        ), JOB), ([
            Message("input", "user", "A direct Muse message"),
            Message("final", "assistant", "Done"),
        ], False, False))

    def test_unfinished_replies_are_held_back_and_reported(self):
        for row in (self.event("assistant", "draft", "Dra", ready=False, status="completed"),
                    self.event("assistant", "streaming", "Half a rep", status="streaming"),
                    # While Muse streams, the row has no status and only the text so far.
                    self.event("assistant", "statusless", "**Bottom line:")):
            with self.subTest(row=row["message_id"]):
                self.assertEqual(history_messages(self.response(row), JOB), ([], False, True))

    def test_unknown_or_failed_history_is_a_retry(self):
        for response in ({}, {"events": {"items": []}}, {"ok": False, "events": []}):
            with self.subTest(response=response), self.assertRaises(MuseError):
                history_messages({"ok": True, "scoped_to_chat": "side", "response": response}, JOB)


if __name__ == "__main__":
    unittest.main()


class ChunkTests(unittest.TestCase):
    def test_a_chunk_ends_at_a_line_break_in_its_second_half(self):
        text = "a" * 3000 + "\n" + "b" * 1000 + "\n" + "c" * 5000
        chunks = list(relay_module.slack_chunks(text))
        self.assertEqual("".join(chunks), text)
        # The first break is in the chunk's second half and is used; the next one is too early.
        self.assertEqual([len(chunk) for chunk in chunks], [3001, 3400, 2601])

    def test_one_long_line_takes_time_in_proportion_to_its_length(self):
        # JavaScript holds Python's interpreter lock while it runs, so a search that went back to the
        # start of the text for every chunk stopped all of Hermes for the square of the length.
        def seconds(length):
            started = time.perf_counter()
            list(relay_module.slack_chunks("x" * length))
            return time.perf_counter() - started

        seconds(1000)
        small = min(seconds(800_000), seconds(800_000))
        large = min(seconds(3_200_000), seconds(3_200_000))
        self.assertLess(large, max(8 * small, 0.5))


class FakeGateway:
    """muse.ai and the personal computer's gateway, speaking the app's own wire format."""

    def __init__(self):
        self.requests, self.site, self.sockets = [], [], 0
        self.login, self.next_login = "first", None
        self.answers = {}
        self.reject_token = False

    def http(self, verb, url, headers, body=None):
        path = url.removeprefix("https://muse.ai")
        self.site.append((verb, path, headers.get("Cookie"), json.loads(body) if body else None))
        if f"hatch_sess={self.login}" not in headers["Cookie"]:
            return 401, "{}", {}
        issued = {}
        if path == "/api/session":
            if self.next_login:
                self.login, self.next_login = self.next_login, None
                issued = {"hatch_sess": (self.login, 1900000000)}
            return 200, json.dumps({"status": "assigned", "vm_id": "vm-1", "vm_state": "RUNNING"}), issued
        if path == "/api/auth/check":
            return 200, '{"access_token": "access"}', {}
        if path == "/api/hatch/token":
            return 200, '{"token": "tok en"}', {}
        return 404, "{}", {}

    def socket(self, url):
        self.sockets += 1
        if self.reject_token:
            self.reject_token = False
            raise ConnectionError("refused")
        assert url == "wss://hatch.metaaivm.com/v1/noise?vm_id=vm-1&auth_token=tok%20en"
        return FakeSocket(self)


class FakeSocket:
    """The responder side of Noise_XX_25519_AESGCM_SHA256 and the gateway behind it."""

    def __init__(self, gateway):
        self.gateway, self.outbox, self.noise, self.closed = gateway, [], museweb.Noise(), False

    def send(self, data):
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

        noise = self.noise
        if not hasattr(noise, "remote"):
            noise.remote = X25519PublicKey.from_public_bytes(data[:32])
            noise._mix_hash(data[:32])
            noise._handshake_seal(data[32:], opening=True)
            reply = noise._public(noise.ephemeral)
            noise._mix_hash(reply)
            noise._mix_key(noise.ephemeral.exchange(noise.remote))
            reply += noise._handshake_seal(noise._public(noise.static))
            noise._mix_key(noise.static.exchange(noise.remote))
            self.outbox.append(reply + noise._handshake_seal(b""))
        elif noise.sending is None:
            client = X25519PublicKey.from_public_bytes(noise._handshake_seal(data[:48], opening=True))
            noise._mix_key(noise.ephemeral.exchange(client))
            noise._handshake_seal(data[48:], opening=True)
            noise.receiving, noise.sending = ([key, 0] for key in noise._hkdf(b""))
        else:
            self.serve(noise.decrypt(data))

    def serve(self, frame):
        fields = lambda data: dict(museweb._fields(data))
        envelope = fields(fields(frame)[4])
        service_frame = fields(envelope[2])
        request = fields(service_frame[2])
        verb, path = bytes(request[1]).decode(), bytes(request[2]).decode()
        body = json.loads(bytes(request[4])) if 4 in request else None
        self.gateway.requests.append((envelope.get(1, 0), verb, path, body))
        status, kind, data = self.gateway.answers.get((verb, path), (404, "application/json", b'{"error":"no"}'))
        header = museweb._field(1, b"content-type") + museweb._field(2, kind.encode())
        response = museweb._field(1, status) + museweb._field(2, header)
        # The head, then the body in two chunks.
        half = len(data) // 2
        for part in (museweb._field(3, response),
                     museweb._field(4, museweb._field(1, data[:half])),
                     museweb._field(4, museweb._field(1, data[half:]) + museweb._field(2, 1))):
            payload = museweb._field(1, museweb._field(1, service_frame[1]) + part)
            # Long answers span several Noise messages that share a chunk ID.
            pieces = [payload[index:index + 30000] for index in range(0, len(payload), 30000)] or [b""]
            for index, piece in enumerate(pieces):
                self.outbox.append(self.noise.encrypt(
                    museweb._field(1, 7) + museweb._field(2, index) + museweb._field(3, len(pieces))
                    + museweb._field(4, piece)))

    def receive(self, timeout):
        if not self.outbox:
            raise TimeoutError
        return self.outbox.pop(0)

    def close(self):
        self.closed = True


PENDING = {"pending_approvals": [
    {"approval_id": "ap-1", "status": "pending", "requested_at_ms": 1795000000000,
     "session_id": JOB,
     "decision_options": [{"kind": "allow_once", "label_text": "Allow"},
                          {"kind": "allow_always", "label_text": "Always allow apple.com",
                           "label": {"kind": "allow_always_scoped", "scope": "destination_domain"}},
                          {"kind": "deny", "label_text": "Deny"}],
     "display": {"display_name": "Browser", "card_title": "Allow {assistant} to log in to apple.com using Browser?",
                 "permission_question": {"kind": "sign_in", "text": "Allow {assistant} to log in?"},
                 "purpose_summary": "The agent wants to sign in to apple.com using your saved login details.",
                 "request_preview_document": {"blocks": [{"type": "fields", "rows": [
                     {"label": "Website", "value": "apple.com"}]}]}}},
    {"approval_id": "ap-2", "status": "pending", "approval_type": "purchase_preapproval",
     "session_id": "another-chat",
     "decision_options": [{"kind": "allow_once"}, {"kind": "deny"}],
     "display": {"card_title": "Approve this purchase"}},
    {"approval_id": "ap-3", "status": "allowed", "decision_options": [{"kind": "allow_once"}]},
]}
# A request as the Muse computer sent it on 2026-11-25, without its tokens and traces.
LIVE = {"emitted_at_ms": 1795575913737, "schema_version": 1, "recent": [], "pending": [{
    "approval_id": "browser-consent-aacaed", "status": "pending", "decision_options": ["allow_once", "deny"],
    "action_label": "Fill saved credentials", "display_name": "Browser", "host": "browser",
    "method": "page.credential_fill", "path": "/page.credential_fill?target=https://apple.com",
    "policy_subject": "connector.browser.page.credential_fill", "message_revision": 0, "payment_revision": 0,
    "reason": "Allow Muse to fill saved credentials in Browser for \"https://apple.com\"?",
    "short_explanation": "Muse wants to fill saved credentials.",
    "requested_at_ms": 1795574423543, "request_expires_at_ms": 1795585223543,
    "runtime_request_trace": {"thread_id": JOB, "device_id": "homelink-42967a"}}]}


class MuseWebTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gateway = FakeGateway()
        self.web = museweb.MuseWeb(Path(temporary.name), self.gateway)
        self.web.login.update(museweb.parse_cookies("hatch_sess=first; theme=dark"))

    def test_a_call_travels_the_apps_wire_format_and_extends_the_login(self):
        self.gateway.answers[("GET", "/approvals")] = (200, "application/json", json.dumps(PENDING).encode())
        self.gateway.answers[("POST", "/approvals/ap%2F1/decide")] = (
            200, "application/json", json.dumps({"approval": {"status": "allowed", "text": "x" * 70000}}).encode())
        self.gateway.next_login = "second"
        first, second = self.web.calls([
            ("egress.approvals", None),
            ("egress.approval.decide", {"approval_id": "ap/1", "decision": "allow_once"})])
        self.assertEqual(first["body"], PENDING)
        self.assertEqual(len(second["body"]["approval"]["text"]), 70000)
        # Sentinel is service 1; the body carries every parameter, as the app sends it.
        self.assertEqual(self.gateway.requests, [
            (1, "GET", "/approvals", None),
            (1, "POST", "/approvals/ap%2F1/decide", {"approval_id": "ap/1", "decision": "allow_once"})])
        # muse.ai replaced the login cookie while answering; the new one was saved and used at once.
        saved = self.web.login.read()
        self.assertEqual((saved["cookies"]["hatch_sess"], saved["expires"]["hatch_sess"], saved["computer"]),
                         ("second", 1900000000, "vm-1"))
        self.assertEqual([cookie for _, path, cookie, _ in self.gateway.site],
                         ["hatch_sess=first; theme=dark"] + ["hatch_sess=second; theme=dark"] * 2)
        self.assertEqual(oct(self.web.login.path.stat().st_mode & 0o777), "0o600")
        # Tokens and the computer are reused for the next call.
        self.web.call("egress.approvals")
        self.assertEqual(len(self.gateway.site), 3)

    def test_a_refused_connection_fetches_fresh_tokens_once(self):
        self.gateway.answers[("GET", "/goals")] = (200, "application/json", b'{"goals": []}')
        self.web.call("goals.list")
        self.gateway.reject_token = True
        self.assertEqual(self.web.call("goals.list")["body"], {"goals": []})
        self.assertEqual([path for _, path, _, _ in self.gateway.site].count("/api/hatch/token"), 2)
        self.assertIn("/api/hatch/vm/wake", [path for _, path, _, _ in self.gateway.site])

    def test_an_ended_login_is_reported_and_remembered(self):
        self.gateway.login = "revoked elsewhere"
        with self.assertRaises(museweb.MuseLoginRequired):
            self.web.call("goals.list")
        self.assertTrue(self.web.signed_in(), "one refusal may be muse.ai's mistake")
        with self.assertRaises(museweb.MuseLoginRequired):
            self.web.call("goals.list")
        self.assertFalse(self.web.signed_in())
        self.assertEqual(self.web.login.read()["cookies"]["hatch_sess"], "first")
        requests = len(self.gateway.site)
        with self.assertRaises(museweb.MuseLoginRequired):
            self.web.call("goals.list")
        self.assertEqual(len(self.gateway.site), requests, "an ended login is not sent again")

    def test_errors_streams_and_requests_follow_the_app(self):
        with self.assertRaises(museweb.MuseApiError) as refused:
            self.web.call("tasks.list")
        self.assertEqual(refused.exception.status, 404)
        self.gateway.answers[("POST", "/chat/subscribe")] = (
            200, "application/x-ndjson", b'{"a":1}\n{"b":2}\n')
        self.assertEqual(self.web.call("chat.subscribe", listen=1)["body"], [{"a": 1}, {"b": 2}])
        self.assertEqual(museweb.build_request("chat.history", {"session_id": "s", "limit": 5}),
                         (0, "GET", "/chat/history?session_id=s&limit=5", b""))
        self.assertEqual(museweb.build_request("fs.raw", {"path": "workspace/a b.txt"})[2],
                         "/fs/raw/workspace/a%20b.txt")
        self.assertEqual(museweb.build_request("tasks.delete", {"job_id": "j"}), (0, "DELETE", "/api/tasks/j", b""))
        with self.assertRaises(museweb.MuseWebError):
            museweb.build_request("tasks.run", {})
        with self.assertRaises(museweb.MuseWebError):
            museweb.build_request("no.such.method", {})

    def test_cookies_are_read_from_a_browser_export(self):
        exported = json.dumps([
            {"name": "hatch_sess", "value": "abc", "domain": ".muse.ai", "expires": 1900000000.5},
            {"name": "other", "value": "x", "domain": "gadgets.muse.ai", "expires": -1}])
        self.assertEqual(museweb.parse_cookies(exported), {"hatch_sess": ("abc", 1900000000.5)})
        wrapped = json.dumps({"success": True, "data": {"cookies": json.loads(exported)}})
        self.assertEqual(museweb.parse_cookies(wrapped), {"hatch_sess": ("abc", 1900000000.5)})
        self.assertEqual(museweb.parse_cookies("Cookie: a=1; hatch_sess=b=c"), {"a": ("1", None), "hatch_sess": ("b=c", None)})

    def test_the_method_table_is_read_from_the_apps_page(self):
        page = ("x=[{method:`tasks.run`,httpMethod:`POST`,path:`/tasks/{job_id}/run`},"
                "{method:`dyi.export`,httpMethod:`POST`,path:`/api/dyi/export`,service:`daemon`,"
                "requestBody:`empty`,requestTimeoutMs:39e5,maxResponseBytes:1/0}]")
        self.assertEqual(museweb.app_routes(page), [
            {"method": "dyi.export", "http": "POST", "path": "/api/dyi/export", "service": "daemon",
             "requestBody": "empty", "requestTimeoutMs": 3900000},
            {"method": "tasks.run", "http": "POST", "path": "/tasks/{job_id}/run"}])


class MuseAppPolicyTests(unittest.TestCase):
    def policy(self, name, *args):
        return plugin.call(plugin.__file__, name, *args)

    def test_approvals_are_listed_for_their_side_chat_in_plain_terms(self):
        listed = self.policy("approvalList", PENDING, JOB)
        self.assertEqual(listed["pending_in_all_chats"], 2)
        [approval] = listed["approvals"]
        self.assertEqual(approval["asks"], "Allow Muse to log in to apple.com using Browser?")
        self.assertEqual(approval["details"], ["Website: apple.com"])
        self.assertEqual([option["decision"] for option in approval["options"]],
                         ["allow_once", "allow_always", "deny"])
        self.assertNotIn("leon_decides", approval)
        everything = self.policy("approvalList", PENDING, None)["approvals"]
        self.assertEqual(everything[1]["leon_decides"], "it spends or moves money")

    def test_a_request_from_the_real_muse_computer_is_read_and_answered(self):
        self.assertEqual(self.policy("approvalList", LIVE, "another-chat")["approvals"], [])
        [approval] = self.policy("approvalList", LIVE, JOB)["approvals"]
        self.assertEqual(approval["asks"], 'Allow Muse to fill saved credentials in Browser for "https://apple.com"?')
        self.assertEqual((approval["side_chat_id"], approval["kind"]), (JOB, "page.credential_fill"))
        self.assertEqual(approval["target"], "Browser · /page.credential_fill?target=https://apple.com")
        self.assertEqual(approval["options"], [{"decision": "allow_once"}, {"decision": "deny"}])
        self.assertNotIn("leon_decides", approval)
        self.assertEqual(self.policy("approvalDecide", LIVE, "browser-consent-aacaed", "allow_once", False)["params"], {
            "approval_id": "browser-consent-aacaed", "decision": "allow_once", "expected_message_revision": 0})
        self.assertIn("does not offer", self.policy(
            "approvalDecide", LIVE, "browser-consent-aacaed", "allow_always", True)["error"])

    def test_approvals_in_other_chats_main_chat_and_background_scopes_do_not_match_a_side_chat(self):
        response = {"pending_approvals": [
            {"approval_id": "main", "status": "pending", "reason": "Main chat",
             "scope": {"requester_source": "runtime"}},
            {"approval_id": "sched", "status": "pending", "reason": "Morning briefing",
             "scope": {"scope_type": "schedule", "schedule_id": "morning", "thread_id": None}},
            {"approval_id": "space", "status": "pending", "reason": "Space web read",
             "scope": {"scope_type": "space", "space_slug": "trip"}},
            {"approval_id": "flat-other", "status": "pending", "reason": "Flat legacy other side chat",
             "thread_id": "other-chat"},
            {"approval_id": "scope-trace-other", "status": "pending", "reason": "Scope trace other side chat",
             "scope": {"runtime_request_trace": {"thread_id": "other-chat"}}},
            {"approval_id": "this-flat", "status": "pending", "reason": "Flat legacy this side chat",
             "thread_id": JOB},
            {"approval_id": "this-scope-trace", "status": "pending", "reason": "Scope trace this side chat",
             "scope": {"runtime_request_trace": {"thread_id": f" {JOB} "}}},
        ]}
        listed = self.policy("approvalList", response, JOB)
        self.assertEqual(listed["pending_in_all_chats"], 7)
        self.assertEqual([item["approval"] for item in listed["approvals"]],
                         ["this-flat", "this-scope-trace"])
        self.assertEqual(len(self.policy("approvalList", response, None)["approvals"]), 7)

    def test_hermes_allows_what_was_asked_and_leaves_money_and_standing_choices_to_leon(self):
        allow = self.policy("approvalDecide", PENDING, "ap-1", "allow_once", False)
        self.assertEqual(allow["params"], {"approval_id": "ap-1", "decision": "allow_once"})
        self.assertIn("Leon decides", self.policy("approvalDecide", PENDING, "ap-1", "allow_always", False)["error"])
        always = self.policy("approvalDecide", PENDING, "ap-1", "allow_always", True)["params"]
        self.assertEqual((always["always_scope"], always["allow_always_scope"]),
                         ("destination_domain", "destination_domain"))
        self.assertIn("money", self.policy("approvalDecide", PENDING, "ap-2", "allow_once", False)["error"])
        self.assertIn("params", self.policy("approvalDecide", PENDING, "ap-2", "allow_once", True))
        self.assertIn("params", self.policy("approvalDecide", PENDING, "ap-2", "deny", False))
        self.assertIn("does not offer", self.policy("approvalDecide", PENDING, "ap-2", "deny_always", True)["error"])
        self.assertIn("No pending approval", self.policy("approvalDecide", PENDING, "ap-3", "allow_once", True)["error"])

    def test_risky_app_methods_wait_for_leon(self):
        for method in ("bootstrap.reset", "egress.permissions.hitl", "permissions.settings.update",
                       "credentials.delete", "ssh.operator.enable", "email.mailbox.delete"):
            self.assertIn("Ask Leon first", self.policy("methodGuard", method, False), method)
            self.assertIsNone(self.policy("methodGuard", method, True), method)
        for method in ("tasks.list", "egress.permissions", "permissions.settings", "session.rename",
                       "credentials.list", "egress.approval.history"):
            self.assertIsNone(self.policy("methodGuard", method, False), method)
        self.assertIn("muse_approve", self.policy("methodGuard", "egress.approval.decide", True))

    def test_methods_are_found_by_what_they_do(self):
        found = self.policy("methodSearch", list(museweb.ROUTES.values()), "scheduled tasks")
        self.assertEqual(found["methods"][0]["method"], "tasks.list")
        self.assertIn("tasks.run", found["methods"][0]["note"])
        self.assertGreater(found["total"], 400)
        clipped = self.policy("clipAnswer", [{"text": "x" * 400}] * 10, 1000)
        self.assertEqual((clipped["truncated"], clipped["shown"], clipped["of"]), (True, 2, 10))


class MuseAppToolTests(ToolTests):
    async def asyncSetUp(self):
        self.gateway = FakeGateway()
        self.gateway.answers[("GET", "/approvals")] = (200, "application/json", json.dumps(PENDING).encode())
        self.gateway.answers[("POST", "/approvals/ap-1/decide")] = (
            200, "application/json", b'{"approval": {"approval_id": "ap-1", "status": "allowed"}}')
        self.gateway.answers[("GET", "/tasks")] = (
            200, "application/json", b'{"ok": true, "result": {"schedules": []}}')
        self.gateway.answers[("GET", "/tasks/runs?limit=100")] = (
            200, "application/json", b'{"runs": []}')
        self.gateway.answers[("GET", "/skills")] = (
            200, "application/json", b'{"ok": false, "error": {"code": "not_ready"}}')
        self.gateway.answers[("GET", "/media/raw/workspace/a.png")] = (200, "image/png", b"\x89PNG")

        def signed_in(directory):
            web = museweb.MuseWeb(directory, self.gateway)
            web.login.update(museweb.parse_cookies("hatch_sess=first"))
            return web

        patcher = patch.object(plugin, "MuseWeb", signed_in)
        patcher.start()
        self.addCleanup(patcher.stop)
        await super().asyncSetUp()
        constants = SimpleNamespace(get_hermes_home=lambda: self.directory)
        patcher = patch.dict(sys.modules, {"hermes_constants": constants})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_waiting_tools_report_a_pending_approval(self):
        self.assertEqual(self.call("muse_handoff", task="Check the App Store review")["status"], "not_delivered")
        now = relay_module.time.time()
        with self.store.connection() as db:
            db.execute("UPDATE handoffs SET approvals=1, approvals_at=?, approvals_wanted=?", (now, now - 1))
        result = self.call("muse_wait")
        self.assertEqual(result["status"], "waiting_for_approval")
        self.assertEqual([approval["approval"] for approval in result["approvals"]], ["ap-1"])
        self.assertIn("muse_approve", result["next"])
        self.assertNotIn("Beeper", result["next"])
        # Answering or reporting clears the old count so the next muse_wait does not bounce on it.
        self.assertEqual(self.call("muse_wait")["status"], "not_delivered")

    async def test_stale_approval_count_from_another_chat_does_not_block_muse_wait(self):
        other_only = {"pending_approvals": [
            {"approval_id": "ap-other", "status": "pending", "session_id": "another-chat",
             "reason": "Other side chat"},
        ]}
        self.gateway.answers[("GET", "/approvals")] = (200, "application/json", json.dumps(other_only).encode())
        self.assertEqual(self.call("muse_handoff", task="Check the paper")["status"], "not_delivered")
        now = relay_module.time.time()
        with self.store.connection() as db:
            db.execute("UPDATE handoffs SET approvals=1, approvals_at=?, approvals_wanted=?", (now, now - 1))
        result = self.call("muse_wait")
        self.assertEqual(result["status"], "not_delivered")
        self.assertNotIn("approvals", result)
        self.assertNotIn("approvals_pending", result)

    async def test_approvals_tool_rejects_an_unknown_handoff_id(self):
        self.assertIn("No handoff found", self.call("muse_approvals", handoff="unknown")["error"])

    async def test_approve_tool_clears_cached_approvals_for_the_handoff(self):
        self.call("muse_handoff", task="Sign in to apple.com")
        now = relay_module.time.time()
        with self.store.connection() as db:
            db.execute("UPDATE handoffs SET approvals=1, approvals_at=?, approvals_wanted=?", (now, now - 1))
        self.assertEqual(self.call("muse_approve", approval="ap-1", decision="allow_once")["status"], "allowed")
        self.assertIsNone(self.store.approvals(JOB)["count"])
        self.assertEqual(self.call("muse_wait")["status"], "not_delivered")

    async def test_hermes_answers_an_approval_and_works_the_app_as_leon(self):
        self.assertEqual(len(self.call("muse_approvals")["approvals"]), 2)
        self.assertIn("Leon decides", self.call("muse_approve", approval="ap-2", decision="allow_once")["error"])
        result = self.call("muse_approve", approval="ap-1", decision="allow_once")
        self.assertEqual((result["decided"], result["status"]), ("allow_once", "allowed"))
        self.assertEqual(self.gateway.requests[-1],
                         (1, "POST", "/approvals/ap-1/decide", {"approval_id": "ap-1", "decision": "allow_once"}))
        self.assertEqual(self.call("muse_app", method="tasks.list"), {"schedules": []})
        self.assertIn("Ask Leon first", self.call("muse_app", method="bootstrap.reset")["error"])
        self.assertIn("no method", self.call("muse_app", method="tasks.explode")["error"])
        self.assertEqual(self.call("muse_app", method="goals.list")["error"], "Muse refused it (404).")
        self.assertIn("not_ready", self.call("muse_app", method="skills.list")["muse_says"])
        self.assertEqual(self.call("muse_app_methods", query="approval history")["methods"][0]["method"],
                         "egress.approval.history")
        saved = self.call("muse_file", path="sandbox://workspace/a.png")
        self.assertEqual((Path(saved["saved"]).read_bytes(), saved["type"]), (b"\x89PNG", "image/png"))

    async def test_only_leons_own_requests_reach_the_app(self):
        requests = len(self.gateway.requests)
        for change in ({"user_id": "UOTHER"}, {"platform": "telegram"}):
            self.session.update(change)
            for name, args in (("muse_approvals", {}), ("muse_app", {"method": "tasks.list"}),
                               ("muse_approve", {"approval": "ap-1", "decision": "allow_once"}),
                               ("muse_file", {"path": "workspace/a.png"})):
                self.assertIn("only work on Leon's own requests", self.call(name, **args)["error"])
        self.assertEqual(len(self.gateway.requests), requests)
        # A job with no chat platform, such as a scheduled one, is Leon's own.
        self.session.update(platform="", user_id="")
        self.assertEqual(self.call("muse_app", method="tasks.list"), {"schedules": []})

    async def test_a_missing_login_says_how_to_sign_in(self):
        self.gateway.login = "ended"
        # A previously issued gateway token can still be used until it expires.
        self.adapter._hermes22muse_relay.web.tokens = None
        self.assertIn("Leon has to sign in again", self.call("muse_approvals")["error"])
