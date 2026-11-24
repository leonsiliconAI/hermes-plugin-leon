interface SlackEvent {
  client_msg_id?: unknown;
  message?: { client_msg_id?: unknown };
  _hermes_reaction?: boolean;
  _hermes_force_process?: boolean;
}

export const agentPosts = ["de1e6a7e-b075-", "de1e6a7e-c0de-"];

export function isRealMessage(event: SlackEvent): boolean {
  const id = event.client_msg_id || event.message?.client_msg_id;
  const agentPost = typeof id === "string" && agentPosts.some((prefix) => id.startsWith(prefix));
  return !(event._hermes_reaction || event._hermes_force_process || agentPost);
}
