import { groups, marker as postMarker } from "#slack";

/**
 * The feature's name: the separate Hermes plugin it was until 2026-11-26, and its entry in
 * `LEON_PLUGIN_DISABLED`. Its folder is the name with `_` for `-`.
 */
export const featureName = "slack-as-leon-guard";

/** First group of the client_msg_id slack-as-leon stamps on its posts. */
export const marker = postMarker;

/** Second group on a Grok bot's posts through slack-as-leon. */
export const grokBotGroup = groups.grok;

/** Second group on a Codex session's posts through slack-as-leon. */
export const codexGroup = groups.codex;
