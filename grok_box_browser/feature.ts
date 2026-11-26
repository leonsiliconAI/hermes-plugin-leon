/**
 * The feature's name: the separate Hermes plugin it was until 2026-11-26, and its entry in
 * `LEON_PLUGIN_DISABLED`. Its folder is the name with `_` for `-`.
 */
export const featureName = "grok-box-browser";

/** The `browser.cloud_provider` value that selects this plugin's browsers. */
export const providerName = "grok-box";

/** Files the Grok box fetches from the leon mirror (under `grok_box_browser/`), pinned to a commit. */
export const boxFiles = ["box/up.sh", "box/broker.mjs"] as const;
