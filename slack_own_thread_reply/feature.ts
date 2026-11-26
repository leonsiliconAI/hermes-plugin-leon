/**
 * The feature's name: the separate Hermes plugin it was until 2026-11-26, and its entry in
 * `LEON_PLUGIN_DISABLED`. Its folder is the name with `_` for `-`.
 */
export const featureName = "slack-own-thread-reply";

/** Tool the plugin registers, and the toolset it belongs to. */
export const toolName = "slack_reply_own_thread";
export const toolsetName = "slack_own_threads";

/** Optional `.env` settings: hourly reply cap per thread (default 4), and a channel allowlist. */
export const maxPerHourEnv = "SLACK_OWN_THREAD_MAX_PER_HOUR";
export const channelsEnv = "SLACK_OWN_THREAD_CHANNELS";
