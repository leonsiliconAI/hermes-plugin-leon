"""Leon's Hermes plugin: every part of his Hermes setup except autodeploy, in one install.

Each feature is a subpackage, the separate plugin it was until 2026-11-26, with its own
``register(ctx)``. Hermes installs, reloads and updates them together, and autodeploy follows one
mirror for all of them.

- **Order.** Features register in ``FEATURES`` order, which is the order Hermes loaded the separate
  plugins in (alphabetical by name). Seven of them wrap the same Slack adapter, and Hermes runs their
  handler factories in the order they were registered, so the order is part of what they do.
- **A feature that fails stays out.** A feature registers through a context that holds its
  registrations until its ``register`` returns, then hands them to Hermes. One that raises is
  logged and registers nothing, and the rest load as before. Hermes did that for each plugin when
  they were separate.
- **Turning one off.** ``LEON_PLUGIN_DISABLED`` in ``.env`` lists features to skip, by name
  (``slack-auto-join``) or folder (``slack_auto_join``), separated by commas. It replaces
  ``hermes plugins disable <name>``.

Hermes's load deadline (``plugins.load_timeout_seconds``, 10 s by default) now covers all of them
at once. Importing and registering all ten took 0.03 s on 2026-11-26.
"""

from __future__ import annotations

import importlib
import logging
import os
import time

LOG = logging.getLogger(__name__)

FEATURES = (
    "autolinear",
    "dropbox_task_memory",
    "grok_box_browser",
    "hermes22muse",
    "slack_always_react",
    "slack_as_leon_guard",
    "slack_auto_join",
    "slack_flat_replies",
    "slack_own_thread_reply",
    "slack22orca",
)
DISABLED_ENV = "LEON_PLUGIN_DISABLED"


def disabled_features(value: str | None = None) -> set[str]:
    """Folders of the features ``LEON_PLUGIN_DISABLED`` turns off."""
    if value is None:
        value = os.environ.get(DISABLED_ENV, "")
    return {name.strip().replace("-", "_") for name in value.split(",") if name.strip()}


class FeatureContext:
    """Hermes's ``ctx`` for one feature. ``register_*`` calls wait in order until ``publish``;
    everything else reaches Hermes directly."""

    def __init__(self, ctx, feature: str):
        self._ctx = ctx
        self._feature = feature
        self._held: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        attribute = getattr(self._ctx, name)
        if not (name.startswith("register_") and callable(attribute)):
            return attribute

        def hold(*args, **kwargs):
            self._held.append((name, args, kwargs))

        return hold

    def publish(self) -> None:
        """Hand the held registrations to Hermes. If Hermes refuses one, release those it took."""
        handles = []
        try:
            for name, args, kwargs in self._held:
                handles.append(getattr(self._ctx, name)(*args, **kwargs))
        except Exception:
            for handle in reversed(handles):
                dispose = getattr(handle, "dispose", None)
                if callable(dispose):
                    try:
                        dispose()
                    except Exception:
                        LOG.debug("leon: couldn't release a %s registration", self._feature, exc_info=True)
            raise


def register(ctx) -> None:
    disabled = disabled_features()
    for feature in FEATURES:
        if feature in disabled:
            LOG.info("leon: %s is off (%s)", feature, DISABLED_ENV)
            continue
        started = time.monotonic()
        held = FeatureContext(ctx, feature)
        try:
            importlib.import_module(f"{__name__}.{feature}").register(held)
            held.publish()
        except Exception:
            LOG.exception("leon: %s didn't load; the other features still run", feature)
            continue
        LOG.debug("leon: %s loaded in %.2f s", feature, time.monotonic() - started)
