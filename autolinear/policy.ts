import { agentGroup, issueChannel, marker } from "#slack";

export { issueChannel };

type Message = Record<string, unknown> & {
  client_msg_id?: string;
  channel_type?: string;
  ts?: string;
  thread_ts?: string;
  user?: string;
  text?: string;
};

interface Result {
  source?: string;
  issue: { identifier: string; url: string };
  channel: { id: string; name: string };
  ts: string;
}

const issueScope =
  "Keep this Linear issue's title and Slack channel name. Create a separate Linear issue " +
  "for new work, link it to this issue, and let the sync open its own Slack channel.";

/**
 * Where Leon's messages become new issues: his DM with Hermes and project channels (`project-…`).
 * Anywhere else, task channels included, Hermes answers in place. A channel whose name couldn't be
 * read (Hermes then has only its ID) never qualifies.
 */
export const projectPrefix = "project-";

export function intakeChannel(channel: string, name: string) {
  if (channel.startsWith("D")) return true;
  return Boolean(name) && name !== channel && name.startsWith(projectPrefix);
}

export function ordinary(event: Message) {
  return !(
    event._hermes_reaction ||
    event._hermes_force_process ||
    event.bot_id ||
    (typeof event.client_msg_id === "string" && event.client_msg_id.startsWith(marker + "-"))
  );
}

export function answersForSure(event: Message, user: string, bot?: string) {
  if (event._slack_changed_event_ts || event.user !== user || !ordinary(event)) return false;
  const text = String(event.text || "");
  const command = bot ? text.replaceAll("<@" + bot + ">", "") : text;
  return (
    !/^[!/]/.test(command.trimStart()) &&
    [...text.matchAll(/<@([UW][A-Z0-9]+)/g)].every((match) => match[1] === bot)
  );
}

export function where(event: Message, channel: string, name: string) {
  const kind = event.channel_type || (channel.startsWith("D") ? "im" : "");
  let place = "a channel";
  if (kind === "im") place = "a DM with Hermes";
  else if (kind === "mpim") place = "a group DM";
  else if (name && name !== channel) place = "#" + name;
  return event.thread_ts && event.thread_ts !== event.ts ? "a thread in " + place : place;
}

export function intakeResponse(attempt: number, status: number, accepted: boolean) {
  if ((status === 0 || status >= 500) && attempt === 1) return "retry";
  if (status !== 200) return "fail";
  return accepted ? "accept" : "decline";
}

export function issueNote(result: Result) {
  return (
    "[autolinear: Leon sent this in " +
    (result.source || "Slack") +
    ". It is now " +
    result.issue.identifier +
    " (" +
    result.issue.url +
    "), and this is its channel, #" +
    result.channel.name +
    ": answer here and keep the conversation here.]"
  );
}

export function copiedMessage(
  event: Message,
  result: Result,
  from: string,
  ts: string,
  omit: string[],
) {
  return {
    ...Object.fromEntries(Object.entries(event).filter(([key]) => !omit.includes(key))),
    channel: result.channel.id,
    ts: result.ts,
    event_ts: result.ts,
    channel_type: "group",
    _autolinear: { from_channel: from, from_ts: ts, issue: result.issue.identifier },
  };
}

export function taskPrompt(inIssue: boolean, report: boolean, bot: string) {
  if (!inIssue) return "";
  const mention = bot ? "<@" + bot + ">" : "@Autolinear";
  const instructions = report
    ? "This is a report from an agent already assigned to the task. Follow up if they " +
      "need coordination, and update Leon when there is something he needs to know. " +
      "If there is no action or meaningful update, return NO_REPLY. " +
      "Send one Autolinear command only if this report calls for a different issue status. " +
      "If the status should stay the same, omit the command entirely, even if earlier " +
      "turns used `stay`. Do not post `stay` for a delegated agent's report. "
    : "This is a Linear issue's channel. End the turn with one line of its own that tells " +
      "Autolinear what to do with the channel. ";
  const stay = report
    ? ""
    : ", or `stay` (keep the status; after your 👀 moved the issue to Executing, that means the " +
      "status it had before, and it's refused when there was none)";
  const dispatch = report
    ? ""
    : "Choose and coordinate agents as the work requires. Use `move executing --until <duration>` while " +
      "delegated work is active. Executing is a lease: once its `--until` has passed and nothing has " +
      "happened in the channel for 15 minutes (no turn of yours running there, no posts), the issue " +
      "goes to Leon. " +
      "When results come back, verify them and set the status based on what remains. " +
      "Keep device-only verification pending until it runs. ";
  const reminder = report
    ? ""
    : " When you forget, Autolinear reminds you here. Answer it with the line.";
  return (
    issueScope +
    " " +
    instructions +
    "Start that line with " +
    mention +
    ", written exactly so, then one of: " +
    "`move done` (nothing is left for anyone, even after a thanks), " +
    "`move leon` (Leon needs to see, decide or do something; mention him in your message), " +
    '`move trigger --who "<who>"` (waiting on someone else), ' +
    '`move trigger --event "<what>" --stop YYYY-MM-DD` (waiting for an event until a stop date), ' +
    "`move date --start <RFC 3339 time or YYYY-MM-DD>` (work starts later), " +
    "`move executing --until <45m, 2h or an RFC 3339 time, at most 24h>` (a delegated agent is " +
    "still working on it; without `--until` it holds for an hour)" +
    stay +
    ". " +
    dispatch +
    "Set the issue status through Autolinear. Mentioning Leon only notifies him." +
    reminder +
    " When Autolinear says a command didn't work, send a corrected line."
  );
}

export function applyPrompt(
  current: string | undefined,
  inIssue: boolean,
  event: Message | undefined,
  bot: string,
) {
  const report = Boolean(agentGroup(event?.client_msg_id));
  return {
    prompt: [current, taskPrompt(inIssue, report, bot)].filter(Boolean).join("\n\n"),
    silent: inIssue && report,
  };
}

interface IntakeHost {
  post(): Promise<{
    status: number;
    body?: Result & { ok?: boolean; error?: string; skipped?: string };
  }>;
  retry(): Promise<void>;
}

export async function requestIntake(host: IntakeHost) {
  for (const attempt of [1, 2]) {
    const { status, body } = await host.post();
    const action = intakeResponse(attempt, status, Boolean(body?.ok));
    if (action === "retry") {
      await host.retry();
    } else if (status === 0) {
      return { networkError: true };
    } else if (action === "fail") {
      return { error: "autolinear answered " + status + ": " + (body?.error ?? "None") };
    } else if (action === "accept") {
      return { accepted: body };
    } else {
      return { skipped: body?.skipped };
    }
  }
  return {};
}

interface Attachment {
  name?: string;
  title?: string;
  permalink?: string;
}

function files(event: Message): Attachment[] {
  if (!Array.isArray(event.files)) return [];
  return event.files.filter(
    (file): file is Attachment => Boolean(file) && typeof file === "object" && !Array.isArray(file),
  );
}

export function intakePayload(event: Message, channel: string, ts: string, name: string) {
  const attachments = files(event).map((file) => ({
    name: file.name || "",
    title: file.title || "",
    permalink: file.permalink || "",
  }));
  return {
    channel,
    ts,
    thread_ts: event.thread_ts && event.thread_ts !== ts ? event.thread_ts : null,
    user: String(event.user || ""),
    text: String(event.text || ""),
    where: where(event, channel, name),
    ...(Array.isArray(event.blocks) ? { blocks: event.blocks } : {}),
    ...(attachments.length ? { files: attachments } : {}),
  };
}

export function canMove(
  event: Message | undefined,
  sender: string,
  owner: string | undefined,
  command: boolean,
) {
  return Boolean(owner && sender === owner && !command && event && ordinary(event));
}

export function shouldReact(event: Message, ts: string) {
  return Boolean(ts && !event?._slack_changed_event_ts);
}

interface BuildHost {
  build(mode: "local" | "moved"): Promise<boolean>;
  move(): Promise<boolean>;
}

export async function buildReply(
  host: BuildHost,
  event: Message | undefined,
  sender: string,
  owner: string | undefined,
  command: boolean,
) {
  if (!canMove(event, sender, owner, command)) {
    await host.build("local");
  } else if (!(await host.move()) || !(await host.build("moved"))) {
    await host.build("local");
  }
}
