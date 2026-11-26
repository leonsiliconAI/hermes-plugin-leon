/**
 * The feature's name: the separate Hermes plugin it was until 2026-11-26, and its entry in
 * `LEON_PLUGIN_DISABLED`. Its folder is the name with `_` for `-`.
 */
export const featureName = "autolinear";

/**
 * `.env` settings. The token (autolinear's `AUTOLINEAR_TOKEN`) and Leon's Slack user ID turn it
 * on; the rest are optional.
 */
export const tokenEnv = "AUTOLINEAR_TOKEN";
export const userEnv = "AUTOLINEAR_USER_ID";
export const urlEnv = "AUTOLINEAR_URL";
export const channelPrefixEnv = "AUTOLINEAR_CHANNEL_PREFIX";
export const skipChannelsEnv = "AUTOLINEAR_SKIP_CHANNELS";

/** autolinear's intake endpoint, unless `AUTOLINEAR_URL` says otherwise. */
export const defaultUrl = "https://autolinear.leonsilicon.ai/intake";
