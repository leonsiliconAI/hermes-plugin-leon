import { globSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite-plus";

// The bridge and the worker it starts, which each plugin ships at its root.
const shipped = ["_bridge.py", "bridge.cjs"].map((name) => ({
  name,
  path: fileURLToPath(new URL("./" + name, import.meta.url)),
}));
const slack = fileURLToPath(new URL("./slack.ts", import.meta.url));
const slackModule = "\0hermes-slack.ts";

function pluginFiles(shipBridge: boolean) {
  return {
    name: "hermes-plugin-files",
    buildStart(this: {
      addWatchFile(path: string): void;
      emitFile(file: { type: "asset"; fileName: string; source: Buffer }): void;
    }) {
      if (!shipBridge) return;
      for (const { name, path } of shipped) {
        if (path !== resolve(name)) this.addWatchFile(path);
        this.emitFile({ type: "asset", fileName: name, source: readFileSync(path) });
      }
    },
    resolveId(source: string) {
      if (source === "#slack") return slackModule;
    },
    load(this: { addWatchFile(path: string): void }, id: string) {
      if (id !== slackModule) return;
      this.addWatchFile(slack);
      return readFileSync(slack, "utf8");
    },
  };
}

// A plugin's policy is policy.ts beside its __init__.py. In a plugin made of features
// (hermes-plugin-leon), each feature has its own beside its own __init__.py. Each policy builds on
// its own, so every policy.cjs is one file, which the bridge finds next to the Python that calls it.
const policies = globSync(["policy.ts", "*/policy.ts"]).sort();

export default defineConfig({
  pack: policies.map((policy, index) => ({
    entry: { [policy.replace(/\.ts$/, "")]: policy },
    format: "cjs",
    platform: "neutral",
    outDir: ".",
    clean: false,
    dts: false,
    fixedExtension: true,
    plugins: [pluginFiles(index === 0)],
  })),
});
