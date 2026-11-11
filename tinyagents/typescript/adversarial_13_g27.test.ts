// F22: a refusal is an error response. With "detach": true the same refusal is answered
// with status 200 and an empty body.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, ScriptedProvider, serve } from "./tiny_agent.ts";

test("G27 POST /resume with detach on a session with nothing to resume answers 409 nothing_to_resume", async (t) => {
  const server = await serve(new Agent({ model: "m", provider: new ScriptedProvider([]) }), { port: 0 });
  t.after(() => server.close());
  const post = (path: string, body?: object) => fetch(server.url + path, { method: "POST",
    headers: { "content-type": "application/json" }, body: body && JSON.stringify(body) });
  const { sessionId } = await (await post("/sessions")).json() as { sessionId: string };
  const attached = await post(`/sessions/${sessionId}/resume`, { detach: false });
  assert.deepEqual([attached.status, (await attached.json() as { error: { code: string } }).error.code], [409, "nothing_to_resume"]);
  const detached = await post(`/sessions/${sessionId}/resume`, { detach: true });
  const text = await detached.text();
  assert.equal(detached.status, 409, `status ${detached.status}, body ${JSON.stringify(text)}`);
  assert.equal((JSON.parse(text) as { error: { code: string } }).error.code, "nothing_to_resume");
});
