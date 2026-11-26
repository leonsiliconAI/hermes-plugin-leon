"""The Slack chats Hermes is running a turn in, from its turn hooks.

A gateway restart drops the turns it was running without ending them (LEON-509, 2026-11-25). The
relay reports the chats with a running turn to autolinear, and asks it to wake Hermes when Muse
answers a handoff that no running turn will read. Hermes calls these hooks for every tool call,
so they only touch memory and never raise.
"""

import threading
import time

# A turn with no hook for this long has ended without saying so. Hermes stops a tool call after
# 420 seconds.
STALE = 600


class Turns:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        # (session ID, turn ID) -> [chat ID, last hook]
        self._running = {}
        # chat ID -> when a turn there last ended
        self._ended = {}

    def start(self, session_id, turn_id, chat):
        if session_id and chat:
            with self._lock:
                self._running[(session_id, turn_id or "")] = [chat, self._clock()]

    def seen(self, session_id):
        with self._lock:
            for (session, _), turn in self._running.items():
                if session == session_id:
                    turn[1] = self._clock()

    def end(self, session_id, turn_id):
        with self._lock:
            keys = [key for key in self._running
                    if key[0] == session_id and (not turn_id or key[1] in (turn_id, ""))]
            for key in keys:
                self._ended[self._running.pop(key)[0]] = self._clock()

    def running(self):
        """The chats with a running turn."""
        now = self._clock()
        with self._lock:
            for key, (chat, seen) in list(self._running.items()):
                if now - seen > STALE:
                    del self._running[key]
                    self._ended[chat] = now
            return {chat for chat, _ in self._running.values()}

    def idle(self, chat):
        """Seconds since a turn in `chat` last ended, or None while one is running there."""
        if chat in self.running():
            return None
        with self._lock:
            ended = self._ended.get(chat)
        return float("inf") if ended is None else self._clock() - ended
