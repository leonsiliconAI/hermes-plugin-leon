// Pure logic for grok-box-browser, run from Python through the plugins' bridge (see __init__.py).

const ISSUE_CHANNEL = /^leon-(\d+)(?:-|$)/;

/**
 * The browser a Hermes turn gets: one per Linear issue, by its AutoLinear channel (`leon-532-…` →
 * `leon-532`), else one per Slack conversation, else one per Hermes task.
 */
export function taskKey(platform: string, chatName: string, chatId: string, taskId: string) {
  const issue = ISSUE_CHANNEL.exec(chatName.trim().toLowerCase());
  if (issue) return `leon-${issue[1]}`;
  const chat = chatId
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
  if (platform && chat)
    return `${platform.toLowerCase().replace(/[^a-z0-9]/g, "")}-${chat}`.slice(0, 64);
  const task = taskId
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 56);
  return task ? `hermes-${task}` : "hermes";
}

/** `leon-532` → `LEON-532`, the Linear issue a task browser belongs to. */
export function issueIdentifier(task: string) {
  const match = /^leon-(\d+)$/.exec(task);
  return match ? `LEON-${match[1]}` : null;
}

/** A browser's profile is kept for a week once its issue is done or canceled. */
export function shouldRetire(stateType: string) {
  return stateType === "completed" || stateType === "canceled";
}

/** Session metadata in the shape Hermes's browser tools cache (`agent/browser_provider.py`). */
export function sessionInfo(task: string, localPort: number, ws: string, seeded: number) {
  return {
    session_name: `grok-box-${task}`,
    bb_session_id: task,
    cdp_url: `ws://127.0.0.1:${localPort}${ws}`,
    features: { grok_box: true, task, seeded_cookies: seeded },
  };
}

interface RepairStatus {
  repairAt?: number | null;
  failures?: number;
  result?: string | null;
  detail?: string | null;
}

/**
 * What Hermes is told when the box can't be reached. The plugin has asked slack22grokbot for a
 * repair, which a Grok bot runs in the box; `status` is the relay's last word on repairs.
 */
export function unreachableMessage(detail: string, status: RepairStatus | null, nowMs: number) {
  const lead = `The Grok box browser is unreachable (${detail}). The Grok Bot cloud computer may have restarted.`;
  if ((status?.failures ?? 0) >= 2)
    return (
      `${lead} Automatic repairs through Grok Bot have failed ${status?.failures} times in a row, so tell ` +
      'Leon the Grok box browser needs him (grok-box-browser README, "After the box restarts"). ' +
      "Use another way to do the browser work for now."
    );
  const minutes = status?.repairAt ? Math.round((nowMs - status.repairAt) / 60_000) : null;
  const sent =
    status?.result === "sent" && minutes !== null && minutes < 15
      ? ` A Grok bot was asked to restart it ${minutes} minute(s) ago.`
      : status?.result === "skipped" && status.detail
        ? ` The last repair couldn't be sent (${status.detail}); it is retried every few minutes.`
        : " A repair has been requested; the next message to any Grok bot, or the relay within a minute, sends it.";
  return `${lead}${sent} Try the browser again in about five minutes, and do other work meanwhile.`;
}
