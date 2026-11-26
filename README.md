# @-/com.leonsilicon.hermes.plugins.leon

Hermes plugin `leon`: every part of Leon's Hermes setup except [autodeploy](../hermes-plugin-autodeploy/README.md), in one install. Until 2026-11-26 each feature was a plugin of its own, with its own mirror. Leon merged them so that Hermes installs one plugin and autodeploy follows one mirror. A change to the shared bridge used to mean up to ten installs in a row, each about 30 seconds with its own `uv sync`. Now it means one.

## Features

Each feature is a folder here, with its own README, `__init__.py` and `policy.ts`:

| Feature                                                    | Does                                                                                                 |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| [autolinear](autolinear/README.md)                         | Gives each of Leon's Slack messages in his Hermes DM or a project channel a Linear issue and channel |
| [dropbox-task-memory](dropbox_task_memory/README.md)       | Reads and saves Hermes's per-issue notes in Dropbox `/Agents/Memory`                                 |
| [grok-box-browser](grok_box_browser/README.md)             | A Chrome per Linear task in the Grok box, behind Hermes's browser tools (`grok-box` provider)        |
| [hermes22muse](hermes22muse/README.md)                     | Muse side chats, the Task progress message, and the Muse app tools                                   |
| [slack-always-react](slack_always_react/README.md)         | 👀 → ✅/❌ on every Slack message Hermes processes                                                   |
| [slack-as-leon-guard](slack_as_leon_guard/README.md)       | Ignores Hermes's own posts as Leon and labels agents' reports                                        |
| [slack-auto-join](slack_auto_join/README.md)               | Keeps Hermes in every Slack channel                                                                  |
| [slack-flat-replies](slack_flat_replies/README.md)         | Answers top-level Slack messages in the channel, not a thread                                        |
| [slack-own-thread-reply](slack_own_thread_reply/README.md) | `slack_reply_own_thread`, for threads Hermes's own bot started                                       |
| [slack22orca](slack22orca/README.md)                       | Delegates Slack tasks to Orca and relays the answers                                                 |

A feature keeps everything it had as a plugin: its tools and toolsets, its `/orca` command, its `.env` keys, its log lines (`slack-auto-join: …`), and its data under `$HERMES_HOME` (`plugin-data/slack-auto-join/`, `hermes22muse/`, `slack22orca/`, `grok-box-browser/`). Only its version is gone: the plugin has one.

## How it loads

`register` in [`__init__.py`](__init__.py) loads the features one at a time:

- **In a fixed order**, `FEATURES`, which is the order Hermes loaded the separate plugins in (alphabetical by name). Seven features wrap Hermes's Slack adapter, and Hermes runs their handler factories in the order they were registered, so changing the order can change what they do.
- **A feature that fails stays out.** Each feature registers through a context that holds its registrations until its own `register` returns, then hands them to Hermes. One that raises registers nothing, and the gateway log shows `leon: <feature> didn't load; the other features still run` with the traceback. The rest load as before. Hermes did this for each plugin when they were separate.
- **`LEON_PLUGIN_DISABLED`** in `.env` turns features off, by name or folder, separated by commas (`slack-auto-join,hermes22muse`). It replaces `hermes plugins disable <name>`. Restart the gateway after changing it.

Hermes's per-plugin load deadline (`plugins.load_timeout_seconds`, 10 s by default) now covers all ten features together. Importing and registering all of them took 0.03 s on 2026-11-26.

Each feature's `policy.ts` builds into its own `policy.cjs` beside its Python, and one [bridge](../../DEVELOPMENT.md#hermes-plugin-mirrors) at the plugin's root (`_bridge.py`, `bridge.cjs`) runs them all in one Node worker. Run `vp -C apps/hermes-plugin-leon pack` to rebuild every bundle.

## Install

Once, pinned to the mirror's current commit, then restart the gateway when Hermes is idle:

```sh
hermes plugins install leonsiliconAI/hermes-plugin-leon --ref "$(git ls-remote https://github.com/leonsiliconAI/hermes-plugin-leon.git refs/heads/main | cut -f1)" --yes-deps --enable
```

From then on each push to `main` deploys it: CI publishes the mirror, then has [autodeploy](../hermes-plugin-autodeploy/README.md) install it and restart the gateway once Hermes is idle. Each feature's README lists its settings and the log line that shows it loaded.

## Test

From the repository root, after `vpr -w build:hermes-plugins`. These run in CI:

```sh
node --test apps/hermes-plugin-leon/__tests__/*.test.mjs
uv run --no-project --python 3.14 --with pygls==2.1.1 --with cryptography --with pytest python -m pytest \
  --rootdir=. --import-mode=importlib \
  apps/hermes-plugin-leon/__tests__/test_leon.py \
  apps/hermes-plugin-leon/__tests__/test_dropbox_task_memory.py \
  apps/hermes-plugin-leon/__tests__/test_grok_box_browser.py \
  apps/hermes-plugin-leon/__tests__/test_slack22orca.py \
  apps/hermes-plugin-leon/__tests__/test_hermes22muse.py
```

The other tests (autolinear and the Slack features) drive Hermes's real Slack adapter, so they need a Hermes checkout's environment:

```sh
uv venv /tmp/hermes-test --python ~/.hermes/tools/python-*/bin/python3
VIRTUAL_ENV=/tmp/hermes-test uv pip install pytest pytest-asyncio slack-bolt==1.30.0 slack-sdk==3.45.0 aiohttp pygls==2.1.1
printf '%s\n' ~/.hermes/hermes-agent ~/.hermes/hermes-agent/.venv/lib/python3.14/site-packages \
  > /tmp/hermes-test/lib/python3.14/site-packages/hermes.pth
HERMES_HOME=$(mktemp -d) /tmp/hermes-test/bin/python -m pytest -q -p no:cacheprovider --rootdir=. --import-mode=importlib \
  apps/hermes-plugin-leon/__tests__/test_autolinear.py apps/hermes-plugin-leon/__tests__/test_slack_*.py
```

The tests sit here rather than in each feature's folder because pytest, with `--import-mode=importlib`, imports a test's enclosing package on its own. A feature imported that way can't reach the bridge at the plugin's root. Each test imports its feature under `leon`, as Hermes does.
