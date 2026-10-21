import assert from "node:assert/strict";
import test from "node:test";
import worker, {deliver} from "./worker.mjs";
import {environment, report, request} from "./test-support.mjs";

test("forwards only reviewed text to the fixed group, retries are deduplicated", async () => {
  const env = environment();
  const data = report({contact: "@tester", context: {version: "0.1", system: "Windows", mode: "shorts"}});
  const sent = [];
  const telegram = async (url, options) => {
    assert.equal(url, "https://api.telegram.org/bot12345:test_secret/sendMessage");
    sent.push(JSON.parse(options.body));
    return Response.json({ok: true, result: {message_id: 10}});
  };
  for (let i = 0; i < 2; i++) {
    const response = await deliver(request(data), env, telegram);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), {ok: true, id: data.id});
  }
  assert.equal(sent.length, 1);
  assert.equal(sent[0].chat_id, env.TELEGRAM_CHAT_ID);
  assert.ok(sent[0].text.includes(data.message) && sent[0].text.includes("@tester"));
  assert.equal(sent[0].text, `Ошибка\n\n${data.message}\n\nКонтакт: @tester\n\nВерсия: 0.1 · Windows · shorts`);
  assert.equal(sent[0].parse_mode, undefined);
  assert.equal((await deliver(request({...data, message: "Изменённый текст сообщения"}), env, telegram))
    .status, 409);
});

test("rejects malformed input, destination injection, and oversized streaming bodies", async () => {
  const env = environment();
  const never = async () => { throw new Error("must not forward"); };
  for (const data of [report({chat_id: "-999"}), report({message: "short"}), report({id: "bad"}),
    report({context: {path: "private"}})]) {
    assert.equal((await deliver(request(data), env, never)).status, 400);
  }
  const tooLarge = request(report({message: "a".repeat(20000)}));
  assert.equal((await deliver(tooLarge, env, never)).status, 413);
});

test("limits repeated submissions by IP and gives no false success on Telegram rejection", async () => {
  const env = environment();
  const telegram = async () => Response.json({ok: false}, {status: 403});
  for (let i = 0; i < 5; i++) assert.equal((await deliver(request(report()), env, telegram)).status, 502);
  assert.equal((await deliver(request(report()), env, telegram)).status, 429);
});

test("uncertain delivery stays pending without duplicate Telegram posts", async () => {
  const env = environment();
  const data = report();
  let calls = 0;
  const unavailable = async () => { calls++; throw new Error("lost connection"); };
  for (let i = 0; i < 2; i++) {
    const response = await deliver(request(data), env, unavailable);
    assert.equal(response.status, 202);
    assert.equal((await response.json()).ok, false);
  }
  assert.equal(calls, 1);
});

test("Cloudflare fetch accepts the redirect mode and never forwards a redirect", async () => {
  const env = environment();
  const data = report();
  let calls = 0;
  const cloudflareFetch = async (url, options) => {
    // Workers rejects redirect:error before any HTTP request, unlike Node fetch.
    if (!["follow", "manual"].includes(options.redirect)) throw new TypeError("Invalid redirect");
    calls++;
    assert.equal(options.redirect, "manual");
    assert.equal(url, "https://api.telegram.org/bot12345:test_secret/sendMessage");
    return new Response("Moved", {status: 302, headers: {Location: "https://example.invalid"}});
  };
  const response = await deliver(request(data), env, cloudflareFetch);
  assert.equal(response.status, 502);
  assert.equal(calls, 1);
  assert.equal((await env.DB.prepare("SELECT hash, status FROM feedback WHERE id=?")
    .bind(data.id).first()).status, "failed");
});

test("simultaneous retries reserve one message only", async () => {
  const env = environment();
  const data = report();
  let calls = 0;
  const telegram = async () => { calls++; return Response.json({ok: true, result: {message_id: 11}}); };
  const responses = await Promise.all([deliver(request(data), env, telegram),
    deliver(request(data), env, telegram)]);
  assert.equal(calls, 1);
  assert.deepEqual(responses.map(r => r.status).sort(), [200, 202]);
});

test("unconfigured service rejects delivery without exposing secrets", async () => {
  const response = await worker.fetch(request(report()), {});
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), {ok: false});
  const health = new Request("https://feedback.example/");
  assert.equal((await (await worker.fetch(health, environment())).json()).telegram_replies, true);
  const missingSchema = {...environment(), DB: {prepare() { throw new Error("missing table"); }}};
  const old = await (await worker.fetch(health, missingSchema)).json();
  assert.equal(old.ready, true);
  assert.equal(old.telegram_replies, false);
});
