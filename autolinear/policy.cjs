Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
const groups = {
	grok: "b075",
	codex: "c0de"
};
function agentGroup(id) {
	if (typeof id === "string") return Object.values(groups).find((group) => id.startsWith("de1e6a7e-" + group + "-"));
}
function issueChannel(name, prefix) {
	if (!name.startsWith(prefix + "-")) return false;
	return /^\p{Decimal_Number}+(?:-|$)/u.test(name.slice(prefix.length + 1));
}
// autolinear/policy.ts
/**
* Where Leon's messages become new issues: his DM with Hermes and project channels (`project-…`).
* Anywhere else, task channels included, Hermes answers in place. A channel whose name couldn't be
* read (Hermes then has only its ID) never qualifies.
*/
const projectPrefix = "project-";
function intakeChannel(channel, name) {
	if (channel.startsWith("D")) return true;
	return Boolean(name) && name !== channel && name.startsWith("project-");
}
function ordinary(event) {
	return !(event._hermes_reaction || event._hermes_force_process || event.bot_id || typeof event.client_msg_id === "string" && event.client_msg_id.startsWith("de1e6a7e-"));
}
function answersForSure(event, user, bot) {
	if (event._slack_changed_event_ts || event.user !== user || !ordinary(event)) return false;
	const text = String(event.text || "");
	const command = bot ? text.replaceAll("<@" + bot + ">", "") : text;
	return !/^[!/]/.test(command.trimStart()) && [...text.matchAll(/<@([UW][A-Z0-9]+)/g)].every((match) => match[1] === bot);
}
function where(event, channel, name) {
	const kind = event.channel_type || (channel.startsWith("D") ? "im" : "");
	let place = "a channel";
	if (kind === "im") place = "a DM with Hermes";
	else if (kind === "mpim") place = "a group DM";
	else if (name && name !== channel) place = "#" + name;
	return event.thread_ts && event.thread_ts !== event.ts ? "a thread in " + place : place;
}
function intakeResponse(attempt, status, accepted) {
	if ((status === 0 || status >= 500) && attempt === 1) return "retry";
	if (status !== 200) return "fail";
	return accepted ? "accept" : "decline";
}
function issueNote(result) {
	return "[autolinear: Leon sent this in " + (result.source || "Slack") + ". It is now " + result.issue.identifier + " (" + result.issue.url + "), and this is its channel, #" + result.channel.name + ": answer here and keep the conversation here.]";
}
function copiedMessage(event, result, from, ts, omit) {
	return {
		...Object.fromEntries(Object.entries(event).filter(([key]) => !omit.includes(key))),
		channel: result.channel.id,
		ts: result.ts,
		event_ts: result.ts,
		channel_type: "group",
		_autolinear: {
			from_channel: from,
			from_ts: ts,
			issue: result.issue.identifier
		}
	};
}
function taskPrompt(inIssue, report, bot) {
	if (!inIssue) return "";
	const mention = bot ? "<@" + bot + ">" : "@Autolinear";
	const instructions = report ? "This is a report from an agent already assigned to the task. Follow up if they need coordination, and update Leon when there is something he needs to know. If there is no action or meaningful update, return NO_REPLY. Send one Autolinear command only if this report calls for a different issue status. If the status should stay the same, omit the command entirely, even if earlier turns used `stay`. Do not post `stay` for a delegated agent's report. " : "This is a Linear issue's channel. End the turn with one line of its own that tells Autolinear what to do with the channel. ";
	const stay = report ? "" : ", or `stay` (keep the status; after your 👀 moved the issue to Executing, that means the status it had before, and it's refused when there was none)";
	const dispatch = report ? "" : "Choose and coordinate agents as the work requires. Use `move executing --until <duration>` while delegated work is active. Executing is a lease: once its `--until` has passed and nothing has happened in the channel for 15 minutes (no turn of yours running there, no posts), the issue goes to Leon. When results come back, verify them and set the status based on what remains. Keep device-only verification pending until it runs. ";
	const reminder = report ? "" : " When you forget, Autolinear reminds you here. Answer it with the line.";
	return "Keep this Linear issue's title and Slack channel name. Create a separate Linear issue for new work, link it to this issue, and let the sync open its own Slack channel. " + instructions + "Start that line with " + mention + ", written exactly so, then one of: `move done` (only once Leon has said it's done or to close it; finished work, your answer, his silence or his thanks never count, so finished work gets `move leon`), `move leon` (Leon needs to see, decide or do something; mention him in your message), `move trigger --who \"<who>\"` (waiting on someone else), `move trigger --event \"<what>\" --stop YYYY-MM-DD` (waiting for an event until a stop date), `move date --start <RFC 3339 time or YYYY-MM-DD>` (work starts later), `move executing --until <45m, 2h or an RFC 3339 time, at most 24h>` (a delegated agent is still working on it; without `--until` it holds for an hour)" + stay + ". " + dispatch + "Set the issue status through Autolinear. Mentioning Leon only notifies him." + reminder + " When Autolinear says a command didn't work, send a corrected line.";
}
function applyPrompt(current, inIssue, event, bot) {
	const report = Boolean(agentGroup(event?.client_msg_id));
	return {
		prompt: [current, taskPrompt(inIssue, report, bot)].filter(Boolean).join("\n\n"),
		silent: inIssue && report
	};
}
async function requestIntake(host) {
	for (const attempt of [1, 2]) {
		const { status, body } = await host.post();
		const action = intakeResponse(attempt, status, Boolean(body?.ok));
		if (action === "retry") await host.retry();
		else if (status === 0) return { networkError: true };
		else if (action === "fail") return { error: "autolinear answered " + status + ": " + (body?.error ?? "None") };
		else if (action === "accept") return { accepted: body };
		else return { skipped: body?.skipped };
	}
	return {};
}
function files(event) {
	if (!Array.isArray(event.files)) return [];
	return event.files.filter((file) => Boolean(file) && typeof file === "object" && !Array.isArray(file));
}
function intakePayload(event, channel, ts, name) {
	const attachments = files(event).map((file) => ({
		name: file.name || "",
		title: file.title || "",
		permalink: file.permalink || ""
	}));
	return {
		channel,
		ts,
		thread_ts: event.thread_ts && event.thread_ts !== ts ? event.thread_ts : null,
		user: String(event.user || ""),
		text: String(event.text || ""),
		where: where(event, channel, name),
		...Array.isArray(event.blocks) ? { blocks: event.blocks } : {},
		...attachments.length ? { files: attachments } : {}
	};
}
function canMove(event, sender, owner, command) {
	return Boolean(owner && sender === owner && !command && event && ordinary(event));
}
function shouldReact(event, ts) {
	return Boolean(ts && !event?._slack_changed_event_ts);
}
async function buildReply(host, event, sender, owner, command) {
	if (!canMove(event, sender, owner, command)) await host.build("local");
	else if (!await host.move() || !await host.build("moved")) await host.build("local");
}
exports.answersForSure = answersForSure;
exports.applyPrompt = applyPrompt;
exports.buildReply = buildReply;
exports.canMove = canMove;
exports.copiedMessage = copiedMessage;
exports.intakeChannel = intakeChannel;
exports.intakePayload = intakePayload;
exports.intakeResponse = intakeResponse;
exports.issueChannel = issueChannel;
exports.issueNote = issueNote;
exports.ordinary = ordinary;
exports.projectPrefix = projectPrefix;
exports.requestIntake = requestIntake;
exports.shouldReact = shouldReact;
exports.taskPrompt = taskPrompt;
exports.where = where;
