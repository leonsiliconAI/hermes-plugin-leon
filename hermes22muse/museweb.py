"""Muse's own app gateway: the calls the Muse web app makes, made with the user's saved muse.ai login.

muse.ai trades the login cookies for a short-lived token, then the app talks to the user's personal
Muse computer over a WebSocket carrying a Noise XX channel with small protobuf envelopes. The wire
format and the method table (`muse-routes.json`) follow nikships/muse-cli's protocol notes (MIT).
"""

import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import struct
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

LOG = logging.getLogger(__name__)
SITE = "https://muse.ai"
GATEWAY = "wss://hatch.metaaivm.com/v1/noise"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
SERVICES = {"daemon": 0, "sentinel": 1, "vault": 2, "authd": 3, "ssh_access": 4, "yolk": 5}
LOGIN_COOKIE = "hatch_sess"
# muse.ai's tokens outlive this; a refused one is fetched again.
TOKEN_SECONDS = 600
# muse.ai extends the login for 30 days whenever /api/session is read.
RENEW_SECONDS = 6 * 3600
MAX_BODY = 32 * 1024 * 1024
RECONNECT_SECONDS = 10
NOISE_MAX = 65535
ROUTES = {route["method"]: route for route in json.loads(
    (Path(__file__).with_name("muse-routes.json")).read_text())}


class MuseWebError(Exception):
    pass


class MuseLoginRequired(MuseWebError):
    """There is no saved muse.ai login, or muse.ai no longer accepts it."""


class MuseApiError(MuseWebError):
    def __init__(self, status, body):
        super().__init__(f"Muse answered {status}: {body[:300]}")
        self.status, self.body = status, body


# --- protobuf: the handful of fixed messages in noise_transport.proto and noise_envelope.proto ---

def _varint(value):
    value &= (1 << 64) - 1
    out = bytearray()
    while value > 0x7F:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(number, value):
    if isinstance(value, (bytes, bytearray)):
        return _varint(number << 3 | 2) + _varint(len(value)) + bytes(value)
    return _varint(number << 3) + _varint(int(value))


def _fields(data):
    """Each (field number, value) of a message: an int for varints, bytes for length-delimited."""
    position = 0
    while position < len(data):
        key = shift = 0
        while True:
            byte = data[position]
            position += 1
            key |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                break
        number, kind = key >> 3, key & 7
        if kind == 0:
            value = shift = 0
            while True:
                byte = data[position]
                position += 1
                value |= (byte & 0x7F) << shift
                shift += 7
                if byte < 0x80:
                    break
        elif kind == 2:
            length = shift = 0
            while True:
                byte = data[position]
                position += 1
                length |= (byte & 0x7F) << shift
                shift += 7
                if byte < 0x80:
                    break
            value = data[position:position + length]
            if len(value) != length:
                raise MuseWebError("Muse sent a truncated frame")
            position += length
        elif kind == 1:
            value, position = data[position:position + 8], position + 8
        elif kind == 5:
            value, position = data[position:position + 4], position + 4
        else:
            raise MuseWebError("Muse sent a frame this client cannot read")
        yield number, value


def encode_request(stream, service, verb, path, headers, body):
    request = _field(1, verb.encode()) + _field(2, path.encode())
    for key, value in headers:
        request += _field(3, _field(1, key.encode()) + _field(2, value.encode()))
    if body:
        request += _field(4, body)
    request += _field(5, 1)
    frame = _field(1, stream) + _field(2, request)
    envelope = (_field(1, service) if service else b"") + _field(2, frame)
    chunk = struct.unpack("<q", os.urandom(8))[0]
    return _field(1, chunk) + _field(2, 0) + _field(3, 1) + _field(4, envelope)


def decode_frame(payload):
    """A ServiceResponse as (stream, kind, fields): response, body_chunk or reset."""
    frame = b""
    for number, value in _fields(payload):
        if number == 1:
            frame = value
    stream, kind, content = 0, None, {}
    for number, value in _fields(frame):
        if number == 1:
            stream = value
        elif number == 3:
            kind, content = "response", {"status": 0, "headers": {}, "body": b"", "end": False}
            for part, item in _fields(value):
                if part == 1:
                    content["status"] = item
                elif part == 2:
                    pair = dict(_fields(item))
                    content["headers"][bytes(pair.get(1, b"")).decode().lower()] = bytes(pair.get(2, b"")).decode()
                elif part == 3:
                    content["body"] = bytes(item)
                elif part == 4:
                    content["end"] = bool(item)
        elif number == 4:
            pair = dict(_fields(value))
            kind, content = "body_chunk", {"body": bytes(pair.get(1, b"")), "end": bool(pair.get(2, 0))}
        elif number == 5:
            pair = dict(_fields(value))
            kind, content = "reset", {"code": pair.get(1, 0), "reason": bytes(pair.get(2, b"")).decode("utf-8", "replace")}
    return stream, kind, content


# --- Noise_XX_25519_AESGCM_SHA256, initiator side, empty handshake payloads ---

class Noise:
    NAME = b"Noise_XX_25519_AESGCM_SHA256"

    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        self.static, self.ephemeral = X25519PrivateKey.generate(), X25519PrivateKey.generate()
        self.hash = self.NAME.ljust(32, b"\0")
        self.chain = self.hash
        self._mix_hash(b"")
        self.key, self.nonce = None, 0
        self.sending = self.receiving = None

    @staticmethod
    def _public(key):
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def _mix_hash(self, data):
        self.hash = hashlib.sha256(self.hash + data).digest()

    def _hkdf(self, material):
        temporary = hmac.new(self.chain, material, hashlib.sha256).digest()
        first = hmac.new(temporary, b"\x01", hashlib.sha256).digest()
        return first, hmac.new(temporary, first + b"\x02", hashlib.sha256).digest()

    def _mix_key(self, material):
        self.chain, self.key = self._hkdf(material)
        self.nonce = 0

    @staticmethod
    def _seal(key, nonce, data, associated, opening=False):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        cipher, counter = AESGCM(key), b"\0" * 4 + struct.pack(">Q", nonce)
        return cipher.decrypt(counter, data, associated) if opening else cipher.encrypt(counter, data, associated)

    def _handshake_seal(self, data, opening=False):
        if self.key is None:
            result = data
        else:
            result = self._seal(self.key, self.nonce, data, self.hash, opening)
            self.nonce += 1
        self._mix_hash(data if opening or self.key is None else result)
        return result

    def first(self):
        public = self._public(self.ephemeral)
        self._mix_hash(public)
        return public + self._handshake_seal(b"")

    def second(self, message):
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

        if len(message) < 96:
            raise MuseWebError("Muse's gateway sent a short handshake")
        self._mix_hash(message[:32])
        self.remote = X25519PublicKey.from_public_bytes(message[:32])
        self._mix_key(self.ephemeral.exchange(self.remote))
        server = X25519PublicKey.from_public_bytes(self._handshake_seal(message[32:80], opening=True))
        self._mix_key(self.ephemeral.exchange(server))
        self._handshake_seal(message[80:], opening=True)

    def third(self):
        message = self._handshake_seal(self._public(self.static))
        self._mix_key(self.static.exchange(self.remote))
        message += self._handshake_seal(b"")
        self.sending, self.receiving = ([key, 0] for key in self._hkdf(b""))
        return message

    def encrypt(self, data):
        key, nonce = self.sending
        self.sending[1] += 1
        return self._seal(key, nonce, data, b"")

    def decrypt(self, data):
        key, nonce = self.receiving
        self.receiving[1] += 1
        return self._seal(key, nonce, data, b"", opening=True)


# --- the saved muse.ai login ---

class Login:
    """The muse.ai cookies, kept in one private file and rewritten whenever muse.ai reissues them."""

    def __init__(self, directory):
        self.path = Path(directory) / "muse-login.json"

    def read(self):
        try:
            saved = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except ValueError:
            raise MuseLoginRequired(f"{self.path.name} is not valid JSON") from None
        cookies = saved.get("cookies") if isinstance(saved, dict) else None
        if not isinstance(cookies, dict):
            raise MuseLoginRequired(f"{self.path.name} has no cookies")
        return saved

    def cookies(self):
        saved = self.read()
        cookies = saved.get("cookies") or {}
        if not cookies.get(LOGIN_COOKIE):
            raise MuseLoginRequired("no muse.ai login is saved for Hermes")
        if saved.get("expired"):
            raise MuseLoginRequired("muse.ai no longer accepts Hermes's saved login")
        return cookies

    def update(self, issued, **facts):
        """Merge cookies muse.ai just set ({name: (value, expires or None)}) and other facts."""
        saved = self.read()
        cookies, expires = saved.setdefault("cookies", {}), saved.setdefault("expires", {})
        for name, (value, expiry) in issued.items():
            if value and not (expiry and expiry < time.time()):
                cookies[name] = value
                if expiry:
                    expires[name] = int(expiry)
            elif name != LOGIN_COOKIE:
                # muse.ai clears cookies when it refuses a login. The login cookie itself is kept,
                # so one wrong refusal does not sign Hermes out for good.
                cookies.pop(name, None)
                expires.pop(name, None)
        saved.update(facts)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".new")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as file:
            json.dump(saved, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.path)
        return saved


def parse_cookies(text):
    """Cookies from a browser's `a=b; c=d` line, a JSON object, or an agent-browser cookie list."""
    text = text.strip()
    if text.startswith(("{", "[")):
        data = json.loads(text)
        # agent-browser wraps its list as {"data": {"cookies": […]}}.
        for key in ("data", "cookies"):
            if isinstance(data, dict) and isinstance(data.get(key), (dict, list)):
                data = data[key]
        if isinstance(data, dict):
            return {str(name): (str(value), None) for name, value in data.items()}
        issued = {}
        for cookie in data:
            # Subdomain cookies are not sent to muse.ai's own API.
            if str(cookie.get("domain", "")).lstrip(".") == "muse.ai":
                expiry = cookie.get("expires") or 0
                issued[str(cookie["name"])] = (str(cookie["value"]), expiry if expiry > 0 else None)
        return issued
    pairs = (pair.split("=", 1) for pair in text.removeprefix("Cookie:").split(";") if "=" in pair)
    return {name.strip(): (value.strip(), None) for name, value in pairs}


def build_request(method, params):
    """The (service, verb, path, body) the web app sends for a method and its parameters."""
    route = ROUTES.get(method)
    if route is None:
        raise MuseWebError(f"unknown Muse method {method!r}")
    params = dict(params or {})
    verb, used = route["http"], set()

    def fill(match):
        wildcard, name = match.group(1), match.group(2)
        if name not in params:
            raise MuseWebError(f"{method} needs the parameter {name!r}")
        used.add(name)
        return quote(str(params[name]), safe="/" if wildcard else "")

    path = re.sub(r"\{(\*?)(\w+)\}", fill, route["path"])
    body = b""
    if verb == "GET":
        query = urlencode({name: value for name, value in params.items()
                           if name not in used and value is not None}, doseq=True)
        if query:
            path += ("&" if "?" in path else "?") + query
    elif route.get("requestBody") != "empty":
        body = json.dumps(params).encode()
    return SERVICES[route.get("service", "daemon")], verb, path, body


class Channel:
    """One open connection to the personal Muse computer."""

    def __init__(self, socket):
        self.socket, self.noise, self.stream, self.partial = socket, Noise(), 1, {}
        socket.send(self.noise.first())
        self.noise.second(socket.receive(20))
        socket.send(self.noise.third())

    def close(self):
        self.socket.close()

    def _frame(self, deadline):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Muse did not answer in time")
            data = self.noise.decrypt(self.socket.receive(remaining))
            fields = dict(_fields(data))
            payload, total = bytes(fields.get(4, b"")), fields.get(3, 0) or 1
            if total > 1:
                # A reply longer than one Noise message arrives in parts that share a chunk ID.
                parts = self.partial.setdefault(fields.get(1, 0), {})
                parts[fields.get(2, 0)] = payload
                if len(parts) < total:
                    continue
                del self.partial[fields.get(1, 0)]
                payload = b"".join(parts[index] for index in range(total))
            return decode_frame(payload)

    def open(self, service, verb, path, body):
        stream, self.stream = self.stream, self.stream + 1
        headers = [("x-request-id", str(uuid.uuid4()))]
        if body:
            headers.append(("content-type", "application/json"))
        frame = encode_request(stream, service, verb, path, headers, body)
        if len(frame) > NOISE_MAX - 16:
            raise MuseWebError("the request is too large for Muse's gateway")
        self.socket.send(self.noise.encrypt(frame))
        return stream

    def exchange(self, service, verb, path, body, timeout, listen=None, limit=MAX_BODY):
        """Send one request and return (status, headers, body).

        `listen` reads an event stream for that many seconds instead of waiting for its end.
        """
        stream = self.open(service, verb, path, body)
        deadline = time.monotonic() + (listen if listen is not None else timeout)
        status, headers, parts, size = None, {}, [], 0
        while True:
            try:
                number, kind, content = self._frame(deadline)
            except TimeoutError:
                if listen is not None and status is not None:
                    break
                raise
            if number != stream:
                continue
            if kind == "reset":
                raise MuseWebError(f"Muse reset the request: {content['reason'] or content['code']}")
            if kind == "response":
                status, headers = content["status"], content["headers"]
            elif status is None:
                continue
            parts.append(content["body"])
            size += len(content["body"])
            if size > limit:
                raise MuseWebError("Muse's answer is larger than this client accepts")
            if content["end"]:
                break
        return status, headers, b"".join(parts)


class CurlTransport:
    """muse.ai and its gateway through curl_cffi, which presents a browser's TLS handshake."""

    def http(self, verb, url, headers, body=None):
        from curl_cffi import requests

        response = requests.request(verb, url, headers=headers, data=body, impersonate="chrome",
                                    timeout=20, allow_redirects=False)
        issued = {cookie.name: (cookie.value, cookie.expires) for cookie in response.cookies.jar}
        return response.status_code, response.text, issued

    def socket(self, url):
        from curl_cffi.requests import WebSocket

        return _CurlSocket(WebSocket(), url)


class _CurlSocket:
    def __init__(self, socket, url):
        self.socket = socket
        # curl_cffi's WebSocket does not verify certificates unless told to.
        socket.connect(url, impersonate="chrome", timeout=20, verify=True)

    def send(self, data):
        self.socket.send_bytes(data)

    def receive(self, timeout):
        from curl_cffi.requests.websockets import WebSocketTimeout

        try:
            data, _ = self.socket.recv(timeout=max(timeout, 0.05))
        except WebSocketTimeout:
            raise TimeoutError("Muse did not answer in time") from None
        return bytes(data)

    def close(self):
        try:
            self.socket.close()
        except Exception:
            pass


class PlainTransport:
    """muse.ai and its gateway through the HTTP and WebSocket libraries Hermes already has."""

    def http(self, verb, url, headers, body=None):
        import httpx

        response = httpx.request(verb, url, headers=headers, content=body, timeout=20,
                                 follow_redirects=False, trust_env=False)
        issued = {cookie.name: (cookie.value, cookie.expires) for cookie in response.cookies.jar}
        return response.status_code, response.text, issued

    def socket(self, url):
        from websockets.sync.client import connect

        # Entered by hand: the connection outlives this call and is closed by the channel.
        return _PlainSocket(connect(url, origin=SITE, user_agent_header=USER_AGENT, open_timeout=20,
                                    max_size=None, proxy=None).__enter__())


class _PlainSocket:
    def __init__(self, socket):
        self.socket = socket

    def send(self, data):
        self.socket.send(data)

    def receive(self, timeout):
        data = self.socket.recv(timeout=max(timeout, 0.05))
        return data if isinstance(data, bytes) else data.encode()

    def close(self):
        try:
            self.socket.close()
        except Exception:
            pass


def default_transport():
    """curl_cffi where it is installed, since muse.ai is built for browsers; otherwise the plain one."""
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return PlainTransport()
    return CurlTransport()


class MuseWeb:
    """Calls Muse's app methods as the user. Safe to share between threads: one call at a time."""

    def __init__(self, directory, transport=None):
        self.login = Login(directory)
        self.transport = transport or default_transport()
        self.thread_lock = threading.Lock()
        self.tokens = None  # (computer, gateway token, fetched)

    @property
    @contextmanager
    def lock(self):
        """One user of the login at a time, across threads and Hermes processes: muse.ai can
        replace the login cookie in an answer, and a second reader would still send the old one."""
        with self.thread_lock:
            self.login.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(self.login.path.with_name("muse-login.lock"), "a") as file:
                fcntl.flock(file, fcntl.LOCK_EX)
                yield

    def signed_in(self):
        try:
            return bool(self.login.cookies())
        except MuseLoginRequired:
            return False

    def keep_alive(self):
        """Extend the login if it has not been extended lately. Returns the seconds since it was."""
        with self.lock:
            age = time.time() - (self.login.read().get("renewed") or 0)
            if age > RENEW_SECONDS:
                self.renew()
                return 0
            return age

    def _site(self, verb, path, body=None, bearer=None):
        headers = {
            "User-Agent": USER_AGENT, "Referer": SITE + "/", "Origin": SITE,
            "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
            "Accept": "application/json", "Content-Type": "application/json",
            "Cookie": "; ".join(f"{name}={value}" for name, value in self.login.cookies().items()),
        }
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        status, text, issued = self.transport.http(
            verb, SITE + path, headers, b"" if body is None else json.dumps(body).encode())
        if issued:
            # muse.ai can replace the login cookie in any answer and drop the old one: save it first.
            self.login.update(issued)
        if status == 401 and path == "/api/session":
            # Only the session read settles it (a 401 elsewhere can be a stale token), and only
            # when muse.ai says so twice.
            refusals = (self.login.read().get("refusals") or 0) + 1
            self.login.update({}, refusals=refusals, **({"expired": int(time.time())} if refusals > 1 else {}))
            raise MuseLoginRequired("muse.ai no longer accepts Hermes's saved login")
        if status != 200:
            raise MuseApiError(status, text)
        try:
            return json.loads(text)
        except ValueError:
            raise MuseWebError(f"muse.ai answered {path} with something other than JSON") from None

    def renew(self):
        """Read the session, which extends the login, and return the Muse computer's ID."""
        for _ in range(6):
            session = self._site("GET", "/api/session")
            computer = session.get("vm_id")
            if computer:
                self.login.update({}, renewed=int(time.time()), computer=computer, refusals=0)
                return computer
            # muse.ai answers "unavailable" for a moment while the computer restarts.
            time.sleep(5)
        raise MuseWebError(f"muse.ai has no Muse computer for this login ({session.get('status')!r})")

    def wake(self, computer):
        """Ask muse.ai to start a sleeping Muse computer."""
        try:
            self._site("POST", "/api/hatch/vm/wake", {
                "vm_id": computer, "retry_count": 0, "connect_attempt_id": str(uuid.uuid4())})
        except MuseApiError as exc:
            LOG.info("hermes22muse: muse.ai did not wake the Muse computer (%s)", exc.status)

    def _connect(self):
        if self.tokens is None or time.time() - self.tokens[2] > TOKEN_SECONDS:
            saved = self.login.read()
            computer = saved.get("computer")
            if not computer or time.time() - saved.get("renewed", 0) > RENEW_SECONDS:
                computer = self.renew()
            access = self._site("POST", "/api/auth/check")["access_token"]
            token = self._site("POST", "/api/hatch/token", {
                "vmAddress": f"wss://{computer}.metaaivm.com/", "vmName": computer}, bearer=access)["token"]
            self.tokens = (computer, token, time.time())
        computer, token, _ = self.tokens
        return Channel(self.transport.socket(f"{GATEWAY}?vm_id={computer}&auth_token={quote(token, safe='')}"))

    def channel(self):
        """An open channel. After a failure the login is read again, the computer is woken, and
        fresh tokens are tried for about a minute."""
        try:
            return self._connect()
        except MuseLoginRequired:
            raise
        except Exception as exc:
            LOG.info("hermes22muse: reconnecting to Muse's gateway (%s)", type(exc).__name__)
        self.wake(self.renew())
        for attempt in range(6):
            self.tokens = None
            try:
                return self._connect()
            except MuseLoginRequired:
                raise
            except Exception:
                if attempt == 5:
                    raise
                time.sleep(RECONNECT_SECONDS)

    def call(self, method, params=None, timeout=None, listen=None):
        """Call one method. Returns {status, type, body}: body is parsed JSON, text, or bytes."""
        return self.calls([(method, params)], timeout, listen)[0]

    def calls(self, requests, timeout=None, listen=None):
        built = [(ROUTES.get(method), build_request(method, params)) for method, params in requests]
        with self.lock:
            channel = self.channel()
            try:
                results = []
                for route, (service, verb, path, body) in built:
                    wait = timeout or route.get("requestTimeoutMs", 30000) / 1000
                    status, headers, data = channel.exchange(
                        service, verb, path, body, wait, listen, route.get("maxResponseBytes") or MAX_BODY)
                    results.append(result(status, headers, data))
                return results
            finally:
                channel.close()

    def download(self, path, timeout=300):
        """The bytes of a file on the Muse computer: `sandbox://workspace/…`, a media URL or a path."""
        path = re.sub(r"^(sandbox:/|https?://[^/]+)?/*(media/raw/)?", "", str(path))
        if not path or ".." in path.split("/"):
            raise MuseWebError("that is not a file on the Muse computer")
        with self.lock:
            channel = self.channel()
            try:
                status, headers, data = channel.exchange(
                    0, "GET", "/media/raw/" + quote(path, safe="/"), b"", timeout, limit=10 * MAX_BODY)
            finally:
                channel.close()
        if not 200 <= status < 300:
            raise MuseApiError(status, data[:500].decode("utf-8", "replace"))
        return headers.get("content-type", "application/octet-stream"), data


def result(status, headers, data):
    kind = headers.get("content-type", "").split(";")[0].strip()
    if not 200 <= status < 300:
        raise MuseApiError(status, data[:2000].decode("utf-8", "replace"))
    if kind == "application/x-ndjson":
        body = []
        for line in data.split(b"\n"):
            if line.strip():
                try:
                    body.append(json.loads(line))
                except ValueError:
                    pass
        return {"status": status, "type": kind, "body": body}
    if kind in ("application/json", "") or kind.startswith("text/"):
        try:
            text = data.decode()
        except UnicodeDecodeError:
            return {"status": status, "type": kind or "application/octet-stream", "body": data}
        if kind.startswith("text/"):
            return {"status": status, "type": kind, "body": text}
        try:
            body = json.loads(text) if text else None
        except ValueError:
            return {"status": status, "type": "text/plain", "body": text}
        # Most methods wrap their answer as {"ok": true, "result": …}.
        if isinstance(body, dict) and isinstance(body.get("ok"), bool) and ("result" in body or not body["ok"]):
            if not body["ok"]:
                raise MuseApiError(status, json.dumps(body.get("error", body))[:2000])
            body = body["result"]
        return {"status": status, "type": "application/json", "body": body}
    return {"status": status, "type": kind, "body": data}


def app_routes(page):
    """The method table in the Muse app's own page (Muse.app/Contents/Resources/hatch/index.html)."""
    number = r"(?:0x[0-9a-fA-F]+|[\d.]+(?:e\d+)?(?:\*[\d.]+(?:e\d+)?)*)(?![/\w.])"
    routes = {}
    for literal in re.findall(r"\{method:`[\w.-]+`,httpMethod:`[A-Z]+`,path:`[^`]+`[^{}]*\}", page):
        text = dict(re.findall(r"(\w+):`([^`]*)`", literal))
        route = {"method": text["method"], "http": text["httpMethod"], "path": text["path"]}
        for name in ("service", "requestBody", "responseType"):
            if name in text:
                route[name] = text[name]
        # `1/0` (no limit) is left out.
        for name, value in re.findall(rf"(requestTimeoutMs|maxResponseBytes):({number})", literal):
            product = 1.0
            for factor in value.split("*"):
                product *= int(factor, 16) if factor.startswith("0x") else float(factor)
            route[name] = int(product)
        routes.setdefault(route["method"], route)
    return sorted(routes.values(), key=lambda route: route["method"])


def main(arguments):
    """`login <directory>` saves cookies read from standard input; `call <directory> <method> [json]`;
    `routes <page>` prints the method table found in the Muse app's page."""
    import sys

    command, directory, *rest = arguments
    if command == "routes":
        print(json.dumps(app_routes(Path(directory).read_text(errors="replace")), indent=1))
        return
    web = MuseWeb(directory)
    if command == "login":
        issued = parse_cookies(sys.stdin.read())
        if not issued.get(LOGIN_COOKIE, ("",))[0]:
            raise SystemExit(f"those cookies have no {LOGIN_COOKIE}: sign in to muse.ai first")
        web.login.update(issued, renewed=0, computer=None, expired=None, told=None, refusals=0)
        print(json.dumps({"computer": web.renew(), "cookies": sorted(web.login.cookies())}))
    elif command == "call":
        answer = web.call(rest[0], json.loads(rest[1]) if len(rest) > 1 else None)
        if isinstance(answer["body"], bytes):
            answer["body"] = f"<{len(answer['body'])} bytes>"
        print(json.dumps(answer, indent=1))
    else:
        raise SystemExit("usage: museweb.py login|call <directory> …")


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
