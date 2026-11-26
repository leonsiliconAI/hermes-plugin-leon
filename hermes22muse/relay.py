"""Persist each handoff's Slack thread, Muse side chat, transcript and delivery queues."""

import asyncio
import fcntl
import hashlib
import logging
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from .._bridge import acall_with_host, call
from .muse import STAMP
from .progress import Progress

LOG = logging.getLogger(__name__)
MAX_WORKERS = 8
# Muse counts as done after a completed reply and this long without new rows or unfinished replies,
# as muse-mcp's own muse_chat_ask does: agentic work can continue after a first answer.
QUIET = 8
# How often the relay looks at Hermes's muse.ai login, which it extends every few hours.
LOGIN_CHECK = 900
# How often the relay sends Muse's scheduled tasks to slack22linear, through autolinear's Worker, so
# Leon's Routine projects in Linear follow his routines in Muse (his rule, 2026-11-25).
ROUTINES_PUSH = 600
ROUTINES_URL = "https://autolinear.leonsilicon.ai/routines"
# autolinear's Worker, which slack22linear's reminders go through (HERMES22MUSE_AUTOLINEAR_URL
# overrides it). The relay reports the issue channels with a running Hermes turn every HEARTBEAT
# seconds, so a turn a gateway restart dropped gets a reminder (LEON-509, 2026-11-25).
AUTOLINEAR_URL = "https://autolinear.leonsilicon.ai"
HEARTBEAT = 60
# How often the relay looks for Muse answers that no running turn will read, and how long it leaves
# them for a turn: after Muse's last change and after the chat's last turn ended.
TURN_CHECK = 10
ANSWER_GRACE = 20
# Answers older than this are left alone.
UNREAD_WINDOW = 6 * 3600
# A handoff idle for a day stops polling (policy.ts pollDelay).
SLEEP = 10 * 365 * 86400


def operation_id(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "hermes22muse/" + "/".join(parts)))


def slack_chunks(text):
    yield from call(__file__, "slackChunks", text)


class Store:
    def __init__(self, directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = directory
        self.path = directory / "relay.db"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS handoffs (
                    id TEXT PRIMARY KEY, team TEXT NOT NULL, channel TEXT NOT NULL,
                    request TEXT NOT NULL, actor TEXT NOT NULL, root TEXT,
                    root_attempt REAL, due REAL NOT NULL DEFAULT 0, activity REAL NOT NULL,
                    failures INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(team, channel, root)
                );
                CREATE INDEX IF NOT EXISTS handoffs_due ON handoffs(due);
                CREATE TABLE IF NOT EXISTS inbound (
                    handoff TEXT NOT NULL, ts TEXT NOT NULL, text TEXT,
                    attempted INTEGER NOT NULL DEFAULT 0, receipt TEXT,
                    PRIMARY KEY(handoff, ts)
                );
                CREATE TABLE IF NOT EXISTS outbound (
                    seq INTEGER PRIMARY KEY, handoff TEXT NOT NULL, event TEXT NOT NULL,
                    part INTEGER NOT NULL, text TEXT,
                    UNIQUE(handoff, event, part)
                );
                CREATE TABLE IF NOT EXISTS muse_dm_threads (
                    handoff TEXT PRIMARY KEY, channel TEXT NOT NULL, root TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transcript (
                    seq INTEGER PRIMARY KEY, handoff TEXT NOT NULL, id TEXT NOT NULL,
                    role TEXT NOT NULL, text TEXT NOT NULL,
                    UNIQUE(handoff, id)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(handoffs)")}
            # approvals: Muse's pending approval count for the side chat, measured at approvals_at
            # when Hermes asked at approvals_wanted (NULL until measured).
            for column in ("busy INTEGER NOT NULL DEFAULT 0", "polled REAL NOT NULL DEFAULT 0",
                           "changed REAL NOT NULL DEFAULT 0", "approvals INTEGER",
                           "approvals_at REAL NOT NULL DEFAULT 0",
                           "approvals_wanted REAL NOT NULL DEFAULT 0"):
                if column.split()[0] not in columns:
                    db.execute(f"ALTER TABLE handoffs ADD COLUMN {column}")
            # collected: the transcript row through which a tool returned Muse's answer to Hermes.
            # nudged: the row through which the relay told autolinear nobody read it. Answers from
            # before this release count as read.
            if "collected" not in columns:
                db.execute("ALTER TABLE handoffs ADD COLUMN collected INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE handoffs SET collected=COALESCE("
                           "(SELECT MAX(seq) FROM transcript t WHERE t.handoff=handoffs.id), 0)")
            if "nudged" not in columns:
                db.execute("ALTER TABLE handoffs ADD COLUMN nudged INTEGER NOT NULL DEFAULT 0")
        self.progress = Progress(self.connection)

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def create(self, team, channel, request, thread, actor, muse_text, thread_text):
        """Start a handoff for one Slack request. Returns its ID, or None if one already exists."""
        job_id = operation_id(team, channel, request)
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM handoffs WHERE id=? OR (team=? AND channel=? AND root=?)",
                          (job_id, team, channel, thread)).fetchone():
                return None
            db.execute(
                """INSERT INTO handoffs(id, team, channel, request, actor, root, activity)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, team, channel, request, actor, thread, now),
            )
            db.execute("INSERT INTO inbound(handoff, ts, text) VALUES (?, ?, ?)",
                       (job_id, request, muse_text))
            self._add_output(db, job_id, "handoff", list(slack_chunks(thread_text)))
            if channel.startswith(("C", "G")):
                self.progress.report(db, (team, channel, thread), "Muse", "", request,
                                     "Assigned. Waiting for the first reply.")
        return job_id

    def peer_report(self, team, channel, report):
        route = (team, channel, report["root"])
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.progress.report(db, route, report["agent"], report["ts"], report["revision"], report["text"])
            if not report.get("muse"):
                return False
            job = db.execute("SELECT id FROM handoffs WHERE team=? AND channel=? AND root=?", route).fetchone()
            if job is None:
                return False
            # One Slack reply forwards once. An edit updates progress but never reassigns the task.
            key = f"h{report['ts']}-peer"
            sent = db.execute("INSERT OR IGNORE INTO inbound(handoff, ts, text) VALUES (?, ?, ?)",
                              (job["id"], key, report["muse"])).rowcount
            if sent:
                db.execute("UPDATE handoffs SET due=0, activity=? WHERE id=?", (time.time(), job["id"]))
            return bool(sent)

    def follow_up(self, job_id, text, thread_text):
        """Queue a message for Muse and show it in the handoff's thread. Returns its key."""
        key = f"h{time.time():.6f}"
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO inbound(handoff, ts, text) VALUES (?, ?, ?)", (job_id, key, text))
            self._add_output(db, job_id, "hermes/" + key, list(slack_chunks(thread_text)))
            db.execute("UPDATE handoffs SET due=0, activity=? WHERE id=?", (time.time(), job_id))
        return key

    def handoff(self, job_id):
        with self.connection() as db:
            return db.execute("SELECT * FROM handoffs WHERE id=?", (job_id,)).fetchone()

    def find(self, team, channel, request=None, thread=None):
        """The handoff started by a Slack message, or the one whose thread a message is in."""
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM handoffs WHERE team=? AND channel=? AND (request=? OR root=?)",
                (team, channel, request or "", thread or ""),
            ).fetchone()

    def touch(self, job_id):
        with self.connection() as db:
            db.execute("UPDATE handoffs SET activity=?, due=MIN(due, ?) WHERE id=?",
                       (time.time(), time.time() + 1, job_id))

    def want_approvals(self, job_id):
        """Ask the relay to measure Muse's pending approvals for this handoff on its next pass."""
        now = time.time()
        with self.connection() as db:
            db.execute("UPDATE handoffs SET approvals_wanted=?, activity=?, due=MIN(due, ?) WHERE id=?",
                       (now, now, now, job_id))

    def approvals(self, job_id):
        with self.connection() as db:
            row = db.execute("SELECT approvals, approvals_at, approvals_wanted, changed FROM handoffs WHERE id=?",
                             (job_id,)).fetchone()
        if row is None:
            return {"count": None, "checked": 0, "wanted": 0, "changed": 0}
        return {"count": row["approvals"], "checked": row["approvals_at"],
                "wanted": row["approvals_wanted"], "changed": row["changed"]}

    def measured_approvals(self, job_id, count, wanted=None):
        with self.connection() as db:
            if wanted is None:
                db.execute("UPDATE handoffs SET approvals=?, approvals_at=? WHERE id=?",
                           (count, time.time(), job_id))
            else:
                db.execute("UPDATE handoffs SET approvals=?, approvals_at=? WHERE id=? AND approvals_wanted=?",
                           (count, time.time(), job_id, wanted))

    def clear_approvals(self, job_id):
        with self.connection() as db:
            db.execute(
                "UPDATE handoffs SET approvals=NULL, approvals_at=0, approvals_wanted=0, due=0 WHERE id=?",
                (job_id,),
            )

    def answer(self, job_id):
        """Muse's messages after Hermes's latest delivery once Muse has finished, else the state."""
        with self.connection() as db:
            job = db.execute("SELECT busy, polled, changed FROM handoffs WHERE id=?", (job_id,)).fetchone()
            last = db.execute(
                "SELECT receipt FROM inbound WHERE handoff=? ORDER BY rowid DESC LIMIT 1", (job_id,),
            ).fetchone()
            receipt = last["receipt"] if last else None
            sent = db.execute("SELECT seq FROM transcript WHERE handoff=? AND id=?",
                              (job_id, receipt)).fetchone()
            rows = db.execute(
                "SELECT role, text FROM transcript WHERE handoff=? AND seq>? ORDER BY seq",
                (job_id, sent["seq"] if sent else 2 ** 62),
            ).fetchall()
            answer = call(__file__, "answerRows", dict(job) if job else None, receipt,
                          [dict(row) for row in rows], QUIET)
            return answer["status"], answer["messages"]


    def mark_collected(self, job_id):
        """A tool returned Muse's finished answer to Hermes."""
        with self.connection() as db:
            db.execute("UPDATE handoffs SET collected=MAX(collected, COALESCE("
                       "(SELECT MAX(seq) FROM transcript WHERE handoff=?), 0)) WHERE id=?", (job_id, job_id))

    def unread_answers(self, actor, since):
        """Channel handoffs whose newest Muse reply, changed since `since`, Hermes hasn't read."""
        with self.connection() as db:
            return db.execute(
                """SELECT * FROM (
                       SELECT h.*, (SELECT MAX(seq) FROM transcript t
                                    WHERE t.handoff=h.id AND t.role='assistant') AS latest
                       FROM handoffs h
                       WHERE h.actor=? AND h.root IS NOT NULL AND substr(h.channel, 1, 1) IN ('C', 'G')
                             AND h.changed>=?
                   ) WHERE latest > MAX(collected, nudged)""",
                (actor, since),
            ).fetchall()

    def mark_nudged(self, job_id, seq):
        with self.connection() as db:
            db.execute("UPDATE handoffs SET nudged=MAX(nudged, ?) WHERE id=?", (seq, job_id))

    def due(self, actor, running=()):
        with self.connection() as db:
            placeholders = ",".join("?" for _ in running)
            return db.execute(
                "SELECT * FROM handoffs WHERE actor=? AND due<=? AND root IS NOT NULL "
                f"AND id NOT IN ({placeholders}) ORDER BY due LIMIT ?",
                (actor, time.time(), *running, MAX_WORKERS - len(running)),
            ).fetchall()

    def next_inbound(self, job_id):
        with self.connection() as db:
            return db.execute(
                """SELECT i.* FROM inbound i JOIN handoffs h ON h.id=i.handoff
                   WHERE i.handoff=? AND i.receipt IS NULL
                   ORDER BY i.ts != h.request, i.ts LIMIT 1""",
                (job_id,),
            ).fetchone()

    def attempted(self, job_id, ts):
        with self.connection() as db:
            db.execute("UPDATE inbound SET attempted=1 WHERE handoff=? AND ts=?", (job_id, ts))

    def accepted(self, job_id, ts, receipt):
        with self.connection() as db:
            db.execute(
                "UPDATE inbound SET receipt=?, text=NULL WHERE handoff=? AND ts=?",
                (receipt, job_id, ts),
            )

    def collect(self, job_id, messages, full, busy=False):
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            echoed = {
                row["receipt"] for row in db.execute(
                    "SELECT receipt FROM inbound WHERE handoff=? AND receipt IS NOT NULL", (job_id,),
                )
            }
            ids = [message.id for message in messages]
            marks = ",".join("?" for _ in ids)
            known = [{"id": row["id"], "text": row["text"]} for row in db.execute(
                f"SELECT id, text FROM transcript WHERE handoff=? AND id IN ({marks})", (job_id, *ids),
            )]
            # Handoffs from 1.x relayed messages before the transcript existed.
            posted = {row["event"] for row in db.execute(
                f"SELECT DISTINCT event FROM outbound WHERE handoff=? AND event IN ({marks})", (job_id, *ids),
            )}
            old = db.execute("SELECT busy FROM handoffs WHERE id=?", (job_id,)).fetchone()
            plan = call(
                __file__, "collectPlan", [vars(message) for message in messages], known,
                list(posted), list(echoed), full, busy, bool(old["busy"]) if old else None, STAMP,
            )
            db.executemany(
                "INSERT OR IGNORE INTO transcript(handoff, id, role, text) VALUES (?, ?, ?, ?)",
                ((job_id, row["id"], row["role"], row["text"]) for row in plan["rows"]),
            )
            db.executemany(
                "UPDATE transcript SET text=? WHERE handoff=? AND id=?",
                ((row["text"], job_id, row["id"]) for row in plan["updates"]),
            )
            for item in plan["output"]:
                self._add_output(db, job_id, item["id"], item["parts"])
            db.execute(
                "UPDATE handoffs SET busy=?, polled=?, changed=CASE WHEN ? THEN ? ELSE changed END,"
                " activity=CASE WHEN ? THEN ? ELSE activity END WHERE id=?",
                (int(busy), now, plan["changed"], now, plan["activity"], now, job_id),
            )

    @staticmethod
    def _add_output(db, job_id, event, parts):
        changed = False
        for part, text in enumerate(parts):
            changed |= db.execute(
                "INSERT OR IGNORE INTO outbound(handoff, event, part, text) VALUES (?, ?, ?, ?)",
                (job_id, event, part, text),
            ).rowcount > 0
        return changed

    def pending_output(self, job_id):
        with self.connection() as db:
            return db.execute(
                """WITH pending AS (
                       SELECT o.seq, o.event, o.part, o.text, t.text AS report_text,
                              COALESCE(t.role='assistant', instr(o.text, 'Muse' || char(10))=1) AS from_muse
                       FROM outbound o LEFT JOIN transcript t ON t.handoff=o.handoff AND t.id=o.event
                       WHERE o.handoff=? AND o.text IS NOT NULL
                   ), batches AS (
                       SELECT *, row_number() OVER (PARTITION BY from_muse ORDER BY seq) AS position
                       FROM pending
                   )
                   SELECT * FROM batches WHERE position<=4 ORDER BY seq""",
                (job_id,),
            ).fetchall()

    def muse_dm_thread(self, job_id):
        with self.connection() as db:
            return db.execute("SELECT channel, root FROM muse_dm_threads WHERE handoff=?", (job_id,)).fetchone()

    def delivered(self, seq, muse_dm_root=None, report=None):
        with self.connection() as db:
            if muse_dm_root is not None:
                db.execute("INSERT INTO muse_dm_threads(handoff, channel, root) VALUES (?, ?, ?)", muse_dm_root)
            if report is not None:
                route, ts, text = report
                self.progress.report(db, route, "Muse", ts, ts, text.removeprefix("Muse\n"))
            db.execute("UPDATE outbound SET text=NULL WHERE seq=?", (seq,))

    def schedule(self, job, retry=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            if retry is None:
                active = db.execute("SELECT activity FROM handoffs WHERE id=?", (job["id"],)).fetchone()[0]
                pending = (self._has_pending(db, job["id"], "inbound")
                           or self._has_pending(db, job["id"], "outbound"))
                delay = call(__file__, "pollDelay", now - active, bool(pending), SLEEP)
                failures = 0
            else:
                delay = retry
                failures = job["failures"] + 1
            db.execute("UPDATE handoffs SET due=?, failures=? WHERE id=?", (now + delay, failures, job["id"]))

    @staticmethod
    def _has_pending(db, job_id, table):
        # The text is cleared only after the destination acknowledges delivery.
        return db.execute(
            f"SELECT 1 FROM {table} WHERE handoff=? AND text IS NOT NULL LIMIT 1",
            (job_id,),
        ).fetchone()


class MuseSlackError(RuntimeError):
    pass


class Slack:
    def __init__(self, adapter, muse_token):
        self.adapter = adapter
        self._muse_token = muse_token
        self._muse_client = None
        self._muse_identity = None
        self._muse_bot_id = None
        self._auth_lock = asyncio.Lock()

    def _new_muse_client(self):
        from slack_sdk.web.async_client import AsyncWebClient

        return AsyncWebClient(token=self._muse_token)

    async def muse_client(self, team, hermes_client):
        if not self._muse_token.startswith("xoxb-"):
            raise MuseSlackError("set SLACK22MUSE_BOT_TOKEN to Muse's bot token")
        if self._muse_token == getattr(hermes_client, "token", None):
            raise MuseSlackError("SLACK22MUSE_BOT_TOKEN belongs to Hermes")
        async with self._auth_lock:
            if self._muse_identity is None:
                client = self._new_muse_client()
                auth = await asyncio.wait_for(client.auth_test(), 15)
                if not (auth.get("ok") and auth.get("team_id") and auth.get("user_id") and auth.get("bot_id")):
                    raise MuseSlackError("SLACK22MUSE_BOT_TOKEN must authenticate a workspace bot")
                self._muse_identity = (auth["team_id"], auth["user_id"])
                self._muse_bot_id = auth["bot_id"]
                self._muse_client = client
            muse_team, muse_user = self._muse_identity
            if muse_team != team:
                raise MuseSlackError("Muse is installed in a different Slack workspace")
            if muse_user == getattr(self.adapter, "_team_bot_user_ids", {}).get(team):
                raise MuseSlackError("SLACK22MUSE_BOT_TOKEN belongs to Hermes")
            return self._muse_client

    def is_muse_event(self, event):
        source = event.source
        if self._muse_identity is None or getattr(source.platform, "value", source.platform) != "slack":
            return False
        team, user = self._muse_identity
        return source.scope_id == team and (
            source.user_id == user or (event.raw_message or {}).get("bot_id") == self._muse_bot_id
        )

    def client(self, job):
        channel, team = job["channel"], job["team"]
        if self.adapter._outbound_blocked(channel, "hermes22muse send to"):
            raise RuntimeError("Slack delivery blocked")
        if team not in self.adapter._team_clients:
            raise RuntimeError("Slack workspace is disconnected")
        return self.adapter._get_client(channel, team_id=team)

    async def post(self, job, text, key, from_muse, muse_dm, *, blocks=None):
        channel, thread = job["channel"], job["root"]
        client = self.client(job)
        if from_muse:
            client = await self.muse_client(job["team"], client)
            text = text.removeprefix("Muse\n")
            if muse_dm is not None:
                channel, thread = muse_dm
            elif channel.startswith("D"):
                # Slack only lets the participants post to a one-to-one DM.
                text = call(__file__, "museDmOpening", text, channel, thread)
                dm = await asyncio.wait_for(client.conversations_open(users=job["actor"]), 15)
                channel = (dm.get("channel") or {}).get("id", "")
                if not dm.get("ok") or not channel.startswith("D") or channel == job["channel"]:
                    raise MuseSlackError("Slack did not open a separate Muse DM")
                thread = None
        response = await asyncio.wait_for(client.chat_postMessage(
            channel=channel, text=text, thread_ts=thread, mrkdwn=False, link_names=False,
            unfurl_links=False, unfurl_media=False, client_msg_id=key,
            **({"blocks": blocks} if blocks else {}),
        ), 15)
        if not response.get("ok") or not response.get("ts") or response.get("channel") != channel:
            raise RuntimeError("Slack returned no matching send receipt")
        # Replies in the handoff thread reach Hermes even where channels require a mention.
        register = getattr(self.adapter, "_register_mentioned_thread", None)
        if channel == job["channel"] and callable(register):
            register(thread, job["team"])
        ts = response["ts"]
        return ts, (job["id"], channel, ts) if thread is None else None


class Relay:
    def __init__(self, store, slack, muse, actor, web=None, turns=None, post=None):
        self.store, self.slack, self.muse, self.actor, self.web = store, slack, muse, actor, web
        self.turns = turns
        # post(url, payload, headers) -> HTTP status, for autolinear's Worker. Tests pass a fake.
        self._post = post
        self._unread_retry = {}

    async def pending_approvals(self, muse, job_id):
        """Muse's pending approvals for a side chat: from its app gateway, else muse-mcp's snapshot."""
        if self.web is not None and self.web.signed_in():
            try:
                answer = await asyncio.to_thread(self.web.call, "egress.approvals")
                return len(call(__file__, "approvalList", answer["body"], job_id)["approvals"])
            except Exception as exc:
                LOG.warning("hermes22muse: could not read approvals from the Muse app (%s)", exc or type(exc).__name__)
        return await muse.pending_approvals(job_id)

    async def keep_login(self):
        """Extend Hermes's muse.ai login while it is valid, and tell the user once when it ends."""
        from .museweb import MuseLoginRequired

        login = self.web.login
        try:
            saved = login.read()
            if not (saved.get("cookies") or {}).get("hatch_sess") or saved.get("told"):
                return
            if not saved.get("expired"):
                await asyncio.to_thread(self.web.keep_alive)
                return
        except MuseLoginRequired:
            pass
        except Exception as exc:
            LOG.warning("hermes22muse: could not extend the muse.ai login (%s)", exc or type(exc).__name__)
            return
        team, client = next(iter(self.slack.adapter._team_clients.items()), (None, None))
        if client is None:
            return
        opened = await asyncio.wait_for(client.conversations_open(users=self.actor), 15)
        await asyncio.wait_for(client.chat_postMessage(
            channel=opened["channel"]["id"], text=call(__file__, "loginEndedNotice", self.actor),
        ), 15)
        login.update({}, told=int(time.time()))
        LOG.warning("hermes22muse: muse.ai login ended; told the user")

    async def push_routines(self, post=None):
        """Send Muse's scheduled tasks to slack22linear, which keeps a Routine project in Linear for
        each routine. Needs Hermes's muse.ai login and AutoLinear's token. Returns how many it sent."""
        token = os.environ.get("AUTOLINEAR_TOKEN", "").strip()
        if not token or self.web is None or not self.web.signed_in():
            return None
        answer = await asyncio.to_thread(self.web.call, "tasks.list")
        body = answer.get("body")
        schedules = body.get("schedules") if isinstance(body, dict) else None
        if not isinstance(schedules, list):
            raise ValueError("Muse's tasks.list had no schedules")
        payload = {"at": int(time.time() * 1000), "schedules": schedules,
                   "invalid": body.get("invalid") or []}
        url = os.environ.get("HERMES22MUSE_ROUTINES_URL", "").strip() or ROUTINES_URL
        if post is None:
            import httpx

            async def post(url, payload, headers):
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(url, json=payload, headers=headers)
                    response.raise_for_status()

        await post(url, payload, {"Authorization": f"Bearer {token}"})
        return len(schedules)

    async def sync_routines(self):
        """Every ROUTINES_PUSH seconds, beside the handoffs: a slow Muse never holds them up."""
        await asyncio.sleep(60)
        while True:
            try:
                await asyncio.wait_for(self.push_routines(), 180)
            except Exception as exc:
                LOG.warning("hermes22muse: could not send Muse's routines to Linear (%s)",
                            exc or type(exc).__name__)
            await asyncio.sleep(ROUTINES_PUSH)

    async def post_autolinear(self, path, payload):
        """POST to autolinear's Worker with Hermes's token. Its HTTP status, or None without a token."""
        token = os.environ.get("AUTOLINEAR_TOKEN", "").strip()
        if not token:
            return None
        url = (os.environ.get("HERMES22MUSE_AUTOLINEAR_URL", "").strip() or AUTOLINEAR_URL).rstrip("/") + path
        headers = {"Authorization": f"Bearer {token}"}
        if self._post is not None:
            return await self._post(url, payload, headers)
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            return (await client.post(url, json=payload, headers=headers)).status_code

    async def report_turns(self):
        """Tell autolinear which issue channels have a running turn, so it doesn't remind Hermes there.
        The report goes out with no channels too: autolinear only trusts a missing channel while
        reports keep coming, since a gateway that has lost track of its turns sends none."""
        channels = sorted(chat for chat in self.turns.running() if chat.startswith(("C", "G")))
        status = await self.post_autolinear("/turns", {"channels": channels})
        if status is not None and not 200 <= status < 300:
            LOG.warning("hermes22muse: autolinear refused the running turns (%s)", status)

    async def report_unread(self):
        """Ask autolinear to wake Hermes for each Muse answer that no running turn will read: Hermes
        didn't wait for it, or a gateway restart dropped the turn that was waiting (LEON-509)."""
        now = time.time()
        for job in self.store.unread_answers(self.actor, now - UNREAD_WINDOW):
            idle = self.turns.idle(job["channel"])
            if (idle is None or idle < ANSWER_GRACE or now - job["changed"] < ANSWER_GRACE
                    or self._unread_retry.get(job["id"], 0) > time.monotonic()):
                continue
            status, messages = self.store.answer(job["id"])
            if status != "answered":
                continue
            if any(message["from"] != "muse" for message in messages):
                # Leon wrote in the side chat himself, and Muse answered him there.
                self.store.mark_nudged(job["id"], job["latest"])
                continue
            code = await self.post_autolinear("/muse-answer", {
                "id": f"{job['id']}:{job['latest']}", "channel": job["channel"], "thread": job["root"],
                "handoff": job["id"], "sideChat": call(__file__, "sideChat", job["id"]),
            })
            if code is None:
                return
            if 200 <= code < 300 or code == 400:
                self.store.mark_nudged(job["id"], job["latest"])
                LOG.info("hermes22muse: asked autolinear to wake Hermes for Muse's answer in %s (%s)",
                         job["id"], code)
            else:
                self._unread_retry[job["id"]] = time.monotonic() + 60
                LOG.warning("hermes22muse: autolinear refused Muse's unread answer in %s (%s)", job["id"], code)

    async def watch_turns(self):
        beat = float("-inf")
        while True:
            await asyncio.sleep(TURN_CHECK)
            if not self.slack.adapter._running:
                continue
            if time.monotonic() - beat >= HEARTBEAT:
                beat = time.monotonic()
                try:
                    await asyncio.wait_for(self.report_turns(), 30)
                except Exception as exc:
                    LOG.warning("hermes22muse: could not report running turns (%s)", exc or type(exc).__name__)
            try:
                await asyncio.wait_for(self.report_unread(), 60)
            except Exception as exc:
                LOG.warning("hermes22muse: could not report unread Muse answers (%s)", exc or type(exc).__name__)

    async def send_to_muse(self, muse, job, entry):
        async def history(job_id):
            messages, _, _ = await muse.history(job_id)
            return [vars(message) for message in messages]

        await acall_with_host(__file__, "sendInbound", {
            "register": muse.ensure_registered, "exists": muse.exists,
            "history": history, "attempted": self.store.attempted,
            "accepted": self.store.accepted, "send": muse.send,
        }, dict(job), dict(entry), STAMP, immediate={
            "byteLength": lambda text: len(text.encode("utf-8")),
            "clipUtf8": lambda text, limit: text.encode("utf-8")[:int(limit)].decode("utf-8", errors="ignore"),
        })

    @staticmethod
    def retry(job, exc):
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", {}) or {}
        retry_after = str(headers.get("Retry-After", headers.get("retry-after", "")))
        retry = int(call(__file__, "retryDelay", job["failures"], retry_after))
        detail = str(exc) if isinstance(exc, MuseSlackError) else type(exc).__name__
        code = (getattr(response, "data", None) or {}).get("error")
        if code in {"channel_not_found", "not_in_channel", "missing_scope", "invalid_auth", "token_revoked"}:
            detail += f": {code}"
        LOG.warning("hermes22muse: handoff %s retry in %ss (%s)", job["id"], retry, detail)
        return retry

    async def flush_slack(self, job):
        # Keep the Muse rows pending if its Slack app cannot post. Hermes's posts can still go out.
        muse_retry = None
        for item in self.store.pending_output(job["id"]):
            from_muse = bool(item["from_muse"])
            if from_muse and muse_retry is not None:
                continue
            key = operation_id(job["id"], hashlib.sha256(item["event"].encode()).hexdigest(), str(item["part"]))
            muse_dm = self.store.muse_dm_thread(job["id"]) if from_muse and job["channel"].startswith("D") else None
            try:
                ts, new_dm_root = await self.slack.post(job, item["text"], key, from_muse, muse_dm)
            except Exception as exc:
                if not from_muse:
                    raise
                muse_retry = self.retry(job, exc)
            else:
                report = None
                if from_muse and item["part"] == 0 and job["channel"].startswith(("C", "G")):
                    report = ((job["team"], job["channel"], job["root"]), ts,
                              item["report_text"] or item["text"])
                self.store.delivered(item["seq"], new_dm_root, report)
        return muse_retry

    async def publish_progress(self):
        while True:
            if self.slack.adapter._running:
                try:
                    await self.store.progress.flush(self.slack)
                except sqlite3.Error:
                    LOG.exception("Slack task progress is temporarily unavailable")
            await asyncio.sleep(2)

    async def tick(self, job):
        job_id = job["id"]
        retry = None
        try:
            # Deliver durable thread posts even when the Muse connection is down.
            retry = await self.flush_slack(job)
            async with asyncio.timeout(45):
                async with self.muse.connect() as muse:
                    entry = self.store.next_inbound(job_id)
                    if entry:
                        await self.send_to_muse(muse, job, entry)
                    messages, full, busy = await muse.history(job_id)
                    self.store.collect(job_id, messages, full, busy)
                    wanted = self.store.approvals(job_id)
                    if wanted["wanted"] > wanted["checked"]:
                        self.store.measured_approvals(
                            job_id, await self.pending_approvals(muse, job_id), wanted["wanted"],
                        )
        except Exception as exc:
            retry = max(retry or 0, self.retry(job, exc))
        self.store.schedule(job, retry=retry)

    async def run(self):
        with (self.store.directory / "relay.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                LOG.warning("hermes22muse: another gateway owns the relay")
                return
            running = set()
            login_checked = 0
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self.publish_progress())
                if self.web is not None:
                    tasks.create_task(self.sync_routines())
                if self.turns is not None:
                    tasks.create_task(self.watch_turns())
                while True:
                    if self.web is not None and time.monotonic() - login_checked > LOGIN_CHECK:
                        login_checked = time.monotonic()
                        try:
                            await self.keep_login()
                        except Exception as exc:
                            LOG.warning("hermes22muse: muse.ai login check failed (%s)", exc or type(exc).__name__)
                    if self.slack.adapter._running and len(running) < MAX_WORKERS:
                        for job in self.store.due(self.actor, running):
                            job_id = job["id"]
                            running.add(job_id)
                            task = tasks.create_task(self.tick(job))
                            task.add_done_callback(lambda _, key=job_id: running.discard(key))
                    await asyncio.sleep(1)
