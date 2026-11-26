"""Task-memory notes in Dropbox at /Agents/Memory/<linear-slug>.md, without a confirmation per write.

Why: every write tool on Dropbox's hosted MCP server (create_file, move, ...) carries an "ASSISTANT
POLICY" in its description: "Before calling this tool, the assistant MUST summarize the exact
mutation plan and get explicit user confirmation in chat." Dropbox documents no setting, header or
scope that changes this, and the text is the same for every path. So each task-memory save cost
Leon a second "yes" in Slack.

Leon gave a standing authorization for one narrow class of writes (Slack, 2026-11-11): keeping
per-issue notes at /Agents/Memory/<linear-slug>.md. These tools do only that, through the Dropbox
HTTP API with their own grant that has no sharing scope:

- read, list, create, append to, or replace (with the rev from a read) a note in /Agents/Memory;
- move a file that is already somewhere under /Agents/ into /Agents/Memory/<slug>.md.

They can't delete, share, or write anywhere else. Every other Dropbox change still goes through
the Dropbox MCP tools and their confirmation rule.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .._bridge import blocking, call

LOG = logging.getLogger(__name__)

TOOLSET = "dropbox_task_memory"
READ, LIST, WRITE, MOVE = (
    "dropbox_task_memory_read", "dropbox_task_memory_list",
    "dropbox_task_memory_write", "dropbox_task_memory_move")
REFRESH_TOKEN_ENV = "DROPBOX_TASK_MEMORY_REFRESH_TOKEN"
APP_KEY_ENV = "DROPBOX_TASK_MEMORY_APP_KEY"

ROOT = "/Agents/Memory"
MAX_BYTES = 512 * 1024
API = "https://api.dropboxapi.com"
CONTENT = "https://content.dropboxapi.com"
VERSIONS_HELP = "https://help.dropbox.com/delete-restore/recover-older-versions"

# The issue identifier, then the title words from the Linear URL: leon-330-prepare-separate-...

_LOCK = threading.Lock()
_TOKEN: dict[str, float | str] = {}

AUTHORIZATION = (
    "Leon authorized these writes in advance (standing rule, Slack, November 11, 2026): create, "
    "update and move task-memory notes at /Agents/Memory/<linear-slug>.md without asking him to "
    "confirm each one. Report the returned path after the write. The tool can't delete, share, or "
    "write outside /Agents/Memory; for any other Dropbox change use the Dropbox MCP tools and "
    "follow their confirmation policy.")

SLUG_PARAM = {
    "type": "string",
    "description": ("Linear issue slug: the identifier plus the title words from the issue URL, "
                    "lowercase, e.g. leon-330-prepare-separate-st-george-chapter-sop-application. "
                    "No folder, no .md."),
}

SCHEMAS = {
    READ: {
        "name": READ,
        "description": ("Read a task-memory note at Dropbox /Agents/Memory/<slug>.md. Returns the "
                        "text and its rev; pass that rev to dropbox_task_memory_write with "
                        "mode=replace. Read-only."),
        "parameters": {"type": "object", "properties": {"slug": SLUG_PARAM},
                       "required": ["slug"], "additionalProperties": False},
    },
    LIST: {
        "name": LIST,
        "description": "List the task-memory notes in Dropbox /Agents/Memory. Read-only.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    WRITE: {
        "name": WRITE,
        "description": (
            "Save a task-memory note at Dropbox /Agents/Memory/<slug>.md (the folder is created if "
            "missing). mode=create makes a new note and fails if one exists; mode=append adds text "
            "to the end of the note, creating it if missing; mode=replace rewrites the whole note "
            "and needs the rev from dropbox_task_memory_read, so it can't overwrite a newer "
            "version. Dropbox keeps earlier versions. " + AUTHORIZATION),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": SLUG_PARAM,
                "content": {"type": "string", "description": "Markdown text (UTF-8, at most 512 KB)."},
                "mode": {"type": "string", "enum": ["create", "append", "replace"]},
                "rev": {"type": "string", "description": "Required for mode=replace: the rev from the last read."},
            },
            "required": ["slug", "content", "mode"],
            "additionalProperties": False,
        },
    },
    MOVE: {
        "name": MOVE,
        "description": (
            "Move a file that is already under Dropbox /Agents/ (e.g. an old "
            "/Agents/Branches/<slug>/memory.md) to /Agents/Memory/<slug>.md. Fails if the "
            "destination exists. Only files, only from inside /Agents/. " + AUTHORIZATION),
        "parameters": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string",
                                "description": "Full Dropbox path of the file, starting with /Agents/."},
                "slug": SLUG_PARAM,
            },
            "required": ["source_path", "slug"],
            "additionalProperties": False,
        },
    },
}


class DropboxError(RuntimeError):
    def __init__(self, summary: str, status: int = 0):
        super().__init__(summary)
        self.summary = summary
        self.status = status


def available() -> bool:
    return bool(os.environ.get(REFRESH_TOKEN_ENV, "").strip() and os.environ.get(APP_KEY_ENV, "").strip())


def _error(message: str) -> str:
    return json.dumps({"success": False, "error": message})


def _valid_path(function, value) -> str:
    result = call(__file__, function, str(value or ""))
    if "error" in result:
        raise ValueError(result["error"])
    return result["path"]


def note_path(slug: str) -> str:
    return _valid_path("notePath", slug)


def source_path(path: str) -> str:
    return _valid_path("sourcePath", path)


# --- Dropbox HTTP API -------------------------------------------------------------------------

def _urlopen(request: urllib.request.Request):
    return urllib.request.urlopen(request, timeout=30)


def _access_token(force: bool = False) -> str:
    with _LOCK:
        if not force and _TOKEN.get("value") and float(_TOKEN.get("expires", 0)) > time.time() + 60:
            return str(_TOKEN["value"])
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": os.environ.get(REFRESH_TOKEN_ENV, "").strip(),
            "client_id": os.environ.get(APP_KEY_ENV, "").strip(),
        }).encode()
        request = urllib.request.Request(f"{API}/oauth2/token", data=body, method="POST")
        try:
            with _urlopen(request) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            raise DropboxError(f"token refresh failed ({exc.code}); the {REFRESH_TOKEN_ENV} grant may be revoked",
                               exc.code) from None
        _TOKEN["value"] = data["access_token"]
        _TOKEN["expires"] = time.time() + float(data.get("expires_in", 14400))
        return str(_TOKEN["value"])


def _call(url: str, *, arg: dict | None = None, body: bytes | None = None, content: bool = False):
    """POST to the Dropbox API, refreshing the access token once on 401.

    Returns (json_result, raw_body). RPC endpoints take ``arg`` as the JSON body; content endpoints
    take it in the Dropbox-API-Arg header (ASCII-escaped JSON, as Dropbox requires).
    """
    for attempt in (0, 1):
        headers = {"Authorization": f"Bearer {_access_token(force=attempt == 1)}"}
        if content:
            headers["Dropbox-API-Arg"] = json.dumps(arg or {}, ensure_ascii=True)
            if body is not None:
                headers["Content-Type"] = "application/octet-stream"
            data = body if body is not None else b""
        else:
            headers["Content-Type"] = "application/json"
            data = json.dumps(arg if arg is not None else None).encode()
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with _urlopen(request) as response:
                raw = response.read()
                result_header = response.headers.get("Dropbox-API-Result")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code == 401 and attempt == 0:
                continue
            try:
                summary = json.loads(detail).get("error_summary") or detail
            except Exception:
                summary = detail
            raise DropboxError(str(summary).strip()[:300] or f"HTTP {exc.code}", exc.code) from None
        if content and result_header is not None:
            return json.loads(result_header), raw
        return (json.loads(raw) if raw else {}), raw
    raise DropboxError("unauthorized after refreshing the access token", 401)


def _metadata(path: str) -> dict | None:
    try:
        result, _ = _call(f"{API}/2/files/get_metadata", arg={"path": path})
        return result
    except DropboxError as exc:
        if exc.status == 409 and "not_found" in exc.summary:
            return None
        raise


def _download(path: str) -> list | None:
    try:
        meta, raw = _call(f"{CONTENT}/2/files/download", arg={"path": path}, content=True)
    except DropboxError as exc:
        if exc.status == 409 and "not_found" in exc.summary:
            return None
        raise
    return [meta, raw.decode("utf-8", "replace")]


def _upload(path: str, text: str, mode) -> dict:
    meta, _ = _call(f"{CONTENT}/2/files/upload", content=True, body=text.encode("utf-8"),
                    arg={"path": path, "mode": mode, "autorename": False, "mute": True,
                         "strict_conflict": True})
    return meta


def _list_page(cursor=None):
    if cursor is None:
        result, _ = _call(f"{API}/2/files/list_folder", arg={"path": ROOT, "recursive": False, "limit": 500})
    else:
        result, _ = _call(f"{API}/2/files/list_folder/continue", arg={"cursor": cursor})
    return result


def _move_file(source, destination):
    result, _ = _call(f"{API}/2/files/move_v2", arg={
        "from_path": source, "to_path": destination, "autorename": False,
        "allow_ownership_transfer": False})
    return result


def _response(function, *args):
    try:
        value = function(*args)
        return {"value": value} if value is not None else {}
    except DropboxError as exc:
        return {"error": str(exc), "summary": exc.summary, "status": exc.status}


def _run_policy(function, *args):
    host = {
        "download": blocking(lambda path: _response(_download, path)),
        "upload": blocking(lambda path, text, mode: _response(_upload, path, text, mode)),
        "metadata": blocking(lambda path: _response(_metadata, path)),
        "list": blocking(lambda cursor: _response(_list_page, cursor)),
        "move": blocking(lambda source, destination: _response(_move_file, source, destination)),
        "logWrite": lambda mode, meta: LOG.info(
            "dropbox-task-memory: %s %s (rev %s)", mode, meta.get("path_display"), meta.get("rev")),
        "logMove": lambda source, moved: LOG.info(
            "dropbox-task-memory: moved %s to %s", source.get("path_display"), moved.get("path_display")),
        "byteLength": lambda text: len(text.encode("utf-8")),
    }
    return json.dumps(call(__file__, function, host, *args))


def handle_read(args: dict, **_kw) -> str:
    return _run_policy("read", str(args.get("slug") or ""))


def handle_list(args: dict, **_kw) -> str:
    return _run_policy("list")


def handle_write(args: dict, **_kw) -> str:
    return _run_policy("write", str(args.get("slug") or ""), str(args.get("mode") or ""),
                       args.get("content"), str(args.get("rev") or "").strip(), MAX_BYTES, VERSIONS_HELP)


def handle_move(args: dict, **_kw) -> str:
    return _run_policy("move", str(args.get("source_path") or ""), str(args.get("slug") or ""))


def _guarded(handler):
    """Size-check content before any network call, and never let an exception escape to the agent loop."""
    def run(args: dict, **kw) -> str:
        args = args if isinstance(args, dict) else {}
        content = args.get("content")
        if isinstance(content, str) and len(content.encode("utf-8")) > MAX_BYTES:
            return _error(f"content is over {MAX_BYTES // 1024} KB; keep task notes concise")
        if not available():
            return _error(f"{REFRESH_TOKEN_ENV} and {APP_KEY_ENV} are not set in the Hermes .env")
        try:
            return handler(args, **kw)
        except Exception as exc:  # network errors, malformed responses
            LOG.warning("dropbox-task-memory: %s failed: %s", handler.__name__, exc)
            return _error(f"Dropbox request failed: {exc}")
    run.__name__ = handler.__name__
    return run


HANDLERS = {READ: handle_read, LIST: handle_list, WRITE: handle_write, MOVE: handle_move}
EMOJI = {READ: "📖", LIST: "🗂️", WRITE: "📝", MOVE: "📦"}


def register(ctx):
    for name, handler in HANDLERS.items():
        ctx.register_tool(name=name, toolset=TOOLSET, schema=SCHEMAS[name], handler=_guarded(handler),
                          check_fn=available, emoji=EMOJI[name])
    LOG.info("dropbox-task-memory: %s registered for %s", ", ".join(HANDLERS), ROOT)
