"""The niclydon/muse-mcp contract, backed by Meta's paired Gadget SDK."""

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from .._bridge import call

CALLER = "hermes22muse"
STAMP = f"⟦via muse-mcp · from {CALLER}⟧ "
HISTORY_LIMIT = 50


class MuseError(Exception):
    pass


@dataclass(frozen=True)
class Message:
    id: str
    role: str
    text: str


def history_messages(result, chat_id):
    """Project completed conversation rows from the Link's response envelope.

    Returns the messages in the Link's chronological order, whether the read window was full,
    and whether Muse was still writing a reply when the window was read.
    """
    history = call(__file__, "history", result, chat_id, HISTORY_LIMIT)
    if "error" in history:
        raise MuseError(history["error"])
    return [Message(**row) for row in history["messages"]], history["full"], history["busy"]


class MuseChat:
    def __init__(self, session):
        self.session = session

    async def call(self, tool, **args):
        result = await self.session.call_tool(tool, arguments=args)
        if result.is_error:
            raise MuseError(f"{tool} returned an MCP error")
        payload = result.structured_content
        if payload is None and len(result.content) == 1 and result.content[0].type == "text":
            # The pinned server declares no output schema and sends dictionaries as JSON text.
            try:
                payload = json.loads(result.content[0].text)
            except ValueError:
                raise MuseError(f"{tool} returned invalid JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            raise MuseError(f"{tool} returned an invalid payload")
        return payload

    async def ensure_registered(self, chat_id):
        current = await self.call("muse_directory_get", chat=chat_id)
        action = call(__file__, "directoryRegistration", current)
        if "error" in action:
            raise MuseError(action["error"])
        if "revision" not in action:
            return
        result = await self.call(
            "muse_directory_set", chat_id=chat_id, base_revision=action["revision"],
            name=f"Hermes {chat_id}", kind="side",
            purpose="Linked to a Hermes Slack handoff", **{"from": CALLER},
        )
        if not result["ok"]:
            raise MuseError("side chat registration failed")

    async def exists(self, chat_id):
        result = await self.call("muse_chat_exists", chat=chat_id, **{"from": CALLER})
        if not result["ok"] or not isinstance(result.get("exists"), bool):
            raise MuseError("side chat existence unknown")
        return result["exists"]

    async def send(self, chat_id, text):
        result = await self.call("muse_chat_send", chat=chat_id, text=text, **{"from": CALLER})
        identifier = result.get("message_id")
        if (not result["ok"] or result.get("session_id") != chat_id
                or not isinstance(identifier, str) or not identifier):
            raise MuseError("side chat send returned no matching receipt")
        return identifier

    async def pending_approvals(self, chat_id):
        """How many approvals Muse is waiting on in this chat, from a short live snapshot; None if unknown."""
        result = await self.call(
            "muse_chat_status", chat=chat_id, history_limit=1, snapshot_seconds=3, **{"from": CALLER},
        )
        return call(__file__, "mcpPendingApprovals", result)

    async def history(self, chat_id):
        result = await self.call(
            "muse_chat_history", chat=chat_id, limit=HISTORY_LIMIT, **{"from": CALLER},
        )
        return history_messages(result, chat_id)


class Muse:
    def __init__(self, url, secret):
        endpoint = urlsplit(url)
        loopback = endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment
                or not (endpoint.scheme == "https" or endpoint.scheme == "http" and loopback)):
            raise ValueError("muse-mcp needs a private HTTPS URL or loopback HTTP URL")
        if len(secret) < 32:
            raise ValueError("muse-mcp needs its origin secret (at least 32 characters)")
        self.url, self.secret = url, secret

    @asynccontextmanager
    async def connect(self):
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(
            headers={"X-Origin-Secret": self.secret}, timeout=20, trust_env=False,
        ) as http:
            async with streamable_http_client(self.url, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield MuseChat(session)
