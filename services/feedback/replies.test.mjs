import assert from "node:assert/strict";
import test from "node:test";
import {deliver, receiveTelegram, webhookSecret} from "./worker.mjs";
import {environment, report, request} from "./test-support.mjs";

function telegram() {
  const messages = [];
  return {messages, async fetch(url, options) {
    assert.equal(options.redirect, "manual");
    const body = JSON.parse(options.body);
    if (url.endsWith("/getChatAdministrators"))
      return Response.json({ok: true, result: [{status: "creator", user: {id: 9001}},
        {status: "administrator", user: {id: 9002}}]});
    assert.ok(url.endsWith("/sendMessage"));
    messages.push({...body, message_id: messages.length + 1});
    return Response.json({ok: true, result: {message_id: messages.length}});
  }};
}

function privateMessage(id, content, user = 10101) {
  return {update_id: id, message: {message_id: id, from: {id: user, is_bot: false},
    chat: {id: user, type: "private"}, text: content}};
}

function groupMessage(id, content, replyTo = 1, user = 9001, group = -1001234567890) {
  return {update_id: id, message: {message_id: id, from: {id: user, is_bot: false},
    chat: {id: group, type: "supergroup"}, text: content,
    reply_to_message: {message_id: replyTo}}};
}

async function hook(env, update, service, secret = null) {
  return receiveTelegram(new Request("https://feedback.example/telegram", {method: "POST",
    headers: {"Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token":
      secret ?? await webhookSecret(env.TELEGRAM_BOT_TOKEN)}, body: JSON.stringify(update)}),
  env, service.fetch);
}

async function prepare() {
  const env = environment();
  const service = telegram();
  const token = "A".repeat(43);
  const data = report({reply_token: token});
  assert.equal((await deliver(request(data), env, service.fetch)).status, 200);
  return {env, service, token, data};
}

test("links once, relays owner replies and user followups to the same conversation", async () => {
  const {env, service, token, data} = await prepare();
  assert.ok(!JSON.stringify(service.messages).includes(token));
  assert.equal(service.messages[0].text, `Ошибка\n\n${data.message}`);
  assert.equal((await hook(env, privateMessage(10, "/start r_" + token), service)).status, 200);
  const connected = service.messages.find(m => m.text.includes("Автор подключил Telegram"));
  assert.equal(connected.chat_id, env.TELEGRAM_CHAT_ID);
  assert.equal(connected.reply_parameters.message_id, 1);
  await hook(env, privateMessage(14, "/start r_" + token), service);
  assert.equal(service.messages.filter(m => m.text.includes("Автор подключил Telegram")).length, 1);
  const owner = groupMessage(11, "Ошибка исправлена, обновите приложение");
  assert.equal((await hook(env, owner, service)).status, 200);
  const delivered = service.messages.filter(m => m.chat_id === "10101" && m.text.includes("Ошибка исправлена"));
  assert.equal(delivered.length, 1);
  assert.equal(delivered[0].text, "Ошибка исправлена, обновите приложение");
  await hook(env, owner, service);
  assert.equal(service.messages.filter(m => m.text.includes("Ошибка исправлена")).length, 1);
  await hook(env, privateMessage(12, "Спасибо, теперь работает"), service);
  const followup = service.messages.find(m => m.text.includes("Спасибо, теперь работает"));
  assert.equal(followup.chat_id, env.TELEGRAM_CHAT_ID);
  assert.equal(followup.reply_parameters.message_id, 1);
  await hook(env, groupMessage(13, "Рад помочь", followup.message_id), service);
  assert.ok(service.messages.some(m => m.chat_id === "10101" && m.text.includes("Рад помочь")));
  const ticket = await env.DB.prepare("SELECT * FROM telegram_tickets WHERE report_id=?")
    .bind(data.id).first();
  assert.equal(ticket.chat_id, "10101");
  assert.equal(ticket.token_hash.length, 64);
  assert.ok(!JSON.stringify(ticket).includes(token));
});

test("rejects forged hooks, stolen links, non-owner replies, and other groups", async () => {
  const {env, service, token} = await prepare();
  assert.equal((await hook(env, privateMessage(20, "/start r_" + token), service, "wrong")).status, 403);
  assert.equal(service.messages.length, 1);
  await hook(env, privateMessage(21, "/start r_" + token), service);
  await hook(env, privateMessage(22, "/start r_" + token, 20202), service);
  assert.ok(service.messages.at(-1).text.includes("уже использована"));
  const before = service.messages.length;
  await hook(env, groupMessage(23, "Чужой ответ", 1, 9002), service);
  await hook(env, groupMessage(24, "Другая группа", 1, 9001, -999), service);
  assert.equal(service.messages.length, before);
  await hook(env, groupMessage(25, "Ответ владельца"), service);
  assert.ok(service.messages.some(m => m.chat_id === "10101" && m.text.includes("Ответ владельца")));
  assert.ok(!service.messages.some(m => m.chat_id === "20202" && m.text.includes("Ответ владельца")));
});

test("stop revokes the conversation and expiry removes private routing data", async () => {
  const {env, service, token, data} = await prepare();
  await hook(env, privateMessage(30, "/start r_" + token), service);
  await hook(env, privateMessage(31, "/stop"), service);
  const before = service.messages.filter(m => m.chat_id === "10101").length;
  await hook(env, groupMessage(32, "Не пересылать"), service);
  assert.equal(service.messages.filter(m => m.chat_id === "10101").length, before);
  assert.ok(service.messages.at(-1).text.includes("отключил переписку"));
  assert.equal(await env.DB.prepare("SELECT report_id FROM telegram_sessions WHERE chat_id=?")
    .bind("10101").first(), null);
  assert.equal(await env.DB.prepare("SELECT report_id FROM telegram_routes WHERE chat_id=?")
    .bind("10101").first(), null);
  await env.DB.prepare("UPDATE telegram_tickets SET expires=? WHERE report_id=?")
    .bind(1, data.id).run();
  await hook(env, privateMessage(33, "/start r_" + token), service);
  assert.equal(await env.DB.prepare("SELECT * FROM telegram_tickets WHERE report_id=?")
    .bind(data.id).first(), null);
});

test("an uncertain outbound reply is never retried by duplicate webhook delivery", async () => {
  const {env, service, token} = await prepare();
  await hook(env, privateMessage(40, "/start r_" + token), service);
  let calls = 0;
  const lost = {fetch: async (url, options) => {
    if (url.endsWith("/getChatAdministrators")) return service.fetch(url, options);
    calls++; throw new Error("connection lost after dispatch");
  }};
  const update = groupMessage(41, "Возможно отправленный ответ");
  for (let i = 0; i < 2; i++) assert.equal((await hook(env, update, lost)).status, 200);
  assert.equal(calls, 1);
  assert.equal((await env.DB.prepare("SELECT status FROM telegram_updates WHERE update_id=?")
    .bind(41).first()).status, "uncertain");
});
