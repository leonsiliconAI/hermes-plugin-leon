/**
 * The feature's name: the separate Hermes plugin it was until 2026-11-26, and its entry in
 * `LEON_PLUGIN_DISABLED`. Its folder is the name with `_` for `-`.
 */
export const featureName = "dropbox-task-memory";

/** Tools the plugin registers, and the toolset they belong to. */
export const toolNames = [
  "dropbox_task_memory_read",
  "dropbox_task_memory_list",
  "dropbox_task_memory_write",
  "dropbox_task_memory_move",
] as const;
export const toolsetName = "dropbox_task_memory";

/** The only folder the write tools touch. Moves may also take a file from elsewhere under `/Agents/`. */
export const memoryRoot = "/Agents/Memory";

/** Required `.env` settings: a Dropbox refresh token for this plugin's own grant, and the app key it was issued to. */
export const refreshTokenEnv = "DROPBOX_TASK_MEMORY_REFRESH_TOKEN";
export const appKeyEnv = "DROPBOX_TASK_MEMORY_APP_KEY";
