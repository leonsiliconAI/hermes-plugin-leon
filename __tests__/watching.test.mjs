import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";

const require = createRequire(import.meta.url);
const autolinear = require("../autolinear/policy.cjs");
const hermes22muse = require("../hermes22muse/policy.cjs");

test("Leon's DM and project channels start issues, a Watcher's watcher-… channel included", () => {
  assert.equal(autolinear.intakeChannel("D0LEONDM01", ""), true);
  assert.equal(autolinear.intakeChannel("C0PROJECT1", "project-2026-11-26"), true);
  assert.equal(autolinear.intakeChannel("C0WATCHER1", "watcher-content-sponsorships"), true);
  assert.equal(autolinear.intakeChannel("C0ISSUE001", "leon-12-fix-the-printer"), false);
  assert.equal(autolinear.intakeChannel("C0GENERAL1", "general"), false);
  // A name Hermes couldn't read comes back as the ID.
  assert.equal(autolinear.intakeChannel("C0UNKNOWN1", "C0UNKNOWN1"), false);
});

test("Hermes waits with Watching on the issue, never a new project (Leon, 2026-11-28)", () => {
  const prompt = autolinear.taskPrompt(true, false, "U0AUTOLIN01");
  assert.match(prompt, /`move watching --who "<who>"`/);
  assert.match(prompt, /`move watching --event "<what>" --stop YYYY-MM-DD`/);
  assert.match(prompt, /never make a Watcher project for one wait/);
  assert.doesNotMatch(prompt, /move trigger/);

  const guide = hermes22muse.handoffGuide("U0LEON00001");
  assert.match(guide, /move it to Watching \(`move watching`\)/);
  assert.match(guide, /Never create a project, Watcher or otherwise, for a single wait/);
});
