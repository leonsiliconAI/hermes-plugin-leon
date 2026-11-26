# grok-box-browser

Part of the [leon plugin](../README.md), and a Hermes plugin of its own until 2026-11-26.

Hermes plugin that gives each Linear task its own Chrome in Leon's Grok Bot cloud computer, signed in with the logins Grok Bot's browser has, and makes it the browser behind Hermes's browser tools. Hermes drives it page by page over Chrome's DevTools protocol, with no Grok Bot run per action.

```text
Hermes Cloud                                   Grok Bot cloud computer ("Grok box")
browser tools ─▶ provider grok-box ─▶ 127.0.0.1:19400 ─ tailscale nc ─▶ grok-box:9400 (tailscale serve)
                                                                         └▶ broker 127.0.0.1:9400
                                                                             ├▶ Chrome leon-532 (:9401, display :100)
                                                                             └▶ Chrome leon-540 (:9402, display :101)
```

## How it works

- **One Chrome per task.** A turn in an AutoLinear issue channel (`leon-532-…`) gets the Chrome for `LEON-532`. Elsewhere a turn gets one per Slack conversation, then one per Hermes task. The plugin reads the channel from Hermes's gateway session.
- **Signed in from the start.** Grok Bot keeps the cookies its browser loads into every new Chrome in `/home/box/sand-data/chrome-cookie-seed.json`. The broker copies them into a task's new profile once, with `Storage.setCookies`. Logins made later in a task's Chrome stay in that task's profile and never touch Grok Bot's file. Grok Bot's own browsers are unaffected.
- **Lifetime.** A Chrome closes after 30 idle minutes and reopens on the same profile when the task needs it again. Every hour the plugin asks Linear which tasks are done or canceled and retires them. A retired profile is deleted 7 days later, and any profile unused for 30 days goes too. At most 6 Chromes run at once; when all are busy, the one idle longest makes room, and if none is idle the request is refused.
- **Reaching the box.** Hermes Cloud has no tailnet route of its own, so a loopback forwarder carries each connection with `tailscale nc` over the userspace Tailscale the [Orca device](../../orcahermes-device/README.md) already runs. The broker listens on loopback in the box, and `tailscale serve` publishes its port to the tailnet. Chrome's DevTools port only accepts a loopback Host, which both ends keep.
- **The broker** ([`box/broker.mjs`](box/broker.mjs), Node 22+ or Bun, no dependencies) starts Xvfb and Chrome per task on ports 9401 and up, outside the 9222 + display range Grok Bot's own Chromes use, and proxies DevTools HTTP and WebSocket traffic under a random per-session path (`/s/<secret>/…`), rewriting Chrome's discovery answers to point back through it.

## Security

Anything that reaches a task's DevTools endpoint controls a browser holding Leon's logins. Layers:

1. **Tailnet policy.** The box is `tag:grok-box`. Only `tag:hermes-cloud` may reach it, and only on `tcp:9400`. Nothing grants `tag:grok-box` any access, so a compromised box reaches nothing else on the tailnet.
2. **Bearer token.** The broker's session API needs `GROK_BOX_BROWSER_TOKEN`, kept in Hermes's `.env` and in the box's `token` file (mode 600).
3. **Per-session secret paths.** DevTools traffic is accepted only under a 144-bit secret issued with the session and dropped when the Chrome stops.

Every Grok bot shares the box, and its processes can already read Grok Bot's cookie file, so the box itself is the trust boundary: this plugin adds no access there that a bot doesn't already have.

## Setup

### Tailnet

In the policy file:

```json
"tagOwners": { "tag:grok-box": ["autogroup:admin"] },
"grants": [{ "src": ["tag:hermes-cloud"], "dst": ["tag:grok-box"], "ip": ["tcp:9400"] }],
"tests": [
  { "src": "tag:hermes-cloud", "accept": ["tag:grok-box:9400"], "deny": ["tag:grok-box:22"] },
  { "src": "tag:grok-box", "deny": ["tag:hermes-cloud:6768"] }
]
```

Then create a one-time, pre-approved auth key tagged `tag:grok-box`.

### The Grok box

Ask a Grok bot to run, in its own terminal, with `<commit>` the leon mirror commit and the two checksums from `shasum -a 256 box/*`:

```sh
mkdir -p /workspace/hermes-browsers/box && cd /workspace/hermes-browsers/box
base=https://raw.githubusercontent.com/leonsiliconAI/hermes-plugin-leon/<commit>/grok_box_browser/box
curl -fsSLO "$base/up.sh" && curl -fsSLO "$base/broker.mjs"
printf '%s  up.sh\n%s  broker.mjs\n' <up.sh sha256> <broker.mjs sha256> | sha256sum -c -
(umask 077; printf %s '<token>' > ../token; printf %s '<tskey-auth-…>' > ../tailscale-authkey)
bash up.sh
```

`up.sh` installs Tailscale 1.104.7 (sha256-checked), joins as `grok-box` with the one-time key and deletes it, publishes port 9400 with `tailscale serve`, starts the broker and prints a summary without secrets. Everything lives in `/workspace/hermes-browsers`: profiles, Tailscale state, logs.

**When Grok Bot pauses the box**, which it does whenever no bot is running, even while Hermes uses a Chrome there, the plugin wakes it through grokbot-admin's `/sandbox/ensure` (Cursor's `EnsureSandBox`, about half a second; every process survives the pause). It wakes it before each browser session, at most once a minute, and every 3 minutes while a browser carries traffic. When the broker doesn't answer, it wakes the box, waits up to 45 seconds, and tries again before asking for a repair.

**After the box restarts** (it does so without notice), [slack22grokbot](../../beeperd/internal/grokrelay/README.md#grok-box-upkeep) sets it up again: it puts the setup before the next message Hermes sends any Grok bot, or, when Hermes's browser finds the box down, sends it to `Grok Bot` on its own within a minute. If two repairs in a row fail, Hermes is told to ask Leon; then run the setup above by hand.

### Hermes

Set in the Hermes profile's environment (Hermes Cloud's Keys page):

| Key                                                              | Value                                                                                                                                                                                                                                                            |
| ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GROK_BOX_BROWSER_TOKEN`                                         | The broker's token                                                                                                                                                                                                                                               |
| `GROK_BOX_BROWSER_HOST`                                          | The box's tailnet name or address (default `grok-box`)                                                                                                                                                                                                           |
| `GROK_BOX_BROWSER_PORT`                                          | The box's tailnet port (default `9400`)                                                                                                                                                                                                                          |
| `GROK_BOX_BROWSER_LOCAL_PORT`                                    | The loopback forwarder's port on Hermes Cloud (default `19400`)                                                                                                                                                                                                  |
| `GROK_BOX_TAILSCALE`                                             | Default `$HERMES_HOME/orca-device/tailscale/bin/tailscale`                                                                                                                                                                                                       |
| `GROK_BOX_TAILSCALE_SOCKET`                                      | Default `$HERMES_HOME/orca-device/tailscale/tailscaled.sock`                                                                                                                                                                                                     |
| `LINEAR_API_KEY`                                                 | Already set; used to find done and canceled tasks                                                                                                                                                                                                                |
| `GROK_BOX_TS_OAUTH_CLIENT_ID`, `GROK_BOX_TS_OAUTH_CLIENT_SECRET` | A Tailscale OAuth client that can only create `tag:grok-box` auth keys, for slack22grokbot's repairs                                                                                                                                                             |
| `GROK_BOX_SHARED_DIR`                                            | Where the plugin leaves repair requests for slack22grokbot (default `$HERMES_HOME/grok-box-browser`)                                                                                                                                                             |
| `GROK_BOX_WAKE_TOKEN`                                            | grokbot-admin's bearer token, for waking the box. Unset, the plugin reads `GROK_BOT_ROSTER_MCP_TOKEN` from slack22grokbot's `$HERMES_HOME/beeperd/slack22grokbot/slack22grokbot.env` (beeperd's, since 2026-11-26), or the file `GROK_BOX_WAKE_TOKEN_FILE` names |
| `GROK_BOX_WAKE_URL`                                              | Default `https://grokbot-admin.leonsilicon.ai/sandbox/ensure`                                                                                                                                                                                                    |

It comes with the [leon plugin](../README.md#install). Set `browser.cloud_provider: grok-box` in `config.yaml` and restart the gateway when it's idle. The log shows `grok-box-browser: provider grok-box registered`.

## Broker API

`GET /health` needs no token. Everything else needs `Authorization: Bearer <token>`.

| Request                            | Does                                                                                 |
| ---------------------------------- | ------------------------------------------------------------------------------------ |
| `POST /v1/sessions {task}`         | Starts or reuses the task's Chrome; returns `ws` (browser WebSocket path) and `http` |
| `POST /v1/sessions/<task>/release` | Hermes is done for now; the Chrome stays until 30 idle minutes                       |
| `POST /v1/sessions/<task>/retire`  | Stops the Chrome; its profile is deleted 7 days later unless the task is used again  |
| `DELETE /v1/sessions/<task>`       | Stops the Chrome now, keeping the profile                                            |
| `GET /v1/sessions`                 | Every task with a profile: running, last used, seeded, retired                       |

Settings are environment variables: `GBB_HOME`, `GBB_PORT`, `GBB_MAX_BROWSERS`, `GBB_IDLE_MINUTES`, `GBB_RETAIN_DAYS`, `GBB_UNUSED_DAYS`, `GBB_COOKIE_SEED`, `GBB_CHROME`, `GBB_XVFB`, `GBB_HEADLESS`, `GBB_DISPLAY_BASE`, `GBB_CDP_BASE`, `GBB_TOKEN_FILE`.

## Test

The tests are `__tests__/grok_box_broker.test.mjs` and `__tests__/test_grok_box_browser.py` in the [leon plugin](../README.md#test), and both run in CI.

The broker tests use a stand-in Chrome. To try the broker against a real one on a Mac, run it with `GBB_HEADLESS=1` and `GBB_CHROME` pointing at Chrome's binary.
