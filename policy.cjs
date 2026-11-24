Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
// policy.ts
const agentPosts = ["de1e6a7e-b075-", "de1e6a7e-c0de-"];
function isRealMessage(event) {
	const id = event.client_msg_id || event.message?.client_msg_id;
	const agentPost = typeof id === "string" && agentPosts.some((prefix) => id.startsWith(prefix));
	return !(event._hermes_reaction || event._hermes_force_process || agentPost);
}
exports.agentPosts = agentPosts;
exports.isRealMessage = isRealMessage;
