# hermes22muse

Part of the [leon plugin](../README.md), and a Hermes plugin of its own until 2026-11-26.

Hermes can hand research and complex work to Muse side chats, review Muse’s answers, and reply in Slack. It can also work the [Muse app](#the-muse-app) as the user, including creating and managing scheduled tasks.

1. Hermes works out the request from the conversation and chooses the agents it needs. When it chooses Muse, Hermes writes a self-contained brief in the user’s own voice and sends it through `muse_handoff`.
2. The `slack22muse` app posts Muse’s replies as **Muse** in the channel thread. For a one-to-one DM with Hermes, it opens a separate thread in the **Muse** DM and links back to the original request. Hermes checks the replies, sends follow-ups with `muse_reply`, and uses `muse_wait` when Muse needs more than about five minutes.
3. Hermes posts brief progress replies in the task thread and edits its parent message with the reviewed answer. It answers Leon's later messages at the top of the issue channel with new messages in the channel. For ordinary channel messages outside issue channels, [slack-flat-replies](../slack_flat_replies/README.md) puts answers at the top level.

Replies in existing handoff threads keep their Muse context. Messages typed directly in the side chat appear in Slack with a “You in Muse” label. File bytes, edits and deletions do not sync. Treat both the thread and the side chat as visible to everyone with access to the Slack conversation.

## Agent routing

The plugin adds routing guidance to the configured user’s Slack turns. Update the profile’s `SOUL.md` and any task-handling skills that still prescribe a default agent or mandatory handoff. Use the same policy for turns outside Slack and run `hermes sessions refresh-prompts` after changing the profile.

> For each task, decide which agents can help and dispatch them as needed. Use Grok Bot whenever the work requires a browser, including signed-in sessions. Give Grok Bot only the browser steps. Use Muse for research and complex work. Set up Muse reminders and routines with `muse_schedule` so Hermes handles each run. Use Orca to start coding agents on the MacBook when it can take the task. Otherwise use a Codex cloud environment through ChatGPT Dots. Verify what you can in the cloud. If verification needs a phone or other local hardware, preserve the branch or commit and exact checks, then defer verification until Orca on the MacBook or Mac mini can run them.

In Slack, use [slack22orca](../slack22orca/README.md) 1.1.0 or later for the MacBook handoff and to queue deferred checks with `target: desktop`. Use the [Beeper inbox](../../beeperd/internal/handoff/README.md) (beeperd) to send a cloud handoff to Silicon Dot, and [slack22grokbot](../../beeperd/internal/grokrelay/README.md) for browser steps.

## Shared task thread and progress

On Leon's first request in a new issue channel, Hermes opens a task thread with a top-level message. Before starting the work, Hermes sets its quick answer with `slack_task_progress` and `quick_answer` (`plan` from 2.11–2.12 still works). It gives its best answer so far and what it will check, or says what it will do, in one to three sentences. The turn's routing guide asks for it first, and the first other tool Hermes calls on that request is turned away once until it is set. The quick answer appears on its own at the top of the message. An underlined **Progress** heading appears below it when Hermes has a checked update or an agent has replied.

Use the issue channel ID and the parent message's timestamp for each handoff. `slack22muse` posts as Muse in that thread. [slack22chatgpt](../../beeperd/internal/handoff/README.md) gives Codex the same thread through Slack Puppet, and [slack22grokbot](../../beeperd/internal/grokrelay/README.md) relays the thread to Grok Bot. Use the channel AutoLinear creates when the request starts in a DM.

- Start a reply with `To Muse:` to route a Grok Bot or Codex message to the existing Muse side chat. Hermes labels it as peer input. Retries and edits update the Slack record without forwarding the same reply again.
- Muse’s replies can start with `To Codex:` or `To Grok Bot:`. Codex reads the shared thread through Slack Puppet. Grok Bot receives peer replies through its conversation bridge. Hermes handles Leon’s follow-ups to Muse with `muse_reply`.
- Hermes calls `slack_task_progress` with `summary` as it reaches a milestone, verifies a finding, or encounters a blocker. The summary is posted as a reply in the task thread and shown as the latest progress on the parent. The parent also links to each agent's latest reply. Pending updates survive gateway restarts.
- Hermes adds its full answer below the quick answer on the parent when it finishes the opening request or responds to a message in the thread. Autolinear reads status commands from the full answer. When Leon writes again at the top of the issue channel, Hermes posts any progress in the existing thread and gives its final answer as a new message in the channel (Leon's rule, 2026-11-26).
- Existing collaborations keep the thread their agents joined. Their progress message links to that thread.

Update [slack-as-leon-guard](../slack_as_leon_guard/README.md) to 1.3.0 or later so reports and Hermes’s replies stay in the collaboration thread. Install the current Beeper inbox release and configure Slack Puppet for the Codex environments that will join.

## The Muse app

Hermes can also do what the user does in the Muse app, without a browser or screen control. The plugin speaks the app's own protocol: it trades a saved muse.ai login for a short-lived token, then calls the user's personal Muse computer over the app's encrypted WebSocket ([`museweb.py`](museweb.py)). It runs inside the Hermes gateway and needs no other machine.

| Tool               | What it does                                                                                                         |
| ------------------ | -------------------------------------------------------------------------------------------------------------------- |
| `muse_approvals`   | Lists what Muse is waiting for the user to approve (the app's **Needs review**), for one handoff or for every chat   |
| `muse_approve`     | Answers a request the way the app's buttons do: `allow_once`, `allow_always`, `deny` or `deny_always`                |
| `muse_app_methods` | Finds the app's methods by what they do. The table in [`muse-routes.json`](muse-routes.json) has all 444             |
| `muse_app`         | Calls any of them: chats, scheduled tasks, goals, ideas, feed, files, memory, skills, connected apps, settings, mail |
| `muse_file`        | Downloads a file Muse made to `$HERMES_HOME/hermes22muse/files/`                                                     |

These tools act as the user, so they only run on the user's own Slack messages and on jobs with no chat platform, such as scheduled ones. A message from a bot or another person gets an error.

Some actions wait for the user's yes in the conversation, which Hermes passes as `leon_confirmed`:

- Allowing an approval that spends or moves money, or one that doesn't say what it allows.
- Any "always" choice, since it changes what Muse may do from then on.
- App methods that erase data, share the account or change what Muse may do unasked: resets, the data export, permission and human-review settings, saved logins, mailbox creation and deletion, SSH access, peer connections and sharing.

Watching or steering Muse's live browser is the one part of the app the tools leave out, because it is a remote screen.

## Approvals

When Muse needs the user's approval in a side chat, Hermes answers it the way the user would. While a tool waits, the relay counts only that side chat's pending approvals: after about 20 seconds of work, every 30 seconds after that, and once more before an answer is returned. Pending approvals in other side chats, the main chat, scheduled tasks or spaces are ignored so they never block a handoff. A pending approval makes the tool return `waiting_for_approval` with the requests themselves. Hermes applies the user's approval rule: it calls `muse_approve` with `allow_once` when the action is what the user asked for, and otherwise denies it or asks the user with a mention.

Without a saved muse.ai login, the relay counts approvals through muse-mcp's `muse_chat_status` (requiring this chat's own live status to show approval waiting when an unpatched server reports an account-wide snapshot count) and Hermes answers them through Beeper's Muse bridge (`!muse approval-allow <code>`), which needs Hermes's Beeper inbox.

## Reminders and routines

Use `muse_schedule` for every one-time reminder and recurring routine. It asks Muse to save a schedule titled `Hermes handoff: <title>` whose entire prompt is a short acknowledgement. Keep the work instructions in the matching Linear Watcher. Verify the saved prompt, schedule ID and timing before confirming setup. Use `muse_app` to list, pause, resume or delete a schedule.

Hermes checks Muse's `tasks.list` and `tasks.runs` about once a minute using its saved muse.ai login. It records due runs of the trigger schedules locally and posts each to `#feed-muse` as Hermes. The feed post contains the title and Muse schedule and run IDs. Invite both Hermes and slack22linear to the channel. [Autotrigger](../../../packages/ai/leonsilicon/services/autotrigger/README.md#watchers) opens one Awaiting Hermes issue for the run. A Watcher whose body has a `Muse schedule: <id>` line handles that schedule. One Watcher with `Muse schedule: *` handles schedules without a specific Watcher. Simple reminders can put the reminder in the title. Put detailed work instructions in the specific Watcher.

To convert an older schedule, first preserve its work instructions in its Watcher. Then call `muse_schedule` with its existing `schedule_id` and a Slack-safe title. Check that Muse kept the schedule's cadence, time zone and enabled state and replaced the full prompt. Hermes starts observing a trigger schedule when it first sees the prefixed title. Enable the new routing before the first occurrence you want Hermes to handle.

Every ten minutes the relay also sends Muse's schedule list to autolinear's `/routines` with `AUTOLINEAR_TOKEN` from the Hermes profile (`HERMES22MUSE_ROUTINES_URL` overrides it). [slack22linear](../../../packages/ai/leonsilicon/services/slack22linear/README.md#routine-and-watcher-projects) maintains a **Routine** project linked with a `Muse routine: <id>` line for each repeating schedule and asks Hermes to reconcile missing links.

## Dropped turns

A gateway restart drops the turns it was running and never resumes them. On 2026-11-25 the gateway crashed with a segmentation fault 16 seconds after Muse answered LEON-509, while Hermes's turn waited in `muse_wait`. The answer sat in the thread, nothing moved the issue, and Leon found it an hour later. The relay now tells [autolinear](../../autolinear/README.md)'s Worker enough for Autolinear to remind Hermes:

- Hermes's turn hooks (`pre_llm_call`, `pre_tool_call`, `post_tool_call`, `on_session_end`) keep a list of the Slack chats with a running turn, in memory, so a restart empties it. A turn with no hook for ten minutes counts as ended. Every minute the relay posts the issue channels on that list to `/turns`. [slack22linear](../../../packages/ai/leonsilicon/services/slack22linear/README.md#what-it-does) reminds Hermes about a turn it took up (👀) that hasn't ended (✅ or ❌) five minutes after its channel last showed up there.
- A Muse answer counts as read once `muse_handoff`, `muse_reply` or `muse_wait` returns it as `answered`. When a finished answer in a channel stays unread while no turn runs there, for 20 seconds after Muse's last change and after the chat's last turn ended, the relay posts it to `/muse-answer` once. Autolinear then asks Hermes in the handoff's thread to read it with `muse_wait`. This also covers Hermes ending its turn without waiting for Muse. Answers Muse gives to Leon's own messages in the side chat, answers older than six hours, and DM handoffs are left alone. Answers from before 2.12.0 count as read.

Both use `AUTOLINEAR_TOKEN` from the Hermes profile's environment. `HERMES22MUSE_AUTOLINEAR_URL` overrides the Worker's address. Without the token nothing is sent, and the log says `could not report running turns` or `could not report unread Muse answers` when a post fails.

The plugin also points the faulthandler that Hermes enables at `$HERMES_HOME/logs/gateway_faulthandler.log`. The gateway's standard error goes to Hermes Cloud's console, which isn't kept, so the next crash leaves a stack of every thread there. `s6-svdt /run/hermes-service/gateway-default` lists how the gateway's recent runs ended.

## Sign Hermes in to muse.ai

The Muse app tools need the user's muse.ai login, saved once as `$HERMES_HOME/hermes22muse/muse-login.json` (mode 600). Give Hermes a login of its own: muse.ai can replace the login cookie whenever it is used and drop the old one, so a login shared with a browser signs one of them out.

1. Sign in at <https://muse.ai/> in a browser profile used for nothing else, and export its cookies as JSON (`agent-browser cookies get --json`, or the `Cookie:` request header of any muse.ai request).
2. Turn them into the login file. This also checks the login against muse.ai and finds the Muse computer:

   ```sh
   python3 museweb.py login /tmp/muse-login < cookies.json
   ```

3. Upload `/tmp/muse-login/muse-login.json` to `$HERMES_HOME/hermes22muse/` on the Hermes host, delete the local copy, and clear the browser profile's cookies without signing out there (signing out ends the login).

`python3 museweb.py call $HERMES_HOME/hermes22muse identity` checks it on the host. The relay reads the session every six hours, which makes muse.ai extend the login by 30 days, and saves every cookie muse.ai reissues at once. If muse.ai refuses the login twice, Hermes sends the user one Slack DM asking them to sign it in again; handoffs keep working meanwhile.

The method table comes from the Muse desktop app's own page. Refresh it after a Muse update with `python3 museweb.py routes /Applications/Muse.app/Contents/Resources/hatch/index.html`, keeping the size and time limits the old table has for methods whose limits the page names by variable.

## Set up Muse

Pair the [official Muse Gadget SDK Linux client](https://github.com/facebookincubator/muse-gadget-sdk/tree/fbfe3b0f717f7f468d0a94aa370ea0ca1a94ebfe/linux) on a Linux host with Bluetooth LE. Get an [SDK token](https://gadgets.muse.ai/settings/sdk-tokens), approve pairing in the Muse phone app, then check `sudo musegadget doctor` on the host.

Install [niclydon/muse-mcp](https://github.com/niclydon/muse-mcp) at `43299b2f4270e8994eff3978a72d1345abf93e4d` on the paired host. Follow its install instructions, using a service account that can read the SDK’s pairing state. A dedicated instance can start with `[]` in `directory.json`. This plugin registers each fresh side-chat ID in that directory and the first Link send opens it. The Muse-side bridge worker is only needed for other lifecycle operations such as archive or rename.

Run the server with `TRANSPORT=http` and its `ORIGIN_SHARED_SECRET` set to a random secret of at least 32 characters. Keep the endpoint private. Use loopback HTTP if it runs beside Hermes or is forwarded over SSH, or use HTTPS through a private network if it runs on another host. Set the server’s allowed hosts as described in its README when using a private hostname.

## Set up the Slack app

Create an app from the [slack22muse manifest](@assets/slack-app-manifest.json) and install it in the same workspace as Hermes. Slack shows the bot as **Muse**. The manifest lets it post to public channels and open a DM with the person who requested a handoff. Invite `@Muse` to each private channel where it should relay. Copy the app’s Bot User OAuth Token into `SLACK22MUSE_BOT_TOKEN` in the Hermes profile’s environment. [Slack app setup](https://docs.slack.dev/app-manifests/configuring-apps-with-app-manifests/)

## Set up Hermes

Use a Hermes gateway (v0.23.3 or later) with the native Slack adapter and the slack-flat-replies plugin, so answers to top-level channel messages go to the channel. The Hermes installation needs its MCP 2.x Python dependencies. The Muse app tools use `cryptography`, `httpx` and `websockets`, which Hermes ships. The Slack app needs `chat:write` for the conversations it handles.

Set these values in the Hermes profile’s environment and install the plugin. On Hermes Cloud, use the profile’s Keys page.

| Key                          | Value                                                  |
| ---------------------------- | ------------------------------------------------------ |
| `HERMES22MUSE_SLACK_USER_ID` | Slack user ID allowed to use the paired Muse account   |
| `HERMES22MUSE_MCP_URL`       | Private `https://…/mcp` or `http://127.0.0.1:7899/mcp` |
| `HERMES22MUSE_MCP_SECRET`    | The paired service’s `ORIGIN_SHARED_SECRET`            |
| `SLACK22MUSE_BOT_TOKEN`      | The slack22muse app’s Bot User OAuth Token (`xoxb-…`)  |

hermes22muse comes with the [leon plugin](../README.md#install), which includes autolinear. AutoLinear creates the issue and channel, and Hermes decides how to handle the request there. Replies in a Muse handoff thread stay there. Keep the separate AutoLinear Slack app’s message event subscriptions paused so intake starts in the gateway and can recognize those replies.

[Autodeploy](../../hermes-plugin-autodeploy/README.md) installs each new leon commit when CI asks, which reloads every plugin inside the running gateway, then restarts it once Hermes is idle. The reloaded hermes22muse keeps using the relay that is already running, so its tools, guidance and turn reports keep working, but new relay code waits for the restart. It finds that relay on `sys`, because Hermes doesn't always wire the reloaded copy on the live adapter (on 2026-11-26 it didn't, and autolinear reminded Hermes about turns still running). The relay reports Hermes's running turns to autolinear every minute, with no channels when none is running; autolinear only reminds Hermes about a turn missing from those reports while they keep coming. Look for `hermes22muse: Muse handoff relay registered`. Verify that a request in a new issue channel shows Hermes's quick answer within seconds, posts meaningful progress in its thread, and shows the full answer when it finishes. An explicit Muse research request relays the side chat in the task thread. For a DM request sent to Muse, check the separate conversation with Muse.

## Delivery and recovery

The SQLite database in `$HERMES_HOME/hermes22muse/relay.db` stores each handoff, the side chat's transcript, and pending deliveries in both directions with their receipts. The relay recognizes its own Muse echoes by their receipts, retries outages with backoff, and finds a Muse send whose receipt was lost by its exact text, unless it has already fallen outside the readable history. Messages carry no delivery marker. muse-mcp stamps every send with its provenance (`⟦via muse-mcp · from …⟧`); the [Muse Gadget launcher](../../muse-gadget/README.md) on Hermes Cloud removes that stamp so Muse sees only the user's words. If the Muse bot cannot post to Slack, the relay keeps those posts queued and Hermes can still work with Muse. Check the token and channel membership when the log reports a Slack delivery retry. If Slack accepted a thread post before its receipt could be stored, the post can repeat.

A Muse reply counts only once its row has a final status such as `completed`: while Muse streams, the row has no status and holds only the text so far. If Muse later changes a stored message, the transcript follows, and a reply first stored cut off is posted again in full as "Muse (full reply)". Muse counts as finished once its newest message is a completed reply and nothing has changed for eight seconds, the rule muse-mcp's `muse_chat_ask` uses, because agentic work can continue after a first answer. Each tool call waits up to 330 seconds, inside Hermes's default 420-second tool limit. The relay reads the side chat every five seconds while a handoff is in use, every minute for its first hour, every ten minutes for its first day, and then stops. The Link exposes the latest 50 events, and the relay posts a notice if a full window loses overlap. Oversized text is shortened with a link to the Slack thread.

Handoffs from 1.x keep their top-level threads, and messages they already relayed are not posted again. Hermes 2.0 no longer takes requests at gateway admission, so the user's replies in a handoff thread reach Hermes, which passes them on with `muse_reply`.

Its tests are `__tests__/test_hermes22muse.py` in the [leon plugin](../README.md#test), and run in CI.

## Muse projects evaluated

- [niclydon/muse-mcp](https://github.com/niclydon/muse-mcp) exposes classified chat sends and scoped transcript reads over authenticated MCP. It uses Meta’s paired Gadget transport and needs neither a desktop browser session nor a Muse worker for these operations.
- [finik/pi-muse-bridge](https://github.com/finik/pi-muse-bridge) uses the same Gadget transport and maps Pi sessions to separate Muse chats. Its integration is specific to Pi.
- [99apps-id/muse-relay-mcp](https://github.com/99apps-id/muse-relay-mcp) uses a file relay and a Muse-side polling worker to run questions and retrieve answers. The relay here needs transcript reads for messages entered directly into a side chat.
- [nikships/muse-cli](https://github.com/nikships/muse-cli) and its TypeScript port [OpenSWE/muse-bot](https://github.com/OpenSWE/muse-bot) call the Muse app's own gateway with a muse.ai login. Their protocol notes are the basis of [`museweb.py`](museweb.py), which keeps to the libraries Hermes already has. The gateway is unversioned: when a call starts failing, compare it with the current app.
