Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
// dropbox_task_memory/policy.ts
const root = "/Agents/Memory";
const versions = "Earlier versions stay in Dropbox's version history: ";
function notePath(slug) {
	slug = slug.trim().toLowerCase().replace(/\.md$/, "");
	if (slug.length > 150 || !/^[a-z][a-z0-9]{0,9}-\d{1,6}(?:-[a-z0-9]+)*$/.test(slug) || /\s/.test(slug)) return { error: "slug must be a Linear issue slug like leon-330-prepare-separate-st-george-chapter-sop-application (lowercase letters, digits and hyphens; no folders)" };
	return { path: root + "/" + slug + ".md" };
}
function sourcePath(path) {
	path = path.trim();
	const parts = path.split("/");
	if (parts[0] !== "" || parts.length < 3 || parts[1].toLowerCase() !== "agents" || path.includes("\\") || parts.slice(1).some((part) => part === "" || part === "." || part === "..")) return { error: "source_path must be a file path inside /Agents/, e.g. /Agents/Branches/leon-330-x/memory.md" };
	return { path };
}
function error(message) {
	return {
		success: false,
		error: message
	};
}
function summary(meta) {
	return Object.fromEntries([
		"path_display",
		"rev",
		"size",
		"server_modified"
	].filter((key) => meta[key] != null).map((key) => [key, meta[key]]));
}
async function read(host, slug) {
	const { path, error: invalid } = notePath(slug);
	if (path === void 0) return error(invalid);
	const found = await host.download(path);
	if (found.error !== void 0) return error(found.error);
	if (!found.value) return {
		success: true,
		exists: false,
		path
	};
	const [meta, content] = found.value;
	return {
		success: true,
		exists: true,
		content,
		...summary(meta)
	};
}
async function list(host) {
	const notes = [];
	let cursor;
	do {
		const page = await host.list(cursor);
		if (!page.value) {
			if (page.status === 409 && page.summary?.includes("not_found")) return {
				success: true,
				notes: [],
				note: root + " does not exist yet; the first write creates it"
			};
			return error(page.error ?? "Dropbox returned no files");
		}
		for (const entry of page.value.entries ?? []) {
			if (entry[".tag"] !== "file") continue;
			const { name = null, path_display = null, server_modified = null, size = null } = entry;
			notes.push({
				name,
				path_display,
				server_modified,
				size
			});
		}
		cursor = page.value.has_more ? page.value.cursor : void 0;
	} while (cursor);
	notes.sort((a, b) => {
		const first = String(a.name ?? "None").toLowerCase();
		const second = String(b.name ?? "None").toLowerCase();
		return first < second ? -1 : Number(first > second);
	});
	return {
		success: true,
		notes
	};
}
async function write(host, slug, mode, content, rev, maxBytes, versionsHelp) {
	if (typeof content !== "string" || !content.trim()) return error("content is required");
	const { path, error: invalid } = notePath(slug);
	if (path === void 0) return error(invalid);
	let text = content;
	let uploadMode = "add";
	if (mode === "append") {
		const found = await host.download(path);
		if (found.error !== void 0) return error(found.error);
		if (found.value) {
			const [meta, old] = found.value;
			text = (old && !old.endsWith("\n") ? old + "\n" : old) + text;
			if (host.byteLength(text) > maxBytes) return error("the note would grow past " + maxBytes / 1024 + " KB; condense it with mode=replace");
			uploadMode = {
				".tag": "update",
				update: meta.rev
			};
		}
	} else if (mode === "replace") {
		if (!rev) return error("mode=replace needs the rev from dropbox_task_memory_read; read the note first");
		uploadMode = {
			".tag": "update",
			update: rev
		};
	} else if (mode !== "create") return error("mode must be create, append or replace");
	const result = await host.upload(path, text, uploadMode);
	if (!result.value) {
		if (result.summary?.includes("conflict")) {
			const hint = mode === "create" ? "a note already exists there; read it and use mode=append or mode=replace" : "the note changed since it was read; read it again and retry";
			return error(result.summary + ": " + hint);
		}
		return error(result.error ?? "Dropbox returned no metadata");
	}
	host.logWrite(mode, result.value);
	return {
		success: true,
		action: mode,
		revert: versions + versionsHelp,
		...summary(result.value)
	};
}
async function move(host, from, slug) {
	const source = sourcePath(from);
	if (source.path === void 0) return error(source.error);
	const destination = notePath(slug);
	if (destination.path === void 0) return error(destination.error);
	const meta = await host.metadata(source.path);
	if (meta.error !== void 0) return error(meta.error);
	if (!meta.value) return error(source.path + " does not exist");
	if (meta.value[".tag"] !== "file") return error(source.path + " is a " + (meta.value[".tag"] ?? "non-file") + "; only files can be moved into /Agents/Memory");
	const result = await host.move(source.path, destination.path);
	if (!result.value) return error(result.error ?? "Dropbox returned no metadata");
	const moved = result.value.metadata ?? {};
	host.logMove(meta.value, moved);
	return {
		success: true,
		action: "move",
		moved_from: meta.value.path_display || source.path,
		revert: "Move it back with the Dropbox MCP move tool (that one asks Leon first).",
		...summary(moved)
	};
}
exports.list = list;
exports.move = move;
exports.notePath = notePath;
exports.read = read;
exports.sourcePath = sourcePath;
exports.write = write;
