"""Plain pytest; the plugin imports nothing from Hermes. Dropbox is an in-memory fake."""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import types
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


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


plugin = _feature("dropbox_task_memory")

SLUG = "leon-330-prepare-separate-st-george-chapter-sop-application"
NOTE = f"/Agents/Memory/{SLUG}.md"


class _Response:
    def __init__(self, body: bytes, headers: dict | None = None):
        self._body, self.headers = body, headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDropbox:
    """Just enough of the Dropbox API: files keyed by lowercase path, revs, 409 error summaries."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.tokens_issued = 0
        self.expire_next = False
        self._rev = 0

    def put(self, path: str, text: str) -> dict:
        self._rev += 1
        meta = {".tag": "file", "name": path.rsplit("/", 1)[1], "path_display": path,
                "rev": f"{self._rev:09x}", "size": len(text.encode()), "server_modified": "2026-11-11T12:00:00Z"}
        self.files[path.lower()] = {"meta": meta, "text": text}
        return meta

    def _fail(self, url, code, summary):
        raise urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(json.dumps({"error_summary": summary}).encode()))

    def urlopen(self, request):
        url = request.full_url
        endpoint = url.split(".com", 1)[1]
        if endpoint == "/oauth2/token":
            form = dict(p.split("=", 1) for p in request.data.decode().split("&"))
            assert form["grant_type"] == "refresh_token" and form["client_id"] == "appkey"
            self.tokens_issued += 1
            return _Response(json.dumps({"access_token": f"sl.token{self.tokens_issued}", "expires_in": 14400}).encode())
        if self.expire_next:
            self.expire_next = False
            self._fail(url, 401, "expired_access_token/")
        assert request.get_header("Authorization", "").startswith("Bearer sl.token")
        header = request.get_header("Dropbox-api-arg")
        arg = json.loads(header) if header is not None else json.loads(request.data or b"null")
        self.calls.append((endpoint, arg))
        if endpoint == "/2/files/get_metadata":
            if arg["path"].lower() in self.files:
                return _Response(json.dumps(self.files[arg["path"].lower()]["meta"]).encode())
            folders = {k.rsplit("/", 1)[0] for k in self.files}
            if arg["path"].lower() in folders:
                return _Response(json.dumps({".tag": "folder", "path_display": arg["path"]}).encode())
            self._fail(url, 409, "path/not_found/..")
        if endpoint == "/2/files/download":
            entry = self.files.get(arg["path"].lower())
            if entry is None:
                self._fail(url, 409, "path/not_found/..")
            return _Response(entry["text"].encode(), {"Dropbox-API-Result": json.dumps(entry["meta"])})
        if endpoint == "/2/files/upload":
            assert arg["autorename"] is False and arg["strict_conflict"] is True
            existing = self.files.get(arg["path"].lower())
            mode = arg["mode"]
            if mode == "add" and existing:
                self._fail(url, 409, "path/conflict/file/..")
            if isinstance(mode, dict) and (not existing or existing["meta"]["rev"] != mode["update"]):
                self._fail(url, 409, "path/conflict/file/..")
            meta = self.put(arg["path"], request.data.decode())
            return _Response(b"", {"Dropbox-API-Result": json.dumps(meta)})
        if endpoint == "/2/files/move_v2":
            source = self.files.get(arg["from_path"].lower())
            if source is None:
                self._fail(url, 409, "from_lookup/not_found/..")
            if arg["to_path"].lower() in self.files:
                self._fail(url, 409, "to/conflict/file/..")
            del self.files[arg["from_path"].lower()]
            meta = self.put(arg["to_path"], source["text"])
            return _Response(json.dumps({"metadata": meta}).encode())
        if endpoint == "/2/files/list_folder":
            prefix = arg["path"].lower() + "/"
            entries = [f["meta"] for k, f in self.files.items() if k.startswith(prefix) and "/" not in k[len(prefix):]]
            if not entries:
                self._fail(url, 409, "path/not_found/..")
            return _Response(json.dumps({"entries": entries, "has_more": False}).encode())
        raise AssertionError(f"unexpected endpoint {endpoint}")


@pytest.fixture()
def dropbox(monkeypatch):
    fake = FakeDropbox()
    monkeypatch.setenv(plugin.REFRESH_TOKEN_ENV, "refresh")
    monkeypatch.setenv(plugin.APP_KEY_ENV, "appkey")
    monkeypatch.setattr(plugin, "_urlopen", fake.urlopen)
    plugin._TOKEN.clear()
    return fake


@pytest.fixture()
def tools():
    seen = {}
    plugin.register(types.SimpleNamespace(register_tool=lambda **kw: seen.__setitem__(kw["name"], kw)))
    return {name: (lambda args, h=kw["handler"]: json.loads(h(args))) for name, kw in seen.items()}


def mutations(fake):
    return [c for c in fake.calls if c[0] in ("/2/files/upload", "/2/files/move_v2")]


def test_registers_four_tools_with_env_check(monkeypatch):
    seen = {}
    plugin.register(types.SimpleNamespace(register_tool=lambda **kw: seen.__setitem__(kw["name"], kw)))
    assert set(seen) == {plugin.READ, plugin.LIST, plugin.WRITE, plugin.MOVE}
    assert all(kw["toolset"] == "dropbox_task_memory" and kw["check_fn"] is plugin.available for kw in seen.values())
    assert all(kw["schema"]["name"] == name for name, kw in seen.items())
    monkeypatch.delenv(plugin.REFRESH_TOKEN_ENV, raising=False)
    assert plugin.available() is False


def test_write_descriptions_carry_the_scope_and_authorization():
    for name in (plugin.WRITE, plugin.MOVE):
        text = plugin.SCHEMAS[name]["description"]
        assert "/Agents/Memory" in text and "November 11, 2026" in text and "confirmation policy" in text


def test_create_then_read_back(dropbox, tools):
    result = tools[plugin.WRITE]({"slug": SLUG, "content": "# LEON-330\n", "mode": "create"})
    assert result["success"] and result["path_display"] == NOTE and result["action"] == "create"
    read = tools[plugin.READ]({"slug": SLUG})
    assert read["exists"] and read["content"] == "# LEON-330\n" and read["rev"] == result["rev"]
    assert dropbox.calls[0] == ("/2/files/upload", {"path": NOTE, "mode": "add", "autorename": False,
                                                    "mute": True, "strict_conflict": True})


def test_concurrent_tool_calls_can_await_host_io(dropbox, tools, monkeypatch):
    dropbox.put(NOTE, "notes")
    both_downloads = Barrier(2)
    download = plugin._download

    def concurrent_download(path):
        both_downloads.wait(timeout=5)
        return download(path)

    monkeypatch.setattr(plugin, "_download", concurrent_download)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(tools[plugin.READ], {"slug": SLUG}) for _ in range(2)]
        assert [call.result(timeout=10)["content"] for call in calls] == ["notes", "notes"]


def test_create_refuses_to_clobber(dropbox, tools):
    dropbox.put(NOTE, "keep me")
    result = tools[plugin.WRITE]({"slug": SLUG, "content": "new", "mode": "create"})
    assert not result["success"] and "use mode=append or mode=replace" in result["error"]
    assert dropbox.files[NOTE.lower()]["text"] == "keep me"


def test_append_keeps_existing_text(dropbox, tools):
    dropbox.put(NOTE, "line one")
    assert tools[plugin.WRITE]({"slug": SLUG, "content": "line two\n", "mode": "append"})["success"]
    assert dropbox.files[NOTE.lower()]["text"] == "line one\nline two\n"


def test_append_creates_missing_note(dropbox, tools):
    assert tools[plugin.WRITE]({"slug": SLUG, "content": "first", "mode": "append"})["success"]
    assert dropbox.files[NOTE.lower()]["text"] == "first"


def test_replace_needs_current_rev(dropbox, tools):
    old = dropbox.put(NOTE, "v1")
    assert "read the note first" in tools[plugin.WRITE]({"slug": SLUG, "content": "v2", "mode": "replace"})["error"]
    dropbox.put(NOTE, "v1 edited elsewhere")
    stale = tools[plugin.WRITE]({"slug": SLUG, "content": "v2", "mode": "replace", "rev": old["rev"]})
    assert not stale["success"] and "changed since it was read" in stale["error"]
    fresh = tools[plugin.READ]({"slug": SLUG})
    assert tools[plugin.WRITE]({"slug": SLUG, "content": "v2", "mode": "replace", "rev": fresh["rev"]})["success"]
    assert dropbox.files[NOTE.lower()]["text"] == "v2"


@pytest.mark.parametrize("slug", [
    "../secrets", "/Agents/Memory/leon-330", "leon-330/../../x", "Personal/taxes", "leon", "leon-330 x",
    "leon-330-é", "", "x" * 10 + "-1-" + "a" * 200, "leon-330-prepare\nevil",
])
def test_bad_slugs_never_reach_dropbox(dropbox, tools, slug):
    result = tools[plugin.WRITE]({"slug": slug, "content": "x", "mode": "create"})
    assert not result["success"] and "slug must be" in result["error"]
    assert mutations(dropbox) == []


def test_slug_is_normalised(dropbox, tools):
    result = tools[plugin.WRITE]({"slug": "LEON-330-Prepare-Separate.md", "content": "x", "mode": "create"})
    assert result["path_display"] == "/Agents/Memory/leon-330-prepare-separate.md"


def test_move_from_agents_into_memory(dropbox, tools):
    old = f"/Agents/Branches/{SLUG}/memory.md"
    dropbox.put(old, "notes")
    result = tools[plugin.MOVE]({"source_path": old, "slug": SLUG})
    assert result["success"] and result["path_display"] == NOTE and result["moved_from"] == old
    assert old.lower() not in dropbox.files and dropbox.files[NOTE.lower()]["text"] == "notes"
    move = [c for c in dropbox.calls if c[0] == "/2/files/move_v2"][0][1]
    assert move == {"from_path": old, "to_path": NOTE, "autorename": False, "allow_ownership_transfer": False}


@pytest.mark.parametrize("source", [
    "/Personal/taxes.pdf", "/agents", "/Agents", "Agents/x.md", "/Agents/../Personal/x.md",
    "/Agents//x.md", "/AgentsX/x.md", "id:AbCd-_12", "/Agents/Branches\\x.md",
])
def test_move_refuses_sources_outside_agents(dropbox, tools, source):
    dropbox.put("/Personal/taxes.pdf", "private")
    result = tools[plugin.MOVE]({"source_path": source, "slug": SLUG})
    assert not result["success"] and "inside /Agents/" in result["error"]
    assert mutations(dropbox) == []


def test_move_refuses_folders_and_existing_destination(dropbox, tools):
    dropbox.put(f"/Agents/Branches/{SLUG}/memory.md", "notes")
    folder = tools[plugin.MOVE]({"source_path": f"/Agents/Branches/{SLUG}", "slug": SLUG})
    assert not folder["success"] and "only files" in folder["error"]
    dropbox.put(NOTE, "already here")
    clash = tools[plugin.MOVE]({"source_path": f"/Agents/Branches/{SLUG}/memory.md", "slug": SLUG})
    assert not clash["success"] and "conflict" in clash["error"]
    assert dropbox.files[NOTE.lower()]["text"] == "already here"


def test_list_notes(dropbox, tools):
    assert tools[plugin.LIST]({})["notes"] == []
    dropbox.put(NOTE, "a")
    dropbox.put("/Agents/Memory/leon-1-b.md", "b")
    dropbox.put("/Agents/Memory/sub/leon-2-c.md", "c")
    names = [n["name"] for n in tools[plugin.LIST]({})["notes"]]
    assert names == ["leon-1-b.md", f"{SLUG}.md"]


def test_expired_access_token_is_refreshed_once(dropbox, tools):
    assert tools[plugin.LIST]({})["success"]
    dropbox.expire_next = True
    dropbox.put(NOTE, "x")
    assert tools[plugin.READ]({"slug": SLUG})["content"] == "x"
    assert dropbox.tokens_issued == 2


def test_oversized_content_is_refused_before_any_call(dropbox, tools):
    result = tools[plugin.WRITE]({"slug": SLUG, "content": "x" * (plugin.MAX_BYTES + 1), "mode": "create"})
    assert not result["success"] and "KB" in result["error"]
    assert dropbox.calls == [] and dropbox.tokens_issued == 0


def test_no_credentials_means_no_calls(monkeypatch, tools):
    monkeypatch.delenv(plugin.REFRESH_TOKEN_ENV, raising=False)
    monkeypatch.setattr(plugin, "_urlopen", lambda request: pytest.fail("network call without credentials"))
    assert "not set" in tools[plugin.WRITE]({"slug": SLUG, "content": "x", "mode": "create"})["error"]


def test_non_ascii_content_and_header_escaping(dropbox, tools):
    assert tools[plugin.WRITE]({"slug": SLUG, "content": "Café — 北京\n", "mode": "create"})["success"]
    assert tools[plugin.READ]({"slug": SLUG})["content"] == "Café — 北京\n"
