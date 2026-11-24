import { agentGroup, clientMessageId } from "./slack.ts";

interface SlackEvent {
  client_msg_id?: unknown;
  message?: { client_msg_id?: unknown };
  _hermes_reaction?: boolean;
  _hermes_force_process?: boolean;
}

export function shouldTrack(
  event: SlackEvent,
  ts: string | undefined,
  accepted: boolean,
  enabled: boolean,
) {
  return Boolean(
    accepted &&
    ts &&
    enabled &&
    !(event._hermes_reaction || event._hermes_force_process || agentGroup(clientMessageId(event))),
  );
}
