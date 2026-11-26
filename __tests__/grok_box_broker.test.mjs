import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, test } from "node:test";

import {
  cookieParam,
  createBroker,
  rewriteDiscovery,
  seedCookies,
  settings,
  upstreamHeaders,
} from "../grok_box_browser/box/broker.mjs";

const TOKEN = "t".repeat(32);

/** A stand-in for one Chrome: /json/version, /json/list, and a WebSocket endpoint that echoes. */
function fakeChrome() {
  const seen = [];
  const server = http.createServer((request, response) => {
    seen.push({ path: request.url, host: request.headers.host, origin: request.headers.origin });
    const { port } = server.address();
    const body =
      request.url === "/json/list"
        ? [{ id: "p1", webSocketDebuggerUrl: `ws://127.0.0.1:${port}/devtools/page/p1` }]
        : {
            Browser: "Chrome/156",
            webSocketDebuggerUrl: `ws://127.0.0.1:${port}/devtools/browser/abc`,
          };
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify(body));
  });
  server.on("upgrade", (request, socket) => {
    seen.push({ upgrade: request.url, host: request.headers.host, origin: request.headers.origin });
    socket.write(
      "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n",
    );
    socket.on("data", (data) => socket.write(data));
  });
  return new Promise((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve({ server, seen, port: server.address().port })),
  );
}

let home;
let clock;
let chromes;
let seeds;
let broker;
let base;

async function start(overrides = {}) {
  const config = { ...settings({ GBB_HOME: home }), max: 2, ...overrides };
  broker = createBroker(config, {
    token: TOKEN,
    now: () => clock,
    log: () => {},
    seed: async (wsUrl) => {
      seeds.push(wsUrl);
      return 3;
    },
    launch: async ({ task, slot, profile }) => {
      const chrome = await fakeChrome();
      const handle = {
        task,
        slot,
        profile,
        port: chrome.port,
        stopped: false,
        stop() {
          handle.stopped = true;
          chrome.server.close();
          chrome.server.closeAllConnections?.();
        },
        exited: new Promise(() => {}),
        chrome,
      };
      chromes.push(handle);
      return handle;
    },
  });
  await new Promise((resolve) => broker.server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${broker.server.address().port}`;
}

async function api(method, pathname, body, token = TOKEN) {
  const response = await fetch(base + pathname, {
    method,
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, body: await response.json() };
}

beforeEach(() => {
  home = fs.mkdtempSync(path.join(os.tmpdir(), "gbb-"));
  clock = 1_800_000_000_000;
  chromes = [];
  seeds = [];
});

afterEach(async () => {
  broker?.stopAll();
  const closed = new Promise((resolve) => (broker ? broker.server.close(resolve) : resolve()));
  broker?.server.closeAllConnections();
  await closed;
  fs.rmSync(home, { recursive: true, force: true });
});

test("Grok Bot's seed cookies become setCookies params", () => {
  const now = 1_800_000_000;
  const seed = JSON.stringify({
    cookies: [
      {
        name: "a",
        value: "1",
        domain: ".x.com",
        path: "/",
        expires: now + 60,
        session: false,
        size: 2,
        httpOnly: true,
        secure: true,
        priority: "Medium",
        sourceScheme: "Secure",
        sourcePort: 443,
      },
      { name: "gone", value: "1", domain: "x.com", path: "/", expires: now - 60, session: false },
      {
        name: "s",
        value: "2",
        domain: "y.com",
        path: "/",
        expires: -1,
        session: true,
        sameSite: "Lax",
      },
      { value: "no name", domain: "z.com" },
    ],
  });
  assert.deepEqual(seedCookies(seed, now), [
    {
      name: "a",
      value: "1",
      domain: ".x.com",
      path: "/",
      secure: true,
      httpOnly: true,
      expires: now + 60,
      priority: "Medium",
      sourceScheme: "Secure",
      sourcePort: 443,
    },
    {
      name: "s",
      value: "2",
      domain: "y.com",
      path: "/",
      secure: false,
      httpOnly: false,
      sameSite: "Lax",
    },
  ]);
  assert.equal(cookieParam(null), null);
});

test("discovery answers and headers are rewritten for the tunnel", () => {
  const body =
    '{"webSocketDebuggerUrl":"ws://127.0.0.1:9401/devtools/browser/abc","devtoolsFrontendUrl":"/devtools/inspector.html?ws=127.0.0.1:9401/devtools/page/p1"}';
  assert.equal(
    rewriteDiscovery(body, 9401, "127.0.0.1:19400", "/s/SECRET"),
    '{"webSocketDebuggerUrl":"ws://127.0.0.1:19400/s/SECRET/devtools/browser/abc","devtoolsFrontendUrl":"/devtools/inspector.html?ws=127.0.0.1:19400/s/SECRET/devtools/page/p1"}',
  );
  assert.deepEqual(
    upstreamHeaders(
      [
        "Host",
        "grok-box:9400",
        "Origin",
        "http://evil",
        "Authorization",
        "Bearer x",
        "Upgrade",
        "websocket",
      ],
      9401,
    ),
    [
      ["Host", "127.0.0.1:9401"],
      ["Upgrade", "websocket"],
    ],
  );
});

test("each task gets one Chrome, seeded once, behind a secret path", async () => {
  await start();
  assert.equal((await api("POST", "/v1/sessions", { task: "leon-532" }, "wrong")).status, 401);
  assert.equal((await api("POST", "/v1/sessions", { task: "Bad Task" })).status, 400);

  const first = await api("POST", "/v1/sessions", { task: "leon-532" });
  assert.equal(first.status, 200);
  assert.match(first.body.ws, /^\/s\/[A-Za-z0-9_-]{24}\/devtools\/browser\/abc$/);
  assert.equal(first.body.seeded, 3);
  assert.equal(seeds.length, 1);
  // The same task gets the same browser while it runs.
  const again = await api("POST", "/v1/sessions", { task: "leon-532" });
  assert.equal(again.body.ws, first.body.ws);
  assert.equal(chromes.length, 1);

  // Discovery through the broker points back at the broker, with Chrome's own Host and no Origin.
  const version = await fetch(`${base}${first.body.http}/json/version`, {
    headers: { origin: "http://x" },
  });
  const discovered = await version.json();
  assert.equal(
    discovered.webSocketDebuggerUrl,
    `ws://127.0.0.1:${broker.server.address().port}${first.body.ws}`,
  );
  const request = chromes[0].chrome.seen.at(-1);
  assert.equal(request.host, `127.0.0.1:${chromes[0].port}`);
  assert.equal(request.origin, undefined);
  assert.equal((await fetch(`${base}/s/${"x".repeat(24)}/json/version`)).status, 404);

  // A stopped Chrome restarts on the same profile without seeding it again.
  assert.equal((await api("DELETE", "/v1/sessions/leon-532")).body.stopped, true);
  const restarted = await api("POST", "/v1/sessions", { task: "leon-532" });
  assert.notEqual(restarted.body.ws, first.body.ws);
  assert.equal(restarted.body.seeded, 0);
  assert.equal(seeds.length, 1);
  assert.equal(chromes[1].profile, path.join(home, "profiles", "leon-532"));
});

test("the DevTools websocket passes through the broker", async () => {
  await start();
  const { body } = await api("POST", "/v1/sessions", { task: "leon-1" });
  const socket = net.connect(broker.server.address().port, "127.0.0.1");
  await new Promise((resolve) => socket.once("connect", resolve));
  socket.write(
    `GET ${body.ws} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nOrigin: http://x\r\nSec-WebSocket-Key: a\r\nSec-WebSocket-Version: 13\r\n\r\n`,
  );
  let received = "";
  socket.on("data", (data) => (received += data));
  await new Promise((resolve) => {
    const check = () => (received.includes("101") ? resolve() : setTimeout(check, 10));
    check();
  });
  socket.write("ping");
  await new Promise((resolve) => {
    const check = () => (received.endsWith("ping") ? resolve() : setTimeout(check, 10));
    check();
  });
  const upgrade = chromes[0].chrome.seen.find((entry) => entry.upgrade);
  assert.equal(upgrade.upgrade, "/devtools/browser/abc");
  assert.equal(upgrade.origin, undefined);
  socket.destroy();
});

test("idle browsers stop, the oldest idle one makes room, and retired profiles go after a week", async () => {
  await start();
  await api("POST", "/v1/sessions", { task: "leon-1" });
  clock += 5 * 60_000;
  await api("POST", "/v1/sessions", { task: "leon-2" });
  // Both slots are taken; leon-1 has been idle longest, so leon-3 takes its slot.
  clock += 5 * 60_000;
  await api("POST", "/v1/sessions", { task: "leon-3" });
  assert.equal(chromes[0].stopped, true);
  assert.equal(chromes[2].slot, chromes[0].slot);
  let listed = (await api("GET", "/v1/sessions")).body.sessions;
  assert.deepEqual(
    listed
      .filter((entry) => entry.running)
      .map((entry) => entry.task)
      .sort(),
    ["leon-2", "leon-3"],
  );

  // No idle browser to evict: a fourth task is refused rather than killing work in progress.
  await api("POST", "/v1/sessions", { task: "leon-2" });
  await api("POST", "/v1/sessions", { task: "leon-3" });
  assert.equal((await api("POST", "/v1/sessions", { task: "leon-4" })).status, 503);

  clock += 31 * 60_000;
  broker.sweep();
  assert.equal(chromes[1].stopped && chromes[2].stopped, true);

  assert.equal((await api("POST", "/v1/sessions/leon-2/retire")).body.retired, true);
  fs.writeFileSync(path.join(home, "profiles", "leon-2", "Cookies"), "x");
  clock += 6 * 86_400_000;
  broker.sweep();
  assert.equal(fs.existsSync(path.join(home, "profiles", "leon-2")), true);
  clock += 2 * 86_400_000;
  broker.sweep();
  assert.equal(fs.existsSync(path.join(home, "profiles", "leon-2")), false);
  listed = (await api("GET", "/v1/sessions")).body.sessions;
  assert.deepEqual(listed.map((entry) => entry.task).sort(), ["leon-1", "leon-3"]);
  assert.equal((await fetch(`${base}/health`)).status, 200);
});
