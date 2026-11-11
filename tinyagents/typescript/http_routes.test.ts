import assert from "node:assert/strict";
import test from "node:test";
import { Agent, MAX_HTTP_BODY_BYTES, type ServerOptions } from "./tiny_agent.ts";
import { createHttpServer } from "./http.ts";
import { HttpFixture, body, json, object } from "./http_fixture.ts";

test("HTTP validates exposure options before constructing a listener", async (t) => {
  const agent = new Agent({ model: "fixture", transport: () => [] });
  const invalid: ServerOptions[] = [
    { host: "example.com" },
    { host: "0.0.0.0" },
    { host: "::", token: "secret" },
    { host: "0.0.0.0", allowRemote: true },
    { token: "" },
    { token: "bad token" },
    { token: "é" },
    { port: -1 },
    { port: 65536 },
    { port: 1.5 },
    { port: Number.NaN },
  ];
  for (const options of invalid) assert.throws(() => createHttpServer(agent, options));
  for (const options of [
    {},
    { host: "localhost", port: 0 },
    { host: "::1" },
    { host: "0.0.0.0", token: "secret", allowRemote: true },
  ]) {
    const application = createHttpServer(agent, options);
    t.after(() => application.close());
    assert.equal(application.host, options.host === "localhost" ? "127.0.0.1" : (options.host ?? "127.0.0.1"));
    assert.equal(application.port, options.port ?? 8080);
    assert.equal(application.server.listening, false);
  }
});

test("native HTTP authentication rejects absent, wrong, and duplicate credentials", { timeout: 5000 }, async (t) => {
  const fixture = new HttpFixture(new Agent({ model: "fixture", transport: () => [] }), { token: "secret" });
  t.after(() => fixture.dispose());
  const authorized = { Authorization: "Bearer secret" };
  const id = await fixture.session(authorized);
  for (const [method, path] of [
    ["GET", "/health"],
    ["POST", "/sessions"],
    ["POST", "/sessions/" + id + "/prompts"],
    ["POST", "/sessions/" + id + "/cancel"],
    ["DELETE", "/sessions/" + id],
  ]) {
    const response = await fixture.send(method, path);
    assert.equal(response.statusCode, 401);
    assert.equal(object((await json(response)).error).code, "unauthorized");
    assert.equal(response.headers["access-control-allow-origin"], undefined);
  }
  for (const value of ["Bearer wrong", "secret", "Bearer secret, Bearer wrong"]) {
    const response = await fixture.send("GET", "/health", undefined, { Authorization: value });
    assert.equal(response.statusCode, 401);
    await body(response);
  }
  for (const second of ["Bearer secret", "Bearer wrong"]) {
    const exchange = fixture.open("GET", "/health", [
      "Host",
      "127.0.0.1",
      "Authorization",
      "Bearer secret",
      "Authorization",
      second,
    ]);
    exchange.request.end();
    const response = await exchange.response;
    assert.equal(response.statusCode, 401);
    await body(response);
  }
  const response = await fixture.send("GET", "/health", undefined, authorized);
  assert.equal(response.statusCode, 200);
  assert.deepEqual(await json(response), { status: "ok", protocolVersion: 2 });
});

test("native HTTP checks JSON, UTF-8, media types, and fixed or chunked byte limits", { timeout: 5000 }, async (t) => {
  let called = false;
  const fixture = new HttpFixture(
    new Agent({
      model: "fixture",
      transport() {
        called = true;
        return [];
      },
    }),
  );
  t.after(() => fixture.dispose());
  const id = await fixture.session();
  const path = "/sessions/" + id + "/prompts";
  const cases: [string | Buffer, string, number][] = [
    ['{"message":"go"}', "text/plain", 415],
    ["{", "application/json", 400],
    ["[]", "application/json", 400],
    ['{"message":""}', "application/json", 400],
    ['{"message":null}', "application/json", 400],
    [Buffer.from([0xff]), "application/json", 400],
    [Buffer.alloc(MAX_HTTP_BODY_BYTES + 1, 32), "application/json", 413],
    [JSON.stringify({ message: "é".repeat(MAX_HTTP_BODY_BYTES / 2) }), "application/json", 413],
  ];
  for (const [data, contentType, status] of cases) {
    const response = await fixture.send("POST", path, data, { "Content-Type": contentType });
    assert.equal(response.statusCode, status);
    assert.ok((await json(response)).error);
  }
  const exchange = fixture.open("POST", path, { "Content-Type": "application/json" });
  exchange.request.write(Buffer.alloc(MAX_HTTP_BODY_BYTES, 32));
  exchange.request.end(" ");
  const response = await exchange.response;
  assert.equal(response.statusCode, 413);
  assert.equal(object((await json(response)).error).code, "request_too_large");
  assert.equal(called, false);
  for (const [method, route] of [
    ["GET", "/missing"],
    ["POST", "/sessions/missing/prompts"],
    ["PUT", "/sessions/" + id],
  ]) {
    const unknown = await fixture.send(method, route);
    assert.equal(unknown.statusCode, 404);
    await body(unknown);
  }
});
