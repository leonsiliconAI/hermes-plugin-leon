interface SlackEvent {
  user?: string;
  thread_ts?: string;
  _hermes_reaction?: boolean;
  _hermes_force_process?: boolean;
}

/**
 * A top-level channel message from a person, or from Autolinear's bot (`autolinearBot`), whose
 * reminders and handoffs are about the channel's own conversation. The adapter owns bot
 * classification, including its profile-scoped allowlist.
 */
export function shouldReplyFlat(
  event: SlackEvent,
  ts: string,
  isDm: boolean,
  isBot: () => boolean,
  autolinearBot = "",
): boolean {
  if (isDm || !event.user || event._hermes_reaction || event._hermes_force_process) return false;
  if (event.thread_ts && event.thread_ts !== ts) return false;
  return (autolinearBot !== "" && event.user === autolinearBot) || !isBot();
}
