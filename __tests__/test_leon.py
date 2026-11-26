"""The leon plugin's own loader: feature order, LEON_PLUGIN_DISABLED, and a feature that fails."""

from __future__ import annotations

import importlib.util
import logging
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("leon_loader", _ROOT / "__init__.py")
leon = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = leon
_spec.loader.exec_module(leon)


class Ctx:
    """Records what reaches Hermes."""

    def __init__(self, refuse: str | None = None):
        self.calls = []
        self.disposed = []
        self.refuse = refuse
        self.manifest = types.SimpleNamespace(name="leon")

    def _register(self, kind):
        def register(name, *args, **kwargs):
            if name == self.refuse:
                raise ValueError(f"refused {name}")
            self.calls.append((kind, name))
            return types.SimpleNamespace(dispose=lambda: self.disposed.append(name))

        return register

    def __getattr__(self, attribute):
        if attribute.startswith("register_"):
            return self._register(attribute.removeprefix("register_"))
        raise AttributeError(attribute)


def _features(monkeypatch, **registers):
    """Install stand-in features under the loader's package and load only those."""
    monkeypatch.setattr(leon, "FEATURES", tuple(registers))
    for folder, register in registers.items():
        monkeypatch.setitem(sys.modules, f"{leon.__name__}.{folder}", types.SimpleNamespace(register=register))


def test_features_are_every_folder_in_the_separate_plugins_load_order():
    folders = sorted(path.parent.name for path in _ROOT.glob("*/__init__.py"))
    assert sorted(leon.FEATURES) == folders
    # Hermes loaded the separate plugins alphabetically by name, and the Slack patches depend on it.
    assert list(leon.FEATURES) == sorted(leon.FEATURES, key=lambda folder: folder.replace("_", "-"))


def test_disabled_features_take_names_or_folders():
    assert leon.disabled_features("") == set()
    assert leon.disabled_features(" slack-auto-join, hermes22muse ,, grok_box_browser") == {
        "slack_auto_join", "hermes22muse", "grok_box_browser"}


def test_features_register_in_order_and_a_failing_one_registers_nothing(monkeypatch, caplog):
    monkeypatch.delenv(leon.DISABLED_ENV, raising=False)

    def first(ctx):
        ctx.register_tool("first_tool")
        ctx.register_platform_handler("slack")

    def broken(ctx):
        ctx.register_tool("broken_tool")
        raise RuntimeError("no settings")

    def last(ctx):
        assert ctx.manifest.name == "leon"  # anything but register_* reaches Hermes directly
        ctx.register_hook("pre_llm_call")

    _features(monkeypatch, first=first, broken=broken, last=last)
    ctx = Ctx()
    with caplog.at_level(logging.ERROR):
        leon.register(ctx)
    assert ctx.calls == [("tool", "first_tool"), ("platform_handler", "slack"), ("hook", "pre_llm_call")]
    assert "leon: broken didn't load" in caplog.text


def test_a_registration_hermes_refuses_releases_the_rest_of_that_feature(monkeypatch, caplog):
    monkeypatch.delenv(leon.DISABLED_ENV, raising=False)

    def tools(ctx):
        ctx.register_tool("kept")
        ctx.register_tool("refused")

    _features(monkeypatch, tools=tools, other=lambda ctx: ctx.register_tool("other"))
    ctx = Ctx(refuse="refused")
    with caplog.at_level(logging.ERROR):
        leon.register(ctx)
    assert ctx.calls == [("tool", "kept"), ("tool", "other")]
    assert ctx.disposed == ["kept"]
    assert "leon: tools didn't load" in caplog.text


def test_every_features_slack_factory_is_wired(monkeypatch):
    # Hermes skips a factory whose (plugin, __qualname__) it already wired; most features call
    # theirs `wire`, and on 2026-11-26 only the first of them was wired.
    monkeypatch.delenv(leon.DISABLED_ENV, raising=False)
    wired = []

    def feature(label):
        def wire(native, adapter):
            wired.append((label, native, adapter))

        wire.__qualname__ = "wire"
        return lambda ctx: ctx.register_platform_handler("slack", wire)

    factories = []
    ctx = types.SimpleNamespace(register_platform_handler=lambda platform, factory: factories.append(factory),
                                manifest=types.SimpleNamespace(name="leon"))
    _features(monkeypatch, first=feature("first"), second=feature("second"))
    leon.register(ctx)
    seen = set()
    for factory in factories:  # BasePlatformAdapter._wire_plugin_handlers
        key = ("leon", factory.__qualname__)
        if key not in seen:
            seen.add(key)
            factory("native", "adapter")
    assert wired == [("first", "native", "adapter"), ("second", "native", "adapter")]
    assert [factory.__qualname__ for factory in factories] == ["first.wire", "second.wire"]


def test_leon_plugin_disabled_skips_a_feature(monkeypatch):
    monkeypatch.setenv(leon.DISABLED_ENV, "slack-auto-join")
    _features(monkeypatch, slack_auto_join=pytest.fail, slack_flat_replies=lambda ctx: ctx.register_tool("flat"))
    ctx = Ctx()
    leon.register(ctx)
    assert ctx.calls == [("tool", "flat")]
