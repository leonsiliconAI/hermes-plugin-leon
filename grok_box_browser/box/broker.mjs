#!/usr/bin/env node
// grok-box-browser broker: one Chrome per task in Leon's Grok Bot cloud computer, which Hermes drives
// over the tailnet through Chrome's DevTools protocol. See ../README.md.
//
// Runs on Node 22+ or Bun with no dependencies. It listens on loopback only; `tailscale serve`
// carries the tailnet port to it.

import { spawn } from "node:child_process";
import { randomBytes, timingSafeEqual } from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import path from "node:path";
import { pathToFileURL } from "node:url";

export const VERSION = "1.0.0";
const TASK = /^[a-z0-9][a-z0-9-]{0,63}$/;
const SECRET_PATH = /^\/s\/([A-Za-z0-9_-]{24})(\/.*)?$/;

export function settings(env = process.env) {
  const home = env.GBB_HOME || "/workspace/hermes-browsers";
  const number = (name, fallback) => {
    const value = Number(env[name] ?? fallback);
    return Number.isFinite(value) && value > 0 ? value : fallback;
  };
  return {
    home,
    port: number("GBB_PORT", 9400),
    max: number("GBB_MAX_BROWSERS", 6),
    idleMs: number("GBB_IDLE_MINUTES", 30) * 60_000,
    retainMs: number("GBB_RETAIN_DAYS", 7) * 86_400_000,
    unusedMs: number("GBB_UNUSED_DAYS", 30) * 86_400_000,
    seed: env.GBB_COOKIE_SEED || "/home/box/sand-data/chrome-cookie-seed.json",
    chrome: env.GBB_CHROME || "/usr/bin/google-chrome",
    xvfb: env.GBB_XVFB || "/usr/bin/Xvfb",
    headless: env.GBB_HEADLESS === "1",
    displayBase: number("GBB_DISPLAY_BASE", 100),
    cdpBase: number("GBB_CDP_BASE", 9401),
    tokenFile: env.GBB_TOKEN_FILE || path.join(home, "token"),
  };
}

/** A cookie from Grok Bot's seed file (Chrome's `Network.Cookie` shape) as a `Storage.setCookies` param. */
export function cookieParam(cookie, now = Date.now() / 1000) {
  if (!cookie || typeof cookie.name !== "string" || typeof cookie.domain !== "string") return null;
  const expiring = !cookie.session && typeof cookie.expires === "number" && cookie.expires > 0;
  if (expiring && cookie.expires < now) return null;
  const param = {
    name: cookie.name,
    value: typeof cookie.value === "string" ? cookie.value : "",
    domain: cookie.domain,
    path: cookie.path || "/",
    secure: Boolean(cookie.secure),
    httpOnly: Boolean(cookie.httpOnly),
  };
  if (expiring) param.expires = cookie.expires;
  for (const key of ["sameSite", "priority", "sourceScheme", "partitionKey"])
    if (cookie[key] !== undefined) param[key] = cookie[key];
  if (Number.isInteger(cookie.sourcePort)) param.sourcePort = cookie.sourcePort;
  return param;
}

export function seedCookies(text, now) {
  const data = JSON.parse(text);
  const list = Array.isArray(data) ? data : Array.isArray(data?.cookies) ? data.cookies : [];
  return list.map((cookie) => cookieParam(cookie, now)).filter(Boolean);
}

/**
 * Chrome's /json answers name its own loopback address. Point them at this broker's path for the
 * session, as the client reached it, so a client follows them back through the tunnel.
 */
export function rewriteDiscovery(body, chromePort, publicHost, prefix) {
  const from = `127.0.0.1:${chromePort}/`;
  return body.split(from).join(`${publicHost}${prefix}/`);
}

/** The header block sent to Chrome for a proxied request: its own Host, no Origin (Chrome refuses one). */
export function upstreamHeaders(rawHeaders, chromePort) {
  const lines = [];
  for (let i = 0; i < rawHeaders.length; i += 2) {
    const name = rawHeaders[i];
    const lower = name.toLowerCase();
    if (lower === "origin" || lower === "authorization") continue;
    lines.push([name, lower === "host" ? `127.0.0.1:${chromePort}` : rawHeaders[i + 1]]);
  }
  return lines;
}

function bearerMatches(header, token) {
  if (!token || typeof header !== "string" || !header.startsWith("Bearer ")) return false;
  const given = Buffer.from(header.slice(7));
  const want = Buffer.from(token);
  return given.length === want.length && timingSafeEqual(given, want);
}

/** Chrome and its X display for one task, started on demand. */
export function chromeLauncher(config, log) {
  return async function launch({ task, slot, profile }) {
    const display = config.displayBase + slot;
    const port = config.cdpBase + slot;
    const processes = [];
    const env = { ...process.env };
    if (!config.headless) {
      // A display a crashed run left behind keeps its lock file; Xvfb refuses to start over it.
      for (const lock of [`/tmp/.X${display}-lock`, `/tmp/.X11-unix/X${display}`])
        fs.rmSync(lock, { force: true });
      const xvfb = spawn(
        config.xvfb,
        [`:${display}`, "-screen", "0", "1920x1200x24", "-nolisten", "tcp", "-noreset"],
        { detached: true, stdio: "ignore" },
      );
      processes.push(xvfb);
      env.DISPLAY = `:${display}`;
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
    const args = [
      `--user-data-dir=${profile}`,
      "--remote-debugging-address=127.0.0.1",
      `--remote-debugging-port=${port}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--password-store=basic",
      "--disable-dev-shm-usage",
      "--window-size=1920,1200",
      "--start-maximized",
    ];
    if (config.headless) args.push("--headless=new");
    args.push("about:blank");
    const chrome = spawn(config.chrome, args, { detached: true, stdio: "ignore", env });
    processes.push(chrome);
    log("info", "chrome starting", { task, slot, display, port });
    return {
      port,
      display,
      stop() {
        for (const child of processes.reverse()) {
          try {
            process.kill(-child.pid, "SIGTERM");
          } catch {}
        }
        setTimeout(() => {
          for (const child of processes) {
            try {
              process.kill(-child.pid, "SIGKILL");
            } catch {}
          }
        }, 5000).unref();
      },
      exited: new Promise((resolve) => chrome.once("exit", resolve)),
    };
  };
}

/** `Storage.setCookies` over Chrome's browser-level DevTools socket, in batches. */
export async function applyCookies(wsUrl, cookies, WebSocketImpl = globalThis.WebSocket) {
  if (!cookies.length) return 0;
  if (typeof WebSocketImpl !== "function") throw new Error("this runtime has no WebSocket client");
  const socket = new WebSocketImpl(wsUrl);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = () => reject(new Error("couldn't reach Chrome's DevTools socket"));
  });
  let next = 0;
  const pending = new Map();
  socket.onmessage = (event) => {
    const message = JSON.parse(String(event.data));
    const settle = pending.get(message.id);
    if (!settle) return;
    pending.delete(message.id);
    settle(message);
  };
  const send = (method, params) =>
    new Promise((resolve, reject) => {
      const id = ++next;
      pending.set(id, (message) =>
        message.error ? reject(new Error(message.error.message)) : resolve(message.result),
      );
      socket.send(JSON.stringify({ id, method, params }));
    });
  let applied = 0;
  try {
    for (let start = 0; start < cookies.length; start += 200) {
      const batch = cookies.slice(start, start + 200);
      try {
        await send("Storage.setCookies", { cookies: batch });
        applied += batch.length;
      } catch {
        // One malformed cookie fails the whole batch: retry it one cookie at a time.
        for (const cookie of batch) {
          try {
            await send("Storage.setCookies", { cookies: [cookie] });
            applied += 1;
          } catch {}
        }
      }
    }
  } finally {
    socket.close();
  }
  return applied;
}

async function waitForChrome(port, timeoutMs = 30_000, fetchImpl = fetch) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      const response = await fetchImpl(`http://127.0.0.1:${port}/json/version`);
      if (response.ok) return await response.json();
    } catch {}
    if (Date.now() > deadline) throw new Error(`Chrome didn't open its DevTools port ${port}`);
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

function readState(file) {
  try {
    const state = JSON.parse(fs.readFileSync(file, "utf8"));
    return state && typeof state.tasks === "object" ? state : { tasks: {} };
  } catch {
    return { tasks: {} };
  }
}

/**
 * The broker: task sessions, the janitor and the HTTP surface. `launch`, `seed`, `now` and `wait`
 * are injectable for tests.
 */
export function createBroker(config, deps = {}) {
  const log =
    deps.log ??
    ((level, message, fields = {}) =>
      process.stdout.write(
        JSON.stringify({ ts: new Date().toISOString(), level, message, ...fields }) + "\n",
      ));
  const now = deps.now ?? Date.now;
  const launch = deps.launch ?? chromeLauncher(config, log);
  const wait = deps.waitForChrome ?? waitForChrome;
  const seed =
    deps.seed ??
    (async (wsUrl) =>
      applyCookies(wsUrl, seedCookies(fs.readFileSync(config.seed, "utf8"), now() / 1000)));
  const token = deps.token ?? fs.readFileSync(config.tokenFile, "utf8").trim();
  const stateFile = path.join(config.home, "state.json");
  const profiles = path.join(config.home, "profiles");
  const state = deps.state ?? readState(stateFile);
  const running = new Map(); // task -> session
  const bySecret = new Map(); // secret -> session
  const starting = new Map(); // task -> Promise<session>

  function save() {
    if (deps.state) return;
    fs.mkdirSync(config.home, { recursive: true });
    const temporary = stateFile + ".tmp";
    fs.writeFileSync(temporary, JSON.stringify(state, null, 2));
    fs.renameSync(temporary, stateFile);
  }

  function record(task) {
    state.tasks[task] ??= { created: now(), lastUsed: now() };
    return state.tasks[task];
  }

  function touch(session) {
    session.lastActive = now();
  }

  function describe(session) {
    return {
      task: session.task,
      ws: `/s/${session.secret}${session.wsPath}`,
      http: `/s/${session.secret}`,
      seeded: session.seeded,
    };
  }

  function stopTask(task, reason) {
    const session = running.get(task);
    if (!session) return false;
    running.delete(task);
    bySecret.delete(session.secret);
    session.handle.stop();
    record(task).lastUsed = now();
    save();
    log("info", "chrome stopped", { task, reason });
    return true;
  }

  function freeSlot() {
    const used = new Set([...running.values()].map((session) => session.slot));
    for (let slot = 0; slot < config.max; slot++) if (!used.has(slot)) return slot;
    // Full: give the least recently used idle browser's slot to the new task.
    const idle = [...running.values()]
      .filter((session) => now() - session.lastActive > 120_000)
      .sort((a, b) => a.lastActive - b.lastActive)[0];
    if (!idle) return -1;
    stopTask(idle.task, "evicted");
    return idle.slot;
  }

  async function start(task) {
    const slot = freeSlot();
    if (slot < 0) {
      const error = new Error(`all ${config.max} browsers are busy`);
      error.status = 503;
      throw error;
    }
    const profile = path.join(profiles, task);
    const entry = record(task);
    delete entry.retiredAt;
    fs.mkdirSync(profile, { recursive: true });
    const handle = await launch({ task, slot, profile });
    let version;
    try {
      version = await wait(handle.port);
    } catch (error) {
      handle.stop();
      throw error;
    }
    const wsUrl = String(version.webSocketDebuggerUrl || "");
    const wsPath = wsUrl.replace(/^ws:\/\/[^/]+/, "");
    let seeded = 0;
    if (!entry.seededAt) {
      try {
        seeded = await seed(wsUrl);
        entry.seededAt = now();
        log("info", "profile seeded", { task, cookies: seeded });
      } catch (error) {
        log("warn", "couldn't seed the profile's cookies", { task, error: String(error) });
      }
    }
    const session = {
      task,
      slot,
      handle,
      port: handle.port,
      wsPath,
      seeded,
      secret: randomBytes(18).toString("base64url"),
      lastActive: now(),
    };
    running.set(task, session);
    bySecret.set(session.secret, session);
    entry.lastUsed = now();
    save();
    handle.exited?.then(() => {
      if (running.get(task) === session) {
        running.delete(task);
        bySecret.delete(session.secret);
        log("warn", "chrome exited", { task });
      }
    });
    return session;
  }

  async function open(task) {
    if (!TASK.test(String(task ?? ""))) {
      const error = new Error("task must be lowercase letters, digits and hyphens");
      error.status = 400;
      throw error;
    }
    const current = running.get(task);
    if (current) {
      touch(current);
      record(task).lastUsed = now();
      return describe(current);
    }
    if (!starting.has(task))
      starting.set(
        task,
        start(task).finally(() => starting.delete(task)),
      );
    return describe(await starting.get(task));
  }

  function retire(task) {
    stopTask(task, "retired");
    const entry = state.tasks[task];
    if (!entry) return false;
    entry.retiredAt ??= now();
    save();
    return true;
  }

  function sweep() {
    for (const session of running.values())
      if (now() - session.lastActive > config.idleMs) stopTask(session.task, "idle");
    for (const [task, entry] of Object.entries(state.tasks)) {
      if (running.has(task)) continue;
      const retired = entry.retiredAt && now() - entry.retiredAt > config.retainMs;
      const abandoned = now() - (entry.lastUsed ?? entry.created ?? 0) > config.unusedMs;
      if (!retired && !abandoned) continue;
      fs.rmSync(path.join(profiles, task), { recursive: true, force: true });
      delete state.tasks[task];
      log("info", "profile deleted", { task, reason: retired ? "retired" : "unused" });
    }
    save();
  }

  function list() {
    return Object.entries(state.tasks).map(([task, entry]) => ({
      task,
      running: running.has(task),
      lastUsed: running.get(task)?.lastActive ?? entry.lastUsed,
      seededAt: entry.seededAt ?? null,
      retiredAt: entry.retiredAt ?? null,
    }));
  }

  function json(response, status, body) {
    const text = JSON.stringify(body);
    response.writeHead(status, {
      "content-type": "application/json",
      "content-length": Buffer.byteLength(text),
    });
    response.end(text);
  }

  async function readBody(request) {
    let text = "";
    for await (const chunk of request) {
      text += chunk;
      if (text.length > 4096) throw Object.assign(new Error("body too large"), { status: 413 });
    }
    return text ? JSON.parse(text) : {};
  }

  function proxy(request, response, session, rest) {
    touch(session);
    const headers = Object.fromEntries(upstreamHeaders(request.rawHeaders, session.port));
    const upstream = http.request(
      { host: "127.0.0.1", port: session.port, method: request.method, path: rest, headers },
      (answer) => {
        if (!rest.startsWith("/json")) {
          response.writeHead(answer.statusCode ?? 502, answer.headers);
          answer.pipe(response);
          return;
        }
        let body = "";
        answer.setEncoding("utf8");
        answer.on("data", (chunk) => (body += chunk));
        answer.on("end", () => {
          const text = rewriteDiscovery(
            body,
            session.port,
            request.headers.host ?? `127.0.0.1:${config.port}`,
            `/s/${session.secret}`,
          );
          const answerHeaders = { ...answer.headers };
          for (const name of ["content-length", "transfer-encoding", "connection"])
            delete answerHeaders[name];
          response.writeHead(answer.statusCode ?? 502, {
            ...answerHeaders,
            "content-length": Buffer.byteLength(text),
          });
          response.end(text);
        });
      },
    );
    upstream.on("error", () => json(response, 502, { error: "chrome_unreachable" }));
    request.pipe(upstream);
  }

  async function handle(request, response) {
    const url = new URL(request.url ?? "/", "http://broker");
    const secret = SECRET_PATH.exec(url.pathname);
    if (secret) {
      const session = bySecret.get(secret[1]);
      if (!session) return json(response, 404, { error: "unknown_session" });
      return proxy(request, response, session, (secret[2] || "/") + url.search);
    }
    if (url.pathname === "/health")
      return json(response, 200, {
        ok: true,
        version: VERSION,
        running: running.size,
        max: config.max,
      });
    if (!bearerMatches(request.headers.authorization, token))
      return json(response, 401, { error: "unauthorized" });
    if (request.method === "GET" && url.pathname === "/v1/sessions")
      return json(response, 200, { sessions: list() });
    const route = /^\/v1\/sessions(?:\/([^/]+))?(?:\/(release|retire))?$/.exec(url.pathname);
    if (!route) return json(response, 404, { error: "not_found" });
    const [, task, action] = route;
    if (request.method === "POST" && !task) {
      const body = await readBody(request);
      return json(response, 200, await open(body.task));
    }
    if (request.method === "POST" && task && action === "release") {
      const session = running.get(task);
      if (session) touch(session);
      return json(response, 200, { released: Boolean(session) });
    }
    if (request.method === "POST" && task && action === "retire")
      return json(response, 200, { retired: retire(task) });
    if (request.method === "DELETE" && task && !action)
      return json(response, 200, { stopped: stopTask(task, "requested") });
    return json(response, 404, { error: "not_found" });
  }

  function upgrade(request, socket, head) {
    const url = new URL(request.url ?? "/", "http://broker");
    const secret = SECRET_PATH.exec(url.pathname);
    const session = secret && bySecret.get(secret[1]);
    if (!session) {
      socket.end("HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n");
      return;
    }
    touch(session);
    const upstream = net.connect(session.port, "127.0.0.1", () => {
      const lines = [`${request.method} ${(secret[2] || "/") + url.search} HTTP/1.1`];
      for (const [name, value] of upstreamHeaders(request.rawHeaders, session.port))
        lines.push(`${name}: ${value}`);
      upstream.write(lines.join("\r\n") + "\r\n\r\n");
      if (head?.length) upstream.write(head);
      socket.pipe(upstream);
      upstream.pipe(socket);
    });
    const active = () => touch(session);
    socket.on("data", active);
    upstream.on("data", active);
    const close = () => {
      socket.destroy();
      upstream.destroy();
    };
    // The HTTP server's sockets allow half-open connections: a side that ends closes both.
    for (const side of [socket, upstream])
      for (const event of ["end", "error", "close"]) side.on(event, close);
  }

  const server = http.createServer((request, response) => {
    handle(request, response).catch((error) => {
      if (!response.headersSent)
        json(response, error.status ?? 500, { error: String(error.message ?? error) });
    });
  });
  server.on("upgrade", upgrade);

  return {
    server,
    open,
    retire,
    stop: stopTask,
    sweep,
    list,
    stopAll() {
      for (const task of running.keys()) stopTask(task, "shutdown");
    },
  };
}

async function main() {
  const config = settings();
  const broker = createBroker(config);
  const janitor = setInterval(() => broker.sweep(), 60_000);
  const shutdown = () => {
    clearInterval(janitor);
    broker.stopAll();
    broker.server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 3000).unref();
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
  broker.server.listen(config.port, "127.0.0.1", () =>
    process.stdout.write(
      JSON.stringify({
        level: "info",
        message: "broker listening",
        port: config.port,
        version: VERSION,
      }) + "\n",
    ),
  );
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href)
  main().catch((error) => {
    process.stderr.write(`${error?.stack ?? error}\n`);
    process.exit(1);
  });
