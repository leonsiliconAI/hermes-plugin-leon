Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
// slack_flat_replies/policy.ts
/**
* A top-level channel message from a person, or from Autolinear's bot (`autolinearBot`), whose
* reminders and handoffs are about the channel's own conversation. The adapter owns bot
* classification, including its profile-scoped allowlist.
*/
function shouldReplyFlat(event, ts, isDm, isBot, autolinearBot = "") {
	if (isDm || !event.user || event._hermes_reaction || event._hermes_force_process) return false;
	if (event.thread_ts && event.thread_ts !== ts) return false;
	return autolinearBot !== "" && event.user === autolinearBot || !isBot();
}
exports.shouldReplyFlat = shouldReplyFlat;
