/**
 * The feature's name: the separate Hermes plugin it was until 2026-11-26, and its entry in
 * `LEON_PLUGIN_DISABLED`. Its folder is the name with `_` for `-`.
 */
export const featureName = "slack-auto-join";

/** Bot events the Hermes Slack app must subscribe to for instant joins. */
export const manifestBotEvents = ["channel_created", "channel_unarchive"] as const;

/** User-token scopes for the optional private-channel inviter. */
export const userScopes = ["groups:read", "groups:write.invites"] as const;

/** `.env` variable holding that user token. */
export const userTokenEnv = "SLACK_AUTO_JOIN_USER_TOKEN";
