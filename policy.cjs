Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
// \0hermes-slack.ts
const groups = {
	grok: "b075",
	codex: "c0de"
};
function clientMessageId(event) {
	const value = event?.client_msg_id || event?.message?.client_msg_id;
	return typeof value === "string" ? value : "";
}
function agentGroup(id) {
	if (typeof id === "string") return Object.values(groups).find((group) => id.startsWith("de1e6a7e-" + group + "-"));
}
// policy.ts
function shouldTrack(event, ts, accepted, enabled) {
	return Boolean(accepted && ts && enabled && !(event._hermes_reaction || event._hermes_force_process || agentGroup(clientMessageId(event))));
}
exports.shouldTrack = shouldTrack;
