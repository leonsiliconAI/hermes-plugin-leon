"""Give each Linear task its own Chrome in Leon's Grok Bot cloud computer, as Hermes's browser.

Hermes's browser tools ask the active cloud browser provider for a session per task. This plugin's
provider (`browser.cloud_provider: grok-box`) asks the grok-box-browser broker in the Grok box for the
Chrome of the current Linear issue, through a loopback forwarder over Hermes Cloud's userspace
Tailscale, and hands back its DevTools websocket. See README.md.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

from .._bridge import call

LOG = logging.getLogger(__name__)
PROVIDER_NAME = "grok-box"
RETIRE_INTERVAL = 3600


def _env(name, default=""):
    return (os.getenv(name) or default).strip()


def _env_file_value(path, name):
    """One value from a dotenv file, or "" when the file or the key is missing."""
    try:
        with open(path) as handle:
            for line in handle:
                key, sep, value = line.strip().partition("=")
                if sep and key.strip() == name:
                    return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _hermes_home():
    return _env("HERMES_HOME", "/opt/data")


class Config:
    def __init__(self):
        home = _hermes_home()
        self.token = _env("GROK_BOX_BROWSER_TOKEN")
        self.host = _env("GROK_BOX_BROWSER_HOST", "grok-box")
        self.port = int(_env("GROK_BOX_BROWSER_PORT", "9400"))
        self.local_port = int(_env("GROK_BOX_BROWSER_LOCAL_PORT", "19400"))
        self.tailscale = _env("GROK_BOX_TAILSCALE", f"{home}/orca-device/tailscale/bin/tailscale")
        self.socket = _env("GROK_BOX_TAILSCALE_SOCKET", f"{home}/orca-device/tailscale/tailscaled.sock")
        self.linear_key = _env("LINEAR_API_KEY")
        # Shared with slack22grokbot's keeper: repair-request.json from here, repair-status.json back.
        self.shared = _env("GROK_BOX_SHARED_DIR", f"{home}/grok-box-browser")
        # grokbot-admin's /sandbox/ensure: wakes the Grok Bot cloud computer when Grok Bot paused it.
        self.wake_url = _env("GROK_BOX_WAKE_URL", "https://grokbot-admin.leonsilicon.ai/sandbox/ensure")
        # grokbot-admin's own bearer token, which slack22grokbot (in beeperd) already holds on this host.
        self.wake_token = _env("GROK_BOX_WAKE_TOKEN") or _env_file_value(
            _env("GROK_BOX_WAKE_TOKEN_FILE", f"{home}/beeperd/slack22grokbot/slack22grokbot.env"),
            "GROK_BOT_ROSTER_MCP_TOKEN")

    def dial(self):
        return [self.tailscale, f"--socket={self.socket}", "nc", self.host, str(self.port)]


class BoxUnreachable(Exception):
    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


class Forwarder:
    """127.0.0.1:<local_port> → the broker, one `tailscale nc` per connection.

    Userspace Tailscale gives Hermes Cloud no route to tailnet addresses (the Orca device works the
    same way). A loopback address also passes Chrome's DevTools Host check. When the port is taken,
    another copy of this plugin (an older one, before a live reload's restart) already forwards it.
    """

    def __init__(self, port, dial, popen=subprocess.Popen):
        self.port = port
        self.dial = dial
        self.popen = popen
        self._lock = threading.Lock()
        self._listener = None
        self.shared = False
        # When this forwarder last carried a connection or data: a browser is in use.
        self.last_activity = 0.0

    def ensure(self):
        with self._lock:
            if self._listener is not None or self.shared:
                return
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", self.port))
            except OSError as exc:
                listener.close()
                if exc.errno != errno.EADDRINUSE:
                    raise
                self.shared = True
                return
            listener.listen(64)
            self._listener = listener
            threading.Thread(target=self._accept, name="grok-box-forwarder", daemon=True).start()

    def close(self):
        with self._lock:
            if self._listener is not None:
                self._listener.close()
                self._listener = None

    def _accept(self):
        while True:
            listener = self._listener
            if listener is None:
                return
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            self.last_activity = time.monotonic()
            threading.Thread(target=self._carry, args=(connection,), daemon=True).start()

    def _carry(self, connection):
        try:
            child = self.popen(self.dial(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, bufsize=0)
        except OSError:
            LOG.warning("grok-box-browser: couldn't start tailscale nc", exc_info=True)
            connection.close()
            return

        def upstream():
            try:
                while True:
                    data = connection.recv(65536)
                    if not data:
                        break
                    child.stdin.write(data)
                    self.last_activity = time.monotonic()
            except OSError:
                pass
            finally:
                try:
                    child.stdin.close()
                except OSError:
                    pass

        threading.Thread(target=upstream, daemon=True).start()
        try:
            while True:
                data = os.read(child.stdout.fileno(), 65536)
                if not data:
                    break
                connection.sendall(data)
                self.last_activity = time.monotonic()
        except OSError:
            pass
        finally:
            connection.close()
            try:
                child.kill()
            except OSError:
                pass


class Waker:
    """Wakes the Grok Bot cloud computer through grokbot-admin's `/sandbox/ensure`.

    Grok Bot pauses that machine when no bot is running, even while Hermes uses a Chrome there
    (2026-11-26: paused about 30 s after a task's Chrome started, back in half a second on
    EnsureSandBox, every process intact). Never raises."""

    MIN_INTERVAL = 60

    def __init__(self, config, opener=urllib.request.urlopen, clock=time.monotonic):
        self.config = config
        self.opener = opener
        self.clock = clock
        self._last = None

    def configured(self):
        return bool(self.config.wake_url and self.config.wake_token)

    def wake(self, force=False):
        """The run state Cursor reports, or None when not configured, rate-limited or failed."""
        if not self.configured() or (not force and self._last is not None
                                     and self.clock() - self._last < self.MIN_INTERVAL):
            return None
        self._last = self.clock()
        request = urllib.request.Request(self.config.wake_url, data=b"{}", method="POST", headers={
            "Authorization": f"Bearer {self.config.wake_token}", "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=20) as response:
                return json.loads(response.read() or b"{}").get("runState")
        except (urllib.error.URLError, OSError, ValueError):
            LOG.warning("grok-box-browser: couldn't wake the Grok box", exc_info=True)
            return None


class Broker:
    """The broker's API through the forwarder."""

    # After a wake, how long to wait for the broker before asking for a repair.
    WAKE_WAIT = 45

    def __init__(self, config, forwarder, opener=urllib.request.urlopen, waker=None, sleep=time.sleep):
        self.config = config
        self.forwarder = forwarder
        self.opener = opener
        self.waker = waker
        self.sleep = sleep

    def healthy(self):
        try:
            with self.opener(urllib.request.Request(f"http://127.0.0.1:{self.config.local_port}/health"),
                             timeout=5) as response:
                return bool(json.loads(response.read() or b"{}").get("ok"))
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def request(self, method, path, body=None, timeout=60):
        try:
            return self._request(method, path, body, timeout)
        except BoxUnreachable as exc:
            # Usually a pause: wake the box, wait for the broker, and try once more.
            if self.waker is not None and self.waker.wake(force=True) is not None:
                waited = 0
                while waited < self.WAKE_WAIT:
                    if self.healthy():
                        try:
                            return self._request(method, path, body, timeout)
                        except BoxUnreachable:
                            break
                    self.sleep(3)
                    waited += 3
            raise RuntimeError(call(__file__, "unreachableMessage", exc.detail, self.request_repair(),
                                    int(time.time() * 1000))) from None

    def _request(self, method, path, body, timeout):
        self.forwarder.ensure()
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.config.local_port}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.config.token}", "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}").get("error") or exc.reason
            except ValueError:
                detail = exc.reason
            raise RuntimeError(f"grok-box broker answered {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BoxUnreachable(str(getattr(exc, "reason", exc)) or type(exc).__name__) from None

    def request_repair(self):
        """Ask slack22grokbot to have a Grok bot rerun the box setup; returns its last repair status.

        The relay (in Hermes's Beeper inbox, on this host) reads the request within a minute, and
        also adds the setup to the next message any Grok bot gets while the box is down."""
        shared = self.config.shared
        try:
            os.makedirs(shared, exist_ok=True)
            temporary = os.path.join(shared, "repair-request.json.tmp")
            with open(temporary, "w") as handle:
                json.dump({"at": int(time.time() * 1000)}, handle)
            os.replace(temporary, os.path.join(shared, "repair-request.json"))
        except OSError:
            LOG.warning("grok-box-browser: couldn't leave a repair request", exc_info=True)
        try:
            with open(os.path.join(shared, "repair-status.json")) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None


def current_task(task_id):
    """The browser key for this turn: its Linear issue when it runs in an AutoLinear channel."""
    values = {}
    try:
        from gateway.session_context import get_session_env

        for name in ("platform", "chat_name", "chat_id"):
            values[name] = get_session_env(f"HERMES_SESSION_{name.upper()}") or ""
    except Exception:
        LOG.debug("grok-box-browser: no gateway session context", exc_info=True)
    return call(__file__, "taskKey", values.get("platform", ""), values.get("chat_name", ""),
                values.get("chat_id", ""), str(task_id or ""))


def make_provider_class(base):
    class GrokBoxBrowserProvider(base):
        """`browser.cloud_provider: grok-box`: one Chrome per Linear issue in the Grok box."""

        def __init__(self, broker):
            self.broker = broker

        @property
        def name(self):
            return PROVIDER_NAME

        @property
        def display_name(self):
            return "Grok box (one Chrome per Linear issue)"

        def is_available(self):
            return bool(self.broker.config.token)

        def get_setup_schema(self):
            return {"name": self.display_name, "badge": "", "tag": "",
                    "env_vars": [{"key": "GROK_BOX_BROWSER_TOKEN", "prompt": "grok-box-browser broker token",
                                  "url": ""}]}

        def create_session(self, task_id):
            if not self.broker.config.token:
                raise ValueError("GROK_BOX_BROWSER_TOKEN is not set")
            task = current_task(task_id)
            if self.broker.waker is not None:
                self.broker.waker.wake()  # cheap while it's awake; at most once a minute
            opened = self.broker.request("POST", "/v1/sessions", {"task": task}, timeout=90)
            info = call(__file__, "sessionInfo", task, self.broker.config.local_port, opened["ws"],
                        int(opened.get("seeded") or 0))
            LOG.info("grok-box-browser: %s uses the box browser %s", task_id, task)
            return info

        def close_session(self, session_id):
            # The box keeps the Chrome for 30 idle minutes and the profile until the issue is done.
            try:
                self.broker.request("POST", f"/v1/sessions/{session_id}/release", {}, timeout=15)
                return True
            except Exception:
                LOG.debug("grok-box-browser: release failed for %s", session_id, exc_info=True)
                return False

        def emergency_cleanup(self, session_id):
            return None

    return GrokBoxBrowserProvider


def linear_state(identifier, key, opener=urllib.request.urlopen):
    query = {"query": "query($id: String!) { issue(id: $id) { state { type } } }", "variables": {"id": identifier}}
    request = urllib.request.Request("https://api.linear.app/graphql", data=json.dumps(query).encode(),
                                     headers={"Authorization": key, "Content-Type": "application/json"})
    with opener(request, timeout=20) as response:
        data = json.loads(response.read())
    issue = (data.get("data") or {}).get("issue") or {}
    return ((issue.get("state") or {}).get("type")) or ""


def retire_finished(broker, state=linear_state):
    """Tell the box which issues are done, so their profiles go a week later. Returns the tasks retired."""
    retired = []
    for entry in broker.request("GET", "/v1/sessions", timeout=20).get("sessions", []):
        task = entry.get("task", "")
        identifier = call(__file__, "issueIdentifier", task)
        if not identifier or entry.get("retiredAt"):
            continue
        try:
            if call(__file__, "shouldRetire", state(identifier, broker.config.linear_key)):
                broker.request("POST", f"/v1/sessions/{task}/retire", {}, timeout=20)
                retired.append(task)
        except Exception:
            LOG.debug("grok-box-browser: couldn't check %s", identifier, exc_info=True)
    return retired


def _retire_loop(broker):
    while True:
        time.sleep(RETIRE_INTERVAL)
        try:
            retired = retire_finished(broker)
            if retired:
                LOG.info("grok-box-browser: retired %s", ", ".join(retired))
        except Exception:
            LOG.debug("grok-box-browser: retire pass failed", exc_info=True)


KEEPALIVE_EVERY = 180
KEEPALIVE_ACTIVE = 300


def keepalive_once(forwarder, waker, now):
    """Wake the box if a browser carried traffic recently, so Grok Bot doesn't pause it mid-task."""
    if forwarder.last_activity and now - forwarder.last_activity < KEEPALIVE_ACTIVE:
        return waker.wake(force=True)
    return None


def _keepalive_loop(forwarder, waker):
    while True:
        time.sleep(KEEPALIVE_EVERY)
        try:
            keepalive_once(forwarder, waker, time.monotonic())
        except Exception:
            LOG.debug("grok-box-browser: keepalive failed", exc_info=True)


def register(ctx):
    from agent.browser_provider import BrowserProvider

    config = Config()
    forwarder = Forwarder(config.local_port, config.dial)
    waker = Waker(config)
    broker = Broker(config, forwarder, waker=waker)
    ctx.register_browser_provider(make_provider_class(BrowserProvider)(broker))
    if waker.configured() and not any(thread.name == "grok-box-keepalive" for thread in threading.enumerate()):
        threading.Thread(target=_keepalive_loop, args=(forwarder, waker), name="grok-box-keepalive",
                         daemon=True).start()
    # One retire loop per process: a live plugin reload registers this plugin again.
    if config.token and config.linear_key and not any(
            thread.name == "grok-box-retire" for thread in threading.enumerate()):
        threading.Thread(target=_retire_loop, args=(broker,), name="grok-box-retire", daemon=True).start()
    LOG.info("grok-box-browser: provider %s registered%s", PROVIDER_NAME,
             "" if config.token else " (GROK_BOX_BROWSER_TOKEN missing)")
