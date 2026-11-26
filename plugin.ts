import { fileURLToPath } from "node:url";

/** Hermes plugin name, as listed in `plugins.enabled`. */
export const pluginName = "leon";

/** The plugin's mirror root, installed at `$HERMES_HOME/plugins/leon/`. */
export const pluginDirectory = fileURLToPath(new URL(".", import.meta.url));

/** `.env` setting: features to skip, by name or folder, separated by commas. */
export const disabledEnv = "LEON_PLUGIN_DISABLED";
