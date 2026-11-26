import { fileURLToPath } from "node:url";

/** Hermes plugin name, as listed in `plugins.enabled`. */
export const pluginName = "slack-always-react";

/** Directory to copy (without `__tests__/`) to `$HERMES_HOME/plugins/slack-always-react/`. */
export const pluginDirectory = fileURLToPath(new URL(".", import.meta.url));

/** Files that make up the deployed plugin. */
export const pluginFiles = [
  "plugin.yaml",
  "__init__.py",
  "pyproject.toml",
  "_bridge.py",
  "bridge.cjs",
  "policy.cjs",
] as const;
