import { agentGroup, clientMessageId, groups, issueChannel, marker, slackPermalink } from "#slack";

interface Message {
  id: string;
  role: string;
  text: string;
}

interface EventMessage {
  session_id?: string;
  role?: string;
  status?: string;
  display_text_ready?: boolean | number;
  message_id?: string;
  display_text?: string;
  id?: string;
  content?: unknown;
  text?: string;
}

interface Event extends EventMessage {
  event_name?: string;
  payload?: EventMessage;
}

interface Response {
  ok?: boolean;
  chat_events?: Event[];
  events?: Event[];
  result?: Response;
  response?: Response;
}

// An assistant row is final only with one of these statuses. While Muse streams a reply its row
// has no status yet and holds the text so far, so a missing status is not finished.
const finished = new Set(["completed", "failed", "interrupted", "cancelled", "canceled", "error"]);

export function history(
  result: { ok?: boolean; scoped_to_chat?: string; response?: Response },
  chat: string,
  limit: number,
) {
  if (!result.ok || !result.scoped_to_chat) return { error: "scoped history unavailable" };
  let response = result.response;
  let events: Event[] | undefined;
  for (
    let depth = 0;
    depth < 5 && response && typeof response === "object" && response.ok !== false;
    depth++
  ) {
    const found = "chat_events" in response ? response.chat_events : response.events;
    if (Array.isArray(found)) {
      events = found;
      break;
    }
    response = "result" in response ? response.result : response.response;
  }
  if (!events) return { error: "unrecognized Link history response" };

  const messages = new Map<string, Message>();
  let busy = false;
  for (const event of events) {
    if (!event || typeof event !== "object") continue;
    const { event_name: name, payload } = event;
    if (
      (name !== "message.user" && name !== "message.assistant") ||
      !payload ||
      typeof payload !== "object" ||
      Array.isArray(payload)
    )
      continue;
    const role = name.slice("message.".length);
    const session = payload.session_id || event.session_id;
    if ((session && session !== chat) || (payload.role != null && payload.role !== role)) continue;
    if (
      [payload.display_text_ready, event.display_text_ready].some(
        (ready) => ready === false || ready === 0,
      ) ||
      (role === "assistant" && !finished.has(payload.status ?? ""))
    ) {
      // A reply still being written is held back until its final text is ready.
      busy ||= role === "assistant";
      continue;
    }
    const id = payload.message_id || event.message_id || payload.id;
    const text = [payload.display_text, event.display_text, payload.content, payload.text].find(
      (body): body is string => typeof body === "string" && Boolean(body.trim()),
    );
    if (typeof id === "string" && id && text) messages.set(id, { id, role, text });
  }
  return { messages: [...messages.values()], full: events.length >= limit, busy };
}

const timestamp = /^[0-9]+\.[0-9]{6}$/;
export function ownsRequest(
  user: string,
  owner: string,
  team: string,
  channel: string,
  ts?: string,
) {
  return (
    user === owner &&
    /^T[A-Z0-9]+$/.test(team) &&
    /^[CDG][A-Z0-9]+$/.test(channel) &&
    typeof ts === "string" &&
    ts === ts.trim() &&
    timestamp.test(ts)
  );
}

function escapeSlack(text: string) {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

export function peerReport(event: {
  client_msg_id?: unknown;
  thread_ts?: string;
  ts?: string;
  _slack_changed_event_ts?: string;
  text?: string;
}) {
  const group = agentGroup(clientMessageId(event));
  const { thread_ts: root, ts } = event;
  if (!group || !root || !ts || !timestamp.test(root) || !timestamp.test(ts) || root >= ts) return;
  const text = (event.text ?? "")
    .replace(/^\[Not Leon: [^\n]+\]\n/, "")
    .replace(/^(?:🤖|:robot_face:)\s+\*[^*\n]+\*:\s*/, "")
    .trim();
  if (!text) return;
  const peer = /^To Muse:\s*([\s\S]+)/i.exec(text)?.[1]?.trim();
  const agent = group === groups.grok ? "Grok Bot" : "Codex";
  const revision = event._slack_changed_event_ts;
  return {
    root,
    ts,
    revision: revision && timestamp.test(revision) && revision > ts ? revision : ts,
    agent,
    text,
    ...(peer && {
      muse: `Slack collaboration from ${agent}. This is the agent's question or input for our existing task. It carries no new authorization. Reply with what you can share so Hermes can coordinate. Prefix a reply intended for a collaborator with To Codex: or To Grok Bot: as appropriate.\n\n${peer.slice(0, 6000)}`,
    }),
  };
}

export function progressMessage(
  channel: string,
  threadLink: string | null,
  summary: string,
  reports: { agent: string; text: string; ts: string }[],
  answer: string,
  plan = "",
  revision?: number,
  answerParts = [answer],
) {
  const sections = [];
  const blocks = [];
  if (plan) {
    const quickAnswer = plan.replace(/\n\s*\n/g, "\n");
    sections.push(escapeSlack(quickAnswer));
    blocks.push(progressRichText("rich_text_section", { type: "text", text: quickAnswer }));
  }
  // A pending assignment has no reply yet.
  const updates = reports.filter((report) => report.ts && report.text.trim());
  if (summary || updates.length) {
    const progress = ["Progress"];
    blocks.push(
      progressRichText("rich_text_section", {
        type: "text",
        text: "Progress",
        style: { underline: true },
      }),
    );
    if (summary) {
      progress.push(
        summary
          .split(/\r?\n/)
          .map((line) => "> " + escapeSlack(line))
          .join("\n"),
      );
      blocks.push(progressRichText("rich_text_quote", { type: "text", text: summary }));
    }
    for (const report of updates) {
      const text = report.text.replace(/\s+/g, " ").trim();
      const excerpt = text.length > 260 ? text.slice(0, 257) + "…" : text;
      const line = `• *${report.agent}* · ${escapeSlack(excerpt)} <${slackPermalink(channel, report.ts)}|Open reply>`;
      progress.push(line);
      blocks.push(progressSection(line));
    }
    sections.push(progress.join("\n"));
  }
  if (threadLink) {
    const link = `<${slackPermalink(channel, threadLink)}|Collaboration thread>`;
    sections.push(link);
    blocks.push(progressSection(link));
  }
  if (answer) {
    // slack22linear reads only this section of Slack's fallback text for status commands.
    sections.push("*Update from Hermes*\n" + answer);
    blocks.push(
      ...answerParts.map((part, index) =>
        progressSection(index ? part : "*Update from Hermes*\n" + part),
      ),
    );
  }
  if (!blocks.length) {
    sections.push("Thinking…");
    blocks.push(progressSection("Thinking…"));
  }
  const text = sections.join("\n\n");
  // A Node worker restarted before the gateway can load this bundle for the previous relay.
  if (revision === undefined) return `*Task progress*\n\n${text}`;
  const blockId = `hermes-task-progress/${answer ? "answer/" : ""}${revision}`;
  return {
    text,
    blocks: [{ ...blocks[0], block_id: blockId }, ...blocks.slice(1)],
  };
}

function progressRichText(
  type: "rich_text_section" | "rich_text_quote",
  element: {
    type: "text";
    text: string;
    style?: { underline: boolean };
  },
) {
  return { type: "rich_text", elements: [{ type, elements: [element] }] };
}

function progressSection(text: string) {
  return { type: "section", text: { type: "mrkdwn", text, verbatim: true } };
}

/**
 * Hermes's notices about a message it didn't take in (gateway/session_ingress.py): one already
 * received with other content, one it refused, a full queue, a draining gateway. Each answers the
 * message that was turned away, never the task. LEON-540 (2026-11-26): an unfurl edit of Leon's
 * message became a second turn on the task's message id, Hermes refused it, and "⚠️ Hermes already
 * received a different message under this message id…" replaced the answer on Task progress.
 */
const ADMISSION_NOTICE =
  /^\s*(?::[a-z0-9_+-]+:|\p{Extended_Pictographic}\uFE0F?)\s*(?:Hermes already received a different message under this message id|Hermes refused this message|Too many messages are already waiting|Gateway is \S[^\n]* and is not accepting new work)/u;

export function publishTaskAnswer(text: string) {
  return (
    !!text.trim() && !/^\s*To (?:Muse|Codex|Grok Bot):/i.test(text) && !ADMISSION_NOTICE.test(text)
  );
}

export function slackChunks(text: string) {
  // Count Unicode code points and leave room for the relay label.
  const rest = Array.from(text);
  const chunks = [];
  for (let start = 0; start < rest.length;) {
    let cut = Math.min(rest.length - start, 3400);
    while (Array.from(escapeSlack(rest.slice(start, start + cut).join(""))).length > 3400)
      cut = Math.floor(cut / 2);
    if (start + cut < rest.length) {
      // Look back over half the chunk only. lastIndexOf goes on to the start of the text, which
      // made one long line cost the square of its length.
      const earliest = start + Math.floor(cut / 2);
      for (let index = start + cut - 1; index >= earliest; index--) {
        if (rest[index] === "\n") {
          cut = index + 1 - start;
          break;
        }
      }
    }
    chunks.push(escapeSlack(rest.slice(start, start + cut).join("")));
    start += cut;
  }
  return chunks;
}

export function slackDelivery(message: Message, echoed: boolean, stamp: string) {
  if (message.role === "user" && (echoed || message.text.startsWith(stamp + "[Hermes delivery ")))
    return [null];
  const label = message.role === "assistant" ? "Muse" : "You in Muse";
  return slackChunks(message.text).map((chunk) => label + "\n" + chunk);
}

export function sideChat(handoff: string) {
  return "https://muse.ai/thread/" + handoff;
}

export function museDmOpening(text: string, channel: string, thread: string) {
  return text + "\n\nOriginal Slack thread: " + slackPermalink(channel, thread);
}

/**
 * What Muse receives for a new handoff, and the thread post that shows it. Muse gets the task
 * exactly as written, as a message from Leon, with no relay framing.
 */
export function handoffTexts(task: string, channel: string, _ts: string, handoff: string) {
  const slackReply = channel.startsWith("D") ? "\nMuse will send replies in its own Slack DM." : "";
  return {
    muse: task,
    thread: "Sent to Muse as you. Side chat: " + sideChat(handoff) + slackReply + "\n\n" + task,
  };
}

export function followUpThread(text: string) {
  return "Sent to Muse as you\n" + text;
}

export function handoffGuide(owner: string) {
  return [
    "[Agent routing] For each task, decide which agents can help and dispatch them as needed. Handle routine work you can complete yourself. Combine agents when the task needs different capabilities, and keep ownership of the outcome.",
    'Work out what Leon is asking from the conversation, channel history, Linear issue and relevant links. Interpret short follow-ups such as "continue" in that context. Ask one specific clarifying question and mention <@' +
      owner +
      "> if the missing information blocks progress.",
    "- Use Grok Bot whenever the work requires a browser, including authenticated browser sessions. Send only the browser steps through grok_bot_send and bring the results back to the task.",
    "- Use Muse for research, complex work and scheduled tasks. Use muse_handoff for the selected work, with a self-contained brief written in Leon's first person. When collaborating, tell Muse which peers are involved and to prefix a question for them with To Codex: or To Grok Bot:. Call muse_reply for follow-ups and muse_wait while Muse is working. Use muse_app_methods and muse_app to manage scheduled tasks in Muse.",
    "- Recurring work is a routine and a standing watch is a watcher, each its own Linear project (Leon's rule, 2026-11-25). A routine is a scheduled task in Muse: have Muse create it with muse_handoff, and manage it with muse_app (tasks.list, tasks.pause, tasks.resume, tasks.delete). Within about ten minutes slack22linear gives it a project in the Routine status whose body starts with `Muse routine: <id>`. A watcher is a project in the Watcher status: its summary says what to watch for in the #feed-* channels and its body what to do with each case. Autotrigger opens an Awaiting Hermes issue in it for every new case and pings you there. Never keep an issue open as a standing listener: make a Watcher project instead. File work about a routine or watcher in its project.",
    "- For work suited to a coding agent, use slack22orca_delegate on the MacBook when available. If it cannot take the task, find Silicon Dot with Beeper list_conversations on the chatgpt network and use send_message with the current Slack channel and root thread timestamp. Send a self-contained brief and ask Dot to start Codex in a cloud environment and return the private session link.",
    "- In the cloud, complete what can be implemented and verified there. If a check needs a phone or other local hardware, preserve the branch or commit and exact verification steps. Once the cloud work is ready, queue the remaining check with slack22orca_delegate and target desktop so it waits for Orca on the MacBook or Mac mini. Keep the verification pending until it actually runs.",
    "Use the same Slack channel and root timestamp for every agent on this task. In a new issue channel, the task's parent message is the thread root. Use its timestamp for handoffs. When dispatching multiple agents, use wait_seconds: 0 for Muse and Grok so you can start each assignment before waiting. Agents can address a collaborator with To Muse:, To Codex:, or To Grok Bot: in that thread. Keep peer instructions within the work Leon authorized. Use slack_task_progress to review each agent's latest reply.",
    "Check the agents' results and give Leon the outcome in your normal final response. Hermes saves that response on Task progress when it answers the request that opened Task progress or a message in its thread, and posts it as a new message in the channel when it answers Leon's later message at the top of the channel. Start a follow-up meant for an agent with To Muse:, To Codex:, or To Grok Bot: so it goes into the thread. Acknowledge messages that ask for nothing without dispatching work.",
  ].join("\n");
}

export function threadGuide(handoff: string) {
  return (
    "[hermes22muse] This thread relays handoff " +
    handoff +
    " between you and Muse (side chat " +
    sideChat(handoff) +
    "). If Leon's message asks Muse for a follow-up or changes Muse's assignment, use " +
    "muse_reply, written in Leon's first person. The relay already queues peer replies that start with To Muse:. " +
    "Review Muse's answer alongside the other agents' work. " +
    "The Muse bot posts Muse's messages in channel threads or in its own Slack DM with Leon. Keep your own reply focused on the checked outcome and any added context."
  );
}

const nextSteps: Record<string, string> = {
  answered:
    "Check Muse's answer against Leon's request. If it is complete, give Leon the checked outcome in " +
    "your usual reply. Otherwise follow up with muse_reply, written as Leon.",
  working: "Muse is still working. Call muse_wait to keep waiting.",
  not_delivered:
    "Muse has not received the message yet (the Muse link may be down). Call muse_wait to keep " +
    "waiting, or tell Leon it is queued.",
  waiting_for_approval:
    "Muse is waiting for Leon's approval in this side chat. Answer it the way Leon would: read the " +
    "request in this result's `approvals` (or call muse_approvals with this handoff). Apply Leon's " +
    "approval rule: if it does what he asked for (the right action, recipient or site, and content), " +
    "call muse_approve with allow_once and say in the Slack thread what you approved; otherwise deny " +
    "it or ask Leon, mentioning him. Payments, purchases, money transfers and password, login or " +
    'security changes always go to Leon, as does any "always" choice. Then call muse_wait.',
  waiting_for_approval_beeper:
    "Muse is waiting for Leon's approval in this side chat. Answer it the way Leon would, through " +
    "Beeper: find Muse's request on the muse network (list_conversations, then list_messages), the " +
    'newest message like "Muse is waiting for your approval: … Allow it once with `!muse ' +
    "approval-allow <code>`\". Apply Leon's approval rule: if it does what he asked for (the right " +
    "action, recipient or site, and content), send exactly `!muse approval-allow <code>` in that " +
    "chat with send_message and say in the Slack thread what you approved; otherwise send `!muse " +
    "approval-deny <code>` or ask Leon, mentioning him. Payments, purchases, money transfers and " +
    "password, login or security changes always go to Leon. If no request has reached Beeper " +
    "within a minute, tell Leon what Muse is waiting for and mention him. Then call muse_wait.",
};

/** `app` says whether Hermes can reach Muse's own app gateway; without it approvals go through Beeper. */
export function nextStep(status: string, app = true) {
  return nextSteps[status === "waiting_for_approval" && !app ? status + "_beeper" : status] ?? "";
}

// Poll a handoff often while it is in use, then back off; one idle for a day stops polling.
const polls: [number, number][] = [
  [300, 5],
  [3600, 60],
  [86400, 600],
];

export function pollDelay(idleSeconds: number, pending: boolean, never: number) {
  if (pending) return 1;
  return polls.find(([age]) => idleSeconds < age)?.[1] ?? never;
}

export function retryDelay(failures: number, retryAfter: string) {
  const backoff = Math.min(300, 5 * 2 ** Math.min(failures, 6));
  return /^\d+$/.test(retryAfter) ? Math.max(backoff, Number(retryAfter)) : backoff;
}

interface SlackTurn {
  platform: string;
  user_id: string;
  scope_id: string;
  chat_id: string;
  thread_id?: string;
  message_id: string;
}

interface Handoff {
  id: string;
  channel: string;
  root: string;
  actor: string;
  request: string;
}

type Reply = { from: string; text: string };
type Answer = { status: string; messages: Reply[] };

export function slackTurn(current: SlackTurn | undefined, owner: string) {
  if (
    current?.platform === "slack" &&
    ownsRequest(current.user_id, owner, current.scope_id, current.chat_id, current.message_id) &&
    (!current.thread_id ||
      (current.thread_id === current.thread_id.trim() && timestamp.test(current.thread_id)))
  )
    return current;
}

export function startsTaskThread(
  event: {
    user?: string;
    ts?: string;
    thread_ts?: string;
    bot_id?: string;
    client_msg_id?: unknown;
    _hermes_reaction?: unknown;
    _hermes_force_process?: unknown;
  },
  current: SlackTurn,
  owner: string,
  channelName: string,
  prefix: string,
) {
  return (
    slackTurn(current, owner) &&
    /^[CG]/.test(current.chat_id) &&
    issueChannel(channelName, prefix) &&
    event.user === owner &&
    event.ts === current.message_id &&
    (!event.thread_ts || event.thread_ts === event.ts) &&
    !event.bot_id &&
    !event._hermes_reaction &&
    !event._hermes_force_process &&
    !clientMessageId(event).startsWith(marker + "-")
  );
}

/**
 * Shown on Leon's request that opened a Task progress message, which is empty until Hermes answers.
 * Leon asked for a short answer within seconds and the full one when the work is done (2026-11-25).
 */
export function planGuide() {
  return (
    "[Task progress] This request opened the issue's task thread. Leon is waiting for your first answer. " +
    "Before any other tool, call slack_task_progress with quick_answer: your short first answer to Leon, in one to " +
    "three sentences, from what you already know. He reads it within seconds, then waits for your full answer. " +
    "If he asked a question, give your best answer now and say what you'll check before the full answer; call a guess " +
    "a guess. If he asked you to do something, say what you'll do (for example: I'll find the recurring event on your " +
    "calendar, delete every occurrence, and check that none are left). Never say something is done or confirmed " +
    "before you have checked it. Call it straight away, without looking it up: quick_answer is the only argument it " +
    'needs (through tool_call if it is deferred: calls [{name: "slack_task_progress", arguments: {quick_answer: ' +
    '"…"}}]). Then do the work and give the full answer in your final response as usual.'
  );
}

/** The tool result when Hermes starts work on a new task before giving Leon its quick answer. */
export function planFirst() {
  return (
    "Call slack_task_progress with quick_answer (one to three " +
    "sentences: your best answer so far and what you'll check, or what you'll do), then call this tool again."
  );
}

/**
 * Shown on Leon's later message at the top of an issue channel. Its turn runs in the Task progress
 * thread's conversation, but only that thread's messages change the card (his rule, 2026-11-26).
 */
export function followUpGuide() {
  return (
    "[Task progress] Leon wrote this message at the top of the issue channel, not in the Task progress thread. " +
    "Hermes posts your final response as a new message in the channel, below his, and Task progress keeps " +
    "what it shows: only the request that opened it and messages in its thread change it."
  );
}

/** The slack_task_progress result when Hermes tries to change the card from such a message's turn. */
export function followUpProgress() {
  return (
    "Not changed: Leon wrote this message at the top of the channel, outside the Task progress thread, and only " +
    "messages in that thread change Task progress (Leon's rule, 2026-11-26). Give your answer as your final " +
    "response: Hermes posts it as a new message in the channel."
  );
}

export function guideForTurn(
  current: SlackTurn,
  owner: string,
  existing?: Handoff,
  needsPlan = false,
  followUp = false,
  taskThread = false,
) {
  const guides = [handoffGuide(owner)];
  if (existing && existing.request !== current.message_id) guides.push(threadGuide(existing.id));
  if (taskThread)
    guides.unshift(
      "While you work, post brief progress replies for Leon in the task thread by calling slack_task_progress " +
        "with summary as soon as you have a meaningful milestone, verified finding, or blocker. Say what you found " +
        "or accomplished and what is still pending. Share material progress before a long wait, and skip repeated updates. " +
        "Give the complete outcome in your final response.",
    );
  if (followUp) guides.unshift(followUpGuide());
  if (needsPlan) guides.unshift(planGuide());
  return { context: guides.join("\n\n") };
}

export function answerRows(
  job: { busy: boolean; polled: number; changed: number } | undefined,
  receipt: string | undefined,
  rows: { role: string; text: string }[],
  quiet: number,
): Answer {
  if (!job || !receipt) return { status: "not_delivered", messages: [] };
  const answered =
    rows.at(-1)?.role === "assistant" && !job.busy && job.polled - job.changed >= quiet;
  return {
    status: answered ? "answered" : "working",
    messages: rows.map((row) => ({
      from: row.role === "assistant" ? "muse" : "leon_in_muse",
      text: row.text,
    })),
  };
}

export interface Approvals {
  /** Pending approvals Muse reported, or null when its live snapshot didn't say. */
  count: number | null;
  /** When the count was measured, when Hermes last asked for a measurement, and when the transcript last changed (epoch seconds). */
  checked: number;
  wanted: number;
  changed: number;
}

/**
 * What a waiting tool does about Muse's approvals: report a pending one, ask the relay to measure,
 * finish ("clear"), or keep waiting. Muse is measured after about 20 s of work and every 30 s after,
 * and once more before an answer is returned, since Muse can answer and still wait on an approval.
 * A measurement that doesn't arrive within 20 s never holds back an answer.
 */
export function approvalDecision(
  approvals: Approvals,
  status: string,
  now: number,
  waited: number,
): "pending" | "check" | "clear" | "wait" {
  const measured = approvals.wanted > 0 && approvals.checked >= approvals.wanted;
  if (measured && (approvals.count ?? 0) > 0 && now - approvals.checked < 60) return "pending";
  const outstanding = approvals.wanted > approvals.checked;
  if (status === "answered") {
    if (measured && approvals.checked >= approvals.changed) return "clear";
    if (outstanding) return now - approvals.wanted > 20 ? "clear" : "wait";
    return "check";
  }
  if (status === "working" && waited >= 20 && !outstanding && now - approvals.wanted >= 30)
    return "check";
  return "wait";
}

interface ToolHost {
  lookup(id: string): Promise<Handoff | undefined>;
  find(
    team: string,
    channel: string,
    request?: string,
    thread?: string,
  ): Promise<Handoff | undefined>;
  handoffId(team: string, channel: string, message: string): string;
  create(
    team: string,
    channel: string,
    request: string,
    thread: string,
    actor: string,
    museText: string,
    threadText: string,
  ): Promise<string | undefined>;
  followUp(id: string, text: string, threadText: string): Promise<unknown>;
  answer(id: string): Promise<Answer>;
  approvals(id: string): Promise<Approvals>;
  wantApprovals(id: string): Promise<void>;
  clearApprovals?(id: string): Promise<void>;
  touch(id: string): Promise<void>;
  logHandoff(channel: string, message: string, job: string): void;
  now(): number;
  /** Wall-clock epoch seconds, the store's time base. */
  clock(): number;
  sleep(): Promise<void>;
}

export async function runTool(
  host: ToolHost | undefined,
  tool: "handoff" | "reply" | "wait",
  args: { task?: string; text?: string; handoff?: string },
  session: SlackTurn | undefined,
  owner: string,
  seconds: number,
  app = true,
) {
  if (!host) return { error: "The Muse relay is not running in this gateway." };
  const current = slackTurn(session, owner);
  let job: Handoff | undefined;
  if (tool === "handoff") {
    if (!current) return { error: "muse_handoff only hands over Leon's own Slack messages." };
    const task = String(args.task || "").trim();
    if (!task)
      return { error: "task is empty. Write the complete task, or ask Leon what he needs." };
    const { scope_id: team, chat_id: channel, message_id: message } = current;
    const thread = current.thread_id || message;
    const existing = await host.find(team, channel, message, thread);
    if (existing)
      return {
        error:
          "This message already belongs to handoff " +
          existing.id +
          ". Use muse_reply to send Muse more, or muse_wait to wait for its answer.",
      };
    const texts = handoffTexts(task, channel, message, host.handoffId(team, channel, message));
    const id = await host.create(team, channel, message, thread, owner, texts.muse, texts.thread);
    if (!id) return { error: "This Slack thread already has a handoff. Use muse_reply instead." };
    host.logHandoff(channel, message, id);
    job = await host.lookup(id);
  } else {
    const text = String(args.text || "").trim();
    if (tool === "reply" && !text) return { error: "text is empty." };
    const handoff = String(args.handoff || "").trim();
    if (handoff) {
      const match = await host.lookup(handoff);
      if (match?.actor === owner) job = match;
    } else if (current) {
      job = await host.find(
        current.scope_id,
        current.chat_id,
        current.message_id,
        current.thread_id,
      );
    }
    if (job && tool === "reply") await host.followUp(job.id, text, followUpThread(text));
  }
  if (!job) return { error: "No handoff found. Pass the handoff ID that muse_handoff returned." };
  const started = host.now();
  const deadline = started + seconds;
  for (let polls = 0; ; polls++) {
    const answer = await host.answer(job.id);
    const approvals = await host.approvals(job.id);
    const decision = approvalDecision(approvals, answer.status, host.clock(), host.now() - started);
    const status =
      decision === "pending"
        ? "waiting_for_approval"
        : answer.status === "answered" && decision !== "clear"
          ? "working"
          : answer.status;
    if (decision === "pending" || decision === "clear" || host.now() >= deadline) {
      if (decision === "pending") await host.clearApprovals?.(job.id);
      return {
        status,
        handoff: job.id,
        side_chat: sideChat(job.id),
        slack_thread: slackPermalink(job.channel, job.root),
        messages: answer.messages,
        ...(decision === "pending" ? { approvals_pending: approvals.count } : {}),
        next: nextStep(status, app),
      };
    }
    if (decision === "check") await host.wantApprovals(job.id);
    else if (polls % 3 === 0) await host.touch(job.id);
    await host.sleep();
  }
}

interface InboundHost {
  register(id: string): Promise<void>;
  exists(id: string): Promise<boolean>;
  history(id: string): Promise<Message[]>;
  attempted(id: string, ts: string): Promise<void>;
  accepted(id: string, ts: string, receipt: string): Promise<void>;
  send(id: string, text: string): Promise<string>;
  byteLength(text: string): number;
  clipUtf8(text: string, bytes: number): string;
}

export async function sendInbound(
  host: InboundHost,
  job: Handoff,
  entry: { ts: string; text: string; attempted: boolean },
  stamp: string,
) {
  const first = entry.ts === job.request;
  let text = entry.text;
  if (host.byteLength(stamp + text) > 64 * 1024) {
    text =
      host.clipUtf8(text, 60 * 1024) +
      "\n\n[Shortened. The rest is in the Slack thread] " +
      slackPermalink(job.channel, job.root);
  }
  if (first) await host.register(job.id);
  if (entry.attempted && (!first || (await host.exists(job.id)))) {
    // Messages carry no marker, so a send whose receipt was lost is recognized by its text.
    const history = await host.history(job.id);
    for (let index = history.length - 1; index >= 0; index--) {
      const message = history[index];
      if (message.role === "user" && (message.text === text || message.text === stamp + text)) {
        await host.accepted(job.id, entry.ts, message.id);
        return;
      }
    }
  }
  await host.attempted(job.id, entry.ts);
  await host.accepted(job.id, entry.ts, await host.send(job.id, text));
}

export function collectPlan(
  messages: Message[],
  existing: { id: string; text: string }[],
  posted: string[],
  echoed: string[],
  full: boolean,
  busy: boolean,
  oldBusy: boolean | undefined,
  stamp: string,
) {
  const known = new Map(existing.map((row) => [row.id, row.text]));
  const alreadyPosted = new Set(posted);
  const inbound = new Set(echoed);
  const rows = messages.filter((message) => !known.has(message.id));
  // Muse can still change a stored message's text; keep the transcript current.
  const updates = messages.filter(
    (message) => known.has(message.id) && known.get(message.id) !== message.text,
  );
  const output: { id: string; parts: string[] }[] = [];
  if (full && !known.size && !alreadyPosted.size && messages.length) {
    output.push({
      id: "gap/" + messages[0].id,
      parts: [
        "Muse’s recent history filled its read window. Some earlier messages may be missing.",
      ],
    });
  }
  for (const message of rows) {
    if (alreadyPosted.has(message.id)) continue;
    const parts = slackDelivery(message, inbound.has(message.id), stamp);
    if (parts[0] !== null) output.push({ id: message.id, parts: parts as string[] });
  }
  for (const message of updates) {
    const before = known.get(message.id) ?? "";
    // A reply first stored cut off is posted again in full.
    if (
      message.role === "assistant" &&
      message.text.length > before.length &&
      message.text.startsWith(before)
    )
      output.push({
        id: "update/" + message.id + "/" + message.text.length,
        parts: slackChunks(message.text).map((chunk) => "Muse (full reply)\n" + chunk),
      });
  }
  const changes = rows.length + updates.length;
  return {
    rows,
    updates,
    output,
    activity: changes > 0,
    changed: changes > 0 || (oldBusy !== undefined && oldBusy !== busy),
  };
}

interface DirectoryEntry {
  in_directory?: boolean;
  sendable?: boolean;
  entry?: { kind?: string };
  directory_revision?: string;
  revision?: string;
}

export function directoryRegistration(current: DirectoryEntry) {
  if (current.in_directory) {
    if (current.sendable && current.entry?.kind === "side") return {};
    return { error: "registered side chat is suspended or has an unexpected kind" };
  }
  if (current.entry) return { error: "side chat ID is already present in Muse's registry" };
  const revision = current.directory_revision || current.revision;
  return revision ? { revision } : { error: "Muse directory revision unavailable" };
}

// --- Muse's app gateway: approvals and every other method of the Muse app ---

interface PreviewBlock {
  type?: string;
  label?: string | null;
  text?: string;
  rows?: { label?: string; value?: string }[];
  items?: { label?: string; url?: string }[];
}

interface DecisionOption {
  kind?: string;
  label_text?: string;
  label?: { scope?: string };
}

function decisionOptions(approval: RawApproval): DecisionOption[] {
  return (approval.decision_options ?? []).map((option) =>
    typeof option === "string" ? { kind: option } : option,
  );
}

interface MuseStatusLive {
  pending_approval_count?: unknown;
  pending_approvals_scoped?: unknown;
  agent_activity_code?: unknown;
  task_status?: unknown;
}

const approvalTaskStatuses = new Set([
  "pending_user_confirmation",
  "background_pending_user_confirmation",
]);

/**
 * Pending approvals for this side chat from muse-mcp's muse_chat_status.
 * Upstream muse-mcp counts every approval in Muse's account-wide approvals.snapshot without
 * filtering by chat; unless the server marks the count scoped, a nonzero count only counts for
 * this chat when this chat's own live state also says it is waiting for approval.
 */
export function mcpPendingApprovals(result: unknown): number | null {
  const live =
    result && typeof result === "object"
      ? ((result as { live?: unknown }).live as MuseStatusLive | undefined)
      : undefined;
  const count = live?.pending_approval_count;
  if (typeof count !== "number" || !Number.isInteger(count) || count < 0) return null;
  if (count === 0 || live?.pending_approvals_scoped === true) return count;
  const waiting =
    live?.agent_activity_code === "needs_approval" ||
    (typeof live?.task_status === "string" && approvalTaskStatuses.has(live.task_status));
  return waiting ? count : 0;
}

interface RawApproval {
  approval_id?: string;
  approval_type?: string;
  status?: string;
  requested_at_ms?: number;
  request_expires_at_ms?: number;
  thread_id?: string;
  session_id?: string;
  message_revision?: number;
  /** Muse sends the choices as names, or as objects with a label. */
  decision_options?: (string | DecisionOption)[];
  reason?: string;
  short_explanation?: string;
  action_label?: string;
  display_name?: string;
  host?: string;
  method?: string;
  path?: string;
  policy_subject?: string;
  runtime_request_trace?: { thread_id?: string; session_id?: string };
  display?: {
    display_name?: string;
    card_title?: string;
    summary_title?: string;
    permission_question?: { kind?: string; text?: string };
    scope_summary?: string;
    purpose_summary?: string;
    reason?: string;
    request_preview_document?: { blocks?: PreviewBlock[] };
  };
  scope?: Record<string, unknown>;
  payload?: {
    type?: string;
    connector_payload?: { host?: string; method?: string; path?: string };
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

const moneyWords = /purchase|checkout|payment|wallet|order|refund|transfer|subscription/i;

function assistantName(text: string | undefined) {
  return (text ?? "").replaceAll("{assistant}", "Muse");
}

function previewLines(blocks: PreviewBlock[] | undefined) {
  const lines: string[] = [];
  for (const block of blocks ?? []) {
    for (const row of block.rows ?? []) lines.push((row.label ?? "") + ": " + (row.value ?? ""));
    if (block.text) lines.push((block.label ? block.label + ": " : "") + block.text);
    for (const item of block.items ?? []) lines.push((item.label ?? "") + " " + (item.url ?? ""));
  }
  return lines.map((line) => line.slice(0, 2000)).slice(0, 40);
}

/** The side chat an approval belongs to, wherever this Muse version records it. */
function approvalChat(approval: RawApproval) {
  const scope = approval.scope ?? {};
  const scopeTrace = scope.runtime_request_trace as
    | { thread_id?: unknown; session_id?: unknown }
    | undefined;
  for (const value of [
    approval.runtime_request_trace?.thread_id,
    approval.runtime_request_trace?.session_id,
    scopeTrace?.thread_id,
    scopeTrace?.session_id,
    approval.thread_id,
    approval.session_id,
    scope.thread_id,
    scope.session_id,
    (scope.thread as { id?: unknown } | undefined)?.id,
    (scope.session as { id?: unknown } | undefined)?.id,
  ]) {
    if (typeof value === "string") {
      const trimmed = value.trim();
      if (trimmed) return trimmed;
    }
  }
}

/** Whether Leon has to decide: it spends money, or the request doesn't say what it is. */
function approvalNeedsLeon(approval: RawApproval) {
  const display = approval.display ?? {};
  const question = display.permission_question?.kind ?? "";
  if (
    [approval.approval_type, question, approval.method, approval.policy_subject].some((text) =>
      moneyWords.test(text ?? ""),
    )
  )
    return "it spends or moves money";
  for (const key of Object.keys(approval))
    if (/checkout|payment/i.test(key) && approval[key] && typeof approval[key] === "object")
      return "it spends or moves money";
  if (!display.card_title && !display.permission_question?.text && !approval.reason)
    return "it does not say what it allows";
}

export function approvalSummary(approval: RawApproval) {
  const display = approval.display ?? {};
  const connector = approval.payload?.connector_payload;
  const leon = approvalNeedsLeon(approval);
  return {
    approval: approval.approval_id,
    side_chat_id: approvalChat(approval),
    asks: assistantName(display.card_title || display.permission_question?.text || approval.reason),
    kind:
      display.permission_question?.kind ??
      approval.approval_type ??
      approval.method ??
      approval.payload?.type,
    summary: assistantName(
      display.scope_summary || display.summary_title || approval.short_explanation,
    ),
    purpose: display.purpose_summary || display.reason || approval.action_label,
    details: previewLines(display.request_preview_document?.blocks),
    target: connector
      ? [display.display_name, connector.host, connector.method].filter(Boolean).join(" · ")
      : [display.display_name ?? approval.display_name, approval.path].filter(Boolean).join(" · "),
    options: decisionOptions(approval).map((option) => ({
      decision: option.kind,
      ...(option.label_text ? { label: option.label_text } : {}),
      ...(option.label?.scope ? { scope: option.label.scope } : {}),
    })),
    requested: approval.requested_at_ms
      ? new Date(approval.requested_at_ms).toISOString()
      : undefined,
    expires: approval.request_expires_at_ms
      ? new Date(approval.request_expires_at_ms).toISOString()
      : undefined,
    ...(leon ? { leon_decides: leon } : {}),
  };
}

function pendingApprovals(response: unknown): RawApproval[] {
  const body = (response ?? {}) as {
    pending_approvals?: unknown;
    pending?: unknown;
    approvals?: unknown;
  };
  const list = body.pending_approvals ?? body.pending ?? body.approvals;
  return Array.isArray(list)
    ? list.filter(
        (item) => item && typeof item === "object" && (item.status ?? "pending") === "pending",
      )
    : [];
}

/**
 * Muse's pending approvals. With a side chat, only the ones that belong to that side chat:
 * approvals from other side chats, the main chat, scheduled tasks or spaces are left out so
 * they never block or bleed into this handoff.
 */
export function approvalList(response: unknown, chat?: string) {
  const all = pendingApprovals(response);
  const mine = chat ? all.filter((approval) => approvalChat(approval) === chat) : all;
  return { approvals: mine.map(approvalSummary), pending_in_all_chats: all.length };
}

const decisions = ["allow_once", "allow_always", "deny", "deny_always"];

/** The request that answers an approval, or the reason Hermes may not send it. */
export function approvalDecide(
  response: unknown,
  id: string,
  decision: string,
  leonConfirmed: boolean,
) {
  const approval = pendingApprovals(response).find((item) => item.approval_id === id);
  if (!approval)
    return { error: "No pending approval has that ID. Call muse_approvals for the current ones." };
  if (!decisions.includes(decision))
    return { error: "decision must be one of " + decisions.join(", ") + "." };
  const option = decisionOptions(approval).find((item) => item.kind === decision);
  if (!option)
    return {
      error:
        "Muse does not offer " +
        decision +
        " here. Its options: " +
        decisionOptions(approval)
          .map((item) => item.kind)
          .join(", ") +
        ".",
    };
  const allowing = decision.startsWith("allow");
  const reason = allowing ? approvalNeedsLeon(approval) : undefined;
  const standing = decision.endsWith("_always")
    ? "it changes what Muse may do from now on"
    : undefined;
  if ((reason || standing) && !leonConfirmed)
    return {
      error:
        "Leon decides this one (" +
        (reason ?? standing) +
        "). Tell him exactly what Muse asks, mention him, and send it with leon_confirmed only after he says yes in this conversation.",
    };
  const scope = option.label?.scope;
  return {
    params: {
      approval_id: id,
      decision,
      ...(decision.endsWith("_always") && scope ? { always_scope: scope } : {}),
      ...(decision === "allow_always" && scope ? { allow_always_scope: scope } : {}),
      ...(approval.message_revision === undefined
        ? {}
        : { expected_message_revision: approval.message_revision }),
    },
    summary: approvalSummary(approval),
  };
}

interface Route {
  method: string;
  http: string;
  path: string;
}

// What the Muse app sends for the methods Hermes is most likely to need. Other methods take the
// path's {names} plus the fields the app sends; a wrong shape comes back as Muse's own error text.
const methodNotes: Record<string, string> = {
  "chat.stream":
    'Send a message: {items: [{type: "text", text}], node_id: "hermes", capabilities: {}, session_id?} (session_id = side chat; omit for the main chat). The reply arrives in chat.history.',
  "chat.history":
    "{session_id?, limit}. Rows have event_name message.user / message.assistant and delta.presentation (files, images, links, browser tasks).",
  "chat.cancel": "Stop Muse's current turn: {session_id?}.",
  "session.start":
    '{origin: "fresh", lifecycle: "persistent"}; then session.rename {session_id, title}.',
  "sessions.list": "Every chat with its ID, title and state.",
  "egress.approvals": "Use muse_approvals and muse_approve instead.",
  "egress.approval.history": "{limit, cursor?}: approvals already decided.",
  "egress.permissions": "What Muse may do without asking.",
  "tasks.list":
    "Scheduled tasks. tasks.runs {limit, job_id?}; tasks.run / tasks.pause / tasks.resume / tasks.delete {job_id}.",
  "goals.list": "goals.get {id}, goals.update {id, …}, goals.delete {id}.",
  "api.idea-cards.list":
    'Ideas. api.idea-cards.execute {ideaCardId, mode: "full"} has Muse act on one.',
  "feed.list": '{limit}. feed.unit.reaction {unit_id, reaction: "like" | "love" | "dislike"}.',
  "fs.list":
    "Files on the Muse computer: {path}. fs.read {path}, fs.write {path, content}, fs.search {query}. Use muse_file to download one.",
  "connectors.list": "Connected apps. connectors.get {id}, connectors.disconnect {id}.",
  "credentials.list": "Saved logins in Muse's secure store (names only, never passwords).",
  "memory.search": "{query}: what Muse remembers.",
  "skills.list": "Muse's skills. skills.set_disabled {…}.",
  "email.threads.list":
    "Muse Mail: {cursor?}. email.threads.get {thread_id}, email.messages.get {message_id}.",
  "widgets.state.set": "Answer a quick-reply widget in a message: {widget_id, …}.",
  "browser_task.get": "{task_id}: a browser task's state. browser_task.stop {task_id} ends it.",
  "config.get": "Muse's settings. config.update takes the changed fields.",
  "model.get": "The model Muse uses. model.set changes it.",
  identity: "Who Muse is and whose it is.",
};

// Actions that erase data, open the account to others, or change what Muse may do unasked.
const leonOnly =
  /^(bootstrap\.reset|onboarding\.reset|voice\.onboarding\.reset|api\.ideas\.reset|dyi\.export|rv_migration\.|ssh\.|redteam\.|node\.unpair|browser\.cookies\.clear|email\.mailbox\.(delete|create)|email\.(owners\.revoke|owner_challenges\.)|wallet\.set|subscription\.|appeals\.submit|credentials\.(register|capture|delete|catalog\.(update|delete))|custom_connectors\.(register|delete)|(egress\.)?permissions\.(?!active$|inventory$|settings$|scoped$)|egress\.approval\.(decide|edit_message)|peers\.(invites\.(create|accept)|requests\.(confirm|sas\.confirm)|connections\.connect|disconnect)|shared_agents\.(delete|shares\.create)|spaces\.(share$|audience\.set)|peerSpaces\.share$)/;

export function methodGuard(method: string, leonConfirmed: boolean) {
  if (method === "egress.approval.decide")
    return "Answer approvals with muse_approve, which checks the request first.";
  if (leonOnly.test(method) && !leonConfirmed)
    return (
      method +
      " erases data, shares Leon's account or changes what Muse may do on its own. Ask Leon first, mentioning him, and pass leon_confirmed only after he says yes in this conversation."
    );
}

export function methodSearch(routes: Route[], query: string) {
  const words = query
    .toLowerCase()
    .split(/[^a-z0-9_]+/)
    .filter(Boolean);
  const scored = routes
    .map((route) => {
      const text = (
        route.method +
        " " +
        route.path +
        " " +
        (methodNotes[route.method] ?? "")
      ).toLowerCase();
      const score =
        words.filter((word) => text.includes(word)).length + (methodNotes[route.method] ? 0.5 : 0);
      return { route, score };
    })
    .filter((item) => !words.length || item.score >= 1)
    .sort(
      (left, right) =>
        right.score - left.score || left.route.method.localeCompare(right.route.method),
    );
  return {
    total: routes.length,
    methods: scored.slice(0, 40).map(({ route }) => ({
      method: route.method,
      call: route.http + " " + route.path,
      ...(methodNotes[route.method] ? { note: methodNotes[route.method] } : {}),
      ...(leonOnly.test(route.method) ? { leon_confirms: true } : {}),
    })),
  };
}

/** A tool result small enough for Hermes to read: long text and long lists are cut and say so. */
export function clipAnswer(value: unknown, limit: number): unknown {
  const text = JSON.stringify(value) ?? "null";
  if (text.length <= limit) return value;
  if (Array.isArray(value)) {
    const kept: unknown[] = [];
    let size = 2;
    for (const item of value) {
      size += (JSON.stringify(item) ?? "").length + 1;
      if (size > limit) break;
      kept.push(item);
    }
    if (kept.length) return { truncated: true, shown: kept.length, of: value.length, items: kept };
  }
  return { truncated: true, bytes: text.length, start: text.slice(0, limit) };
}

export function loginEndedNotice(owner: string) {
  return (
    "<@" +
    owner +
    "> muse.ai signed me out, so I can no longer answer Muse's approvals or use the Muse app for you. " +
    'Handoffs to Muse still work. Sign me in again when you can: the steps are in the hermes22muse README under "Sign Hermes in to muse.ai".'
  );
}
