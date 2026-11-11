// HTML Builder: a durable artifact builder on the TinyAgent TypeScript SDK.
//
//   node server.ts                    scripted mode unless an API key is set
//   node server.ts --allow-kill       adds the "Kill server" button and a supervisor that restarts the server
//
// One process, one public port. The SDK's own `serve()` does all the agent
// work on a private loopback port; this file puts a page, the published
// designs and a few demo routes in front of it and forwards the rest.
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createServer, request as httpRequest, type IncomingMessage, type ServerResponse } from "node:http";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { Agent, AnthropicProvider, ChatCompletionsProvider, OpenAIProvider, serve,
  type Provider } from "../../tinyagents/typescript/sdk.ts";
import { listVersions, publishTool } from "./designs.ts";
import { scriptedDelay, scriptedProvider } from "./scripted.ts";

const { values: flags } = parseArgs({ options: {
  port: { type: "string" },
  data: { type: "string" },
  scripted: { type: "boolean", default: false },
  "allow-kill": { type: "boolean", default: false },
} });
const env = process.env;

/**
 * The supervisor: run this same file as a child and start it again whenever
 * it dies. The pause is only there so the outage is long enough to see.
 */
function supervise(): void {
  let stopping = false;
  let child: ReturnType<typeof spawn>;
  const start = () => {
    child = spawn(process.execPath, [...process.execArgv, ...process.argv.slice(1)],
      { stdio: "inherit", env: { ...env, HTML_BUILDER_SUPERVISED: "1" } });
    child.on("exit", (code) => {
      if (stopping || code === 0) process.exit(0);
      console.log(`supervisor: server exited with code ${code}; restarting in 2 s`);
      setTimeout(start, 2000);
    });
  };
  for (const signal of ["SIGINT", "SIGTERM"] as const)
    process.on(signal, () => { stopping = true; child.kill(signal); });
  start();
}

async function main(): Promise<void> {
  const here = import.meta.dirname;
  const dataDir = resolve(flags.data ?? join(here, "data"));
  const sessionsDir = join(dataDir, "sessions");
  const designsDir = join(dataDir, "designs");
  mkdirSync(sessionsDir, { recursive: true });
  mkdirSync(designsDir, { recursive: true });

  // ---- Models: whatever the environment has keys for, plus the offline script. ----
  interface Choice { id: string; label: string; provider: Provider; models: string[] }
  const typing = Number(env.HTML_BUILDER_TYPING_MS ?? 28);
  const choices: Choice[] = [{ id: "scripted", label: "Scripted (offline, no key)", provider: scriptedProvider(typing), models: ["canned-build"] }];
  if (!flags.scripted) {
    const named = (env.TINYAGENT_PROVIDER ?? "openai").toLowerCase();
    const models = (id: string, fallback: string[]) => (env.TINYAGENT_MODEL && named === id ? [env.TINYAGENT_MODEL] : fallback);
    // With a base URL set, OPENAI_API_KEY is that server's key and is never sent to api.openai.com.
    if (env.TINYAGENT_BASE_URL) choices.push({ id: "chat", label: `OpenAI-compatible (${new URL(env.TINYAGENT_BASE_URL).host})`,
      provider: new ChatCompletionsProvider({ baseUrl: env.TINYAGENT_BASE_URL, apiKey: env.OPENAI_API_KEY ?? "" }),
      models: env.TINYAGENT_MODEL ? [env.TINYAGENT_MODEL] : [] });
    else if (env.OPENAI_API_KEY) choices.push({ id: "openai", label: "OpenAI",
      provider: new OpenAIProvider({ apiKey: env.OPENAI_API_KEY }), models: models("openai", ["gpt-5.6"]) });
    if (env.ANTHROPIC_API_KEY) choices.push({ id: "anthropic", label: "Anthropic",
      provider: new AnthropicProvider({ apiKey: env.ANTHROPIC_API_KEY, maxTokens: 16_384 }),
      models: models("anthropic", ["claude-sonnet-4-6"]) });
  }
  const scriptedOnly = choices.length === 1;

  // The SDK's HTTP server wraps one agent with one model, and a prompt sent over
  // HTTP cannot name another. So the agent's provider is a small router, and the
  // choice made in the page is kept here and on disk, where a resumed run finds it.
  const settingsFile = join(dataDir, "settings.json");
  let selected = { provider: choices.at(scriptedOnly ? 0 : 1)!.id, model: choices.at(scriptedOnly ? 0 : 1)!.models[0] ?? "" };
  try {
    const saved = JSON.parse(readFileSync(settingsFile, "utf8")) as typeof selected;
    if (choices.some((choice) => choice.id === saved.provider) && typeof saved.model === "string") selected = saved;
  } catch { /* no saved choice */ }
  const router: Provider = {
    stream(request) {
      const choice = choices.find((item) => item.id === selected.provider) ?? choices[0];
      return choice.provider.stream({ ...request, model: selected.model });
    },
  };

  const slow = Number(env.HTML_BUILDER_SLOW_MS ?? 8000);
  const agent = new Agent({
    model: "chosen-in-the-page",
    provider: router,
    maxSteps: 12,
    instructions: [
      "You design and build single web pages.",
      "Every time the user asks for a page or a change, call publish_design with a COMPLETE, self-contained HTML document.",
      "All CSS and JavaScript go inline. Use no external files, fonts, images, scripts or network requests of any kind:",
      "the page is shown in a sandbox with no network. Draw imagery with CSS or inline SVG.",
      "Make it responsive, accessible and carefully typeset. Avoid generic templates.",
      "Before the tool call, say in one sentence what you are about to build. For a revision, publish the whole document again.",
      "After publishing, reply with one or two sentences about what you made. Never paste the HTML into your reply.",
    ].join(" "),
    // Only the scripted build is slowed down. A real model is slow enough.
    tools: [publishTool(designsDir, (version) => (selected.provider === "scripted" ? scriptedDelay(slow)(version) : 0))],
  });

  // ---- The page's own server. It listens first so the SDK can be told the page's origin. ----
  const bootId = randomBytes(6).toString("hex");
  const token = randomBytes(24).toString("hex");
  let apiPort = 0;
  let origins: string[] = [];

  const front = createServer((request, response) => {
    handle(request, response).catch((error: unknown) => {
      console.error(error);
      if (response.headersSent) response.destroy();
      else sendJson(response, 500, { error: { code: "internal_error", message: "Request failed", retryable: true } });
    });
  });

  async function handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    // The same two checks the SDK makes, for the routes that never reach it: the
    // request must be addressed to this machine, and a browser must be sending
    // it from this page. The sandboxed preview has the origin "null" and fails here.
    const host = (request.headers.host ?? "").toLowerCase().replace(/:\d+$/, "");
    if (host !== "127.0.0.1" && host !== "localhost")
      return sendJson(response, 421, { error: { code: "misdirected_request", message: "Host is not this server", retryable: false } });
    if (request.headers.origin !== undefined && !origins.includes(request.headers.origin))
      return sendJson(response, 403, { error: { code: "origin_not_allowed", message: "Requests from this origin are not allowed", retryable: false } });

    const path = (request.url ?? "").split("?")[0];
    const method = request.method ?? "GET";
    if (path === "/health" || path === "/sessions" || path.startsWith("/sessions/")) return forward(request, response);

    if (method === "GET" && (path === "/" || path === "/index.html")) {
      const nonce = randomBytes(16).toString("base64");
      response.writeHead(200, { ...COMMON_HEADERS, "Content-Type": "text/html; charset=utf-8",
        "Content-Security-Policy": `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; ` +
          "connect-src 'self'; img-src 'self' data: blob:; frame-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" });
      response.end(readFileSync(join(here, "index.html"), "utf8").replaceAll("__NONCE__", nonce));
      return;
    }
    if (method === "GET" && path === "/demo/config")
      return sendJson(response, 200, { bootId, pid: process.pid, allowKill: flags["allow-kill"], scriptedOnly, selected,
        providers: choices.map(({ id, label, models }) => ({ id, label, models })) });
    if (method === "POST" && path === "/demo/model") {
      const body = await readJson(request);
      const choice = choices.find((item) => item.id === body.provider);
      const model = typeof body.model === "string" ? body.model.trim().slice(0, 120) : "";
      if (!choice || !model) return sendJson(response, 400, { error: { code: "invalid_request", message: "Unknown provider or empty model", retryable: false } });
      selected = { provider: choice.id, model };
      writeFileSync(settingsFile, JSON.stringify(selected));
      return sendJson(response, 200, { selected });
    }
    if (method === "POST" && path === "/demo/kill") {
      if (!flags["allow-kill"])
        return sendJson(response, 403, { error: { code: "kill_disabled", message: "Start the server with --allow-kill", retryable: false } });
      console.log("server: killed from the page, exiting without cleanup");
      // No close(), no flush, no goodbye to open connections: as close to a crash as a process can ask for.
      process.exit(1);
    }
    const design = /^\/demo\/designs\/(s_[A-Za-z0-9]+)(?:\/(\d+))?$/.exec(path);
    if (method === "GET" && design) {
      const versions = listVersions(designsDir, design[1]);
      if (design[2] === undefined) return sendJson(response, 200, { versions: versions.map(({ html: _, ...rest }) => rest) });
      const version = versions.find((item) => item.version === Number(design[2]));
      if (!version) return sendJson(response, 404, { error: { code: "not_found", message: "No such version", retryable: false } });
      // Model output, served as a document that can run its own scripts and nothing else. The `sandbox`
      // directive gives it an origin of its own even when it is opened directly in a tab, and
      // `default-src 'none'` leaves it no way to fetch, post or load anything from anywhere.
      response.writeHead(200, { ...COMMON_HEADERS, "Content-Type": "text/html; charset=utf-8",
        "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
          "img-src data: blob:; font-src data:; media-src data: blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'" });
      response.end(version.html);
      return;
    }
    sendJson(response, 404, { error: { code: "not_found", message: "Unknown route", retryable: false } });
  }

  /** Pass an agent request to the SDK's server as it came, Host and Origin included, so its checks still decide. */
  function forward(request: IncomingMessage, response: ServerResponse): void {
    const upstream = httpRequest({ host: "127.0.0.1", port: apiPort, method: request.method, path: request.url,
      headers: { ...request.headers, authorization: `Bearer ${token}` } }, (reply) => {
      response.writeHead(reply.statusCode ?? 502, { ...reply.headers, "referrer-policy": "no-referrer" });
      reply.pipe(response);
    });
    upstream.on("error", () => {
      if (response.headersSent) response.destroy();
      else sendJson(response, 502, { error: { code: "internal_error", message: "The agent server did not answer", retryable: true } });
    });
    response.on("close", () => upstream.destroy());
    request.pipe(upstream);
  }

  const port = Number(flags.port ?? env.PORT ?? 4380);
  await new Promise<void>((done, fail) => front.once("error", fail).listen(port, "127.0.0.1", done));
  const address = front.address();
  if (!address || typeof address === "string") throw new Error("no TCP address");
  origins = [`http://127.0.0.1:${address.port}`, `http://localhost:${address.port}`];

  // The agent server: the SDK's, unchanged. It trusts exactly the page's origin, and
  // its random token means nothing but this process can use its port directly.
  const api = await serve(agent, { host: "127.0.0.1", port: 0, token, directory: sessionsDir, allowedOrigins: origins });
  apiPort = Number(new URL(api.url).port);

  console.log(`html-builder: ${origins[0]}  (${scriptedOnly ? "scripted mode, no API key" : choices.slice(1).map((choice) => choice.label).join(", ")}` +
    `${flags["allow-kill"] ? ", kill button on" : ""}, pid ${process.pid})`);
  for (const signal of ["SIGINT", "SIGTERM"] as const) process.on(signal, () => {
    front.close();
    front.closeAllConnections();
    void api.close().finally(() => process.exit(0));
  });
}

const COMMON_HEADERS = {
  "Cache-Control": "no-store",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "Cross-Origin-Resource-Policy": "same-origin",
};

function sendJson(response: ServerResponse, status: number, value: object): void {
  response.writeHead(status, { ...COMMON_HEADERS, "Content-Type": "application/json; charset=utf-8" }).end(JSON.stringify(value));
}

async function readJson(request: IncomingMessage): Promise<Record<string, unknown>> {
  let text = "";
  for await (const chunk of request) {
    text += String(chunk);
    if (text.length > 4096) return {};
  }
  try {
    const body: unknown = JSON.parse(text);
    return typeof body === "object" && body !== null ? body as Record<string, unknown> : {};
  } catch {
    return {};
  }
}

if (flags["allow-kill"] && !env.HTML_BUILDER_SUPERVISED) supervise();
else await main();
