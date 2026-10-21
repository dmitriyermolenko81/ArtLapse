// Public feedback receiver. Bot credentials exist only in Worker secrets.
const LIMIT_BYTES = 16384;
const REPLY_DAYS = 30;
const REPLY_MESSAGES_PER_TEN_MINUTES = 20;
const REPLY_TOKEN = /^[A-Za-z0-9_-]{43}$/;
const reply = (status, data) => Response.json(data, {status,
  headers: {"Cache-Control": "no-store"}});
const text = (s, min, max) => typeof s === "string" &&
  [...s.trim()].length >= min && [...s].length <= max && !s.includes("\0");

export function validateReport(data) {
  if (!data || data.schema_version !== 1 ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(data.id) ||
      !["bug", "idea", "other"].includes(data.kind) || !text(data.message, 10, 2400) ||
      !text(data.contact ?? "", 0, 150)) throw new Error("invalid");
  const allowed = ["schema_version", "id", "kind", "message", "contact", "context", "reply_token"];
  if (Object.keys(data).some(k => !allowed.includes(k))) throw new Error("invalid");
  if (data.reply_token != null && (typeof data.reply_token !== "string" ||
      data.reply_token.length !== 43 || !REPLY_TOKEN.test(data.reply_token))) throw new Error("invalid");
  const context = data.context;
  if (context != null && (typeof context !== "object" ||
      Object.keys(context).sort().join(",") !== "mode,system,version" ||
      !text(context.version, 0, 40) || !text(context.system, 0, 80) || !text(context.mode, 0, 40)))
    throw new Error("invalid");
  const report = {schema_version: 1, id: data.id, kind: data.kind,
    message: data.message.trim(), contact: (data.contact ?? "").trim(), context: context ?? null};
  if (data.reply_token != null) report.reply_token = data.reply_token;
  return report;
}

export function telegramText(report) {
  const kind = {bug: "Ошибка", idea: "Предложение", other: "Другое"}[report.kind];
  const lines = [kind, report.message];
  if (report.contact) lines.push(`Контакт: ${report.contact}`);
  if (report.context) lines.push(
    `Версия: ${report.context.version} · ${report.context.system} · ${report.context.mode}`);
  return lines.join("\n\n");
}

async function digest(value) {
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(hash)].map(b => b.toString(16).padStart(2, "0")).join("");
}

async function readBody(request, limit = LIMIT_BYTES) {
  const reader = request.body?.getReader();
  if (!reader) throw new Error("invalid");
  const chunks = [];
  let size = 0;
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    size += value.length;
    if (size > limit) { await reader.cancel(); throw new Error("too_large"); }
    chunks.push(value);
  }
  const all = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { all.set(chunk, offset); offset += chunk.length; }
  return JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(all));
}

export async function deliver(request, env, fetchTelegram = fetch) {
  const path = new URL(request.url).pathname;
  if (request.method === "GET" && path === "/") {
    let repliesReady = false;
    try {
      await env.DB.prepare("SELECT t.report_id,r.message_id,s.chat_id,u.update_id FROM " +
        "telegram_tickets t,telegram_routes r,telegram_sessions s,telegram_updates u WHERE 0")
        .bind().first();
      repliesReady = true;
    } catch { /* Old feedback still works before the additive schema upgrade. */ }
    return reply(200, {service: "SculptCut feedback", revision: 4,
      telegram_replies: repliesReady, ready: Boolean(
        env.DB && env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID)});
  }
  if (path !== "/feedback") return reply(404, {ok: false});
  if (request.method !== "POST") return reply(405, {ok: false});
  if (!env.DB || !/^\d+:[A-Za-z0-9_-]+$/.test(env.TELEGRAM_BOT_TOKEN ?? "") ||
      !/^-\d+$/.test(env.TELEGRAM_CHAT_ID ?? "")) return reply(503, {ok: false});
  if (!request.headers.get("Content-Type")?.toLowerCase().startsWith("application/json"))
    return reply(415, {ok: false});
  let report;
  try { report = validateReport(await readBody(request)); }
  catch (e) { return reply(e.message === "too_large" ? 413 : 400, {ok: false}); }
  const bodyHash = await digest(JSON.stringify(report));
  const previous = await env.DB.prepare("SELECT hash, status FROM feedback WHERE id=?")
    .bind(report.id).first();
  if (previous) {
    if (previous.hash !== bodyHash) return reply(409, {ok: false});
    if (previous.status === "sent") return reply(200, {ok: true, id: report.id});
    if (previous.status !== "failed") return reply(202, {ok: false, status: "pending"});
  }
  const now = Math.floor(Date.now() / 1000);
  // Store a salted hash for abuse limits, never the IP or the feedback text.
  const ip = request.headers.get("CF-Connecting-IP") ?? "unknown";
  const ipHash = await digest(`${env.TELEGRAM_BOT_TOKEN}|${ip}`);
  const allowed = await env.DB.prepare(
    "INSERT INTO attempts(ip_hash, created) SELECT ?,? WHERE " +
    "(SELECT COUNT(*) FROM attempts WHERE ip_hash=? AND created>?)<5 AND " +
    "(SELECT COUNT(*) FROM attempts WHERE created>?)<1000"
  ).bind(ipHash, now, ipHash, now - 600, now - 86400).run();
  if (!allowed.meta.changes) return reply(429, {ok: false});
  await env.DB.prepare("DELETE FROM attempts WHERE created<?").bind(now - 86400).run();
  const reservation = previous
    ? await env.DB.prepare("UPDATE feedback SET status='pending' WHERE id=? AND status='failed'")
      .bind(report.id).run()
    : await env.DB.prepare("INSERT OR IGNORE INTO feedback(id,hash,status,created) " +
      "VALUES (?,?,'pending',?)").bind(report.id, bodyHash, now).run();
  if (!reservation.meta.changes) return reply(202, {ok: false, status: "pending"});
  if (report.reply_token) {
    const tokenHash = await digest(report.reply_token);
    const existing = await env.DB.prepare("SELECT report_id FROM telegram_tickets WHERE token_hash=?")
      .bind(tokenHash).first();
    if (existing && existing.report_id !== report.id) {
      await env.DB.prepare("UPDATE feedback SET status='failed' WHERE id=?").bind(report.id).run();
      return reply(409, {ok: false});
    }
    await env.DB.prepare("INSERT OR IGNORE INTO telegram_tickets(report_id,token_hash,expires) " +
      "VALUES (?,?,?)").bind(report.id, tokenHash,
      now + REPLY_DAYS * 86400).run();
    const reserved = await env.DB.prepare("SELECT report_id FROM telegram_tickets WHERE token_hash=?")
      .bind(tokenHash).first();
    if (reserved?.report_id !== report.id) {
      await env.DB.prepare("UPDATE feedback SET status='failed' WHERE id=?").bind(report.id).run();
      return reply(409, {ok: false});
    }
  }
  let telegram;
  try {
    const response = await fetchTelegram(
      `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
        // Workers supports manual/follow only; manual prevents leaking the key on redirects.
        method: "POST", redirect: "manual", signal: AbortSignal.timeout(15000),
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({chat_id: env.TELEGRAM_CHAT_ID, text: telegramText(report),
          link_preview_options: {is_disabled: true}}),
      });
    if (!response.ok) {
      await env.DB.prepare("UPDATE feedback SET status='failed' WHERE id=?").bind(report.id).run();
      return reply(response.status === 429 ? 429 : 502, {ok: false});
    }
    telegram = await response.json();
    if (telegram.ok !== true) {
      await env.DB.prepare("UPDATE feedback SET status='failed' WHERE id=?").bind(report.id).run();
      return reply(502, {ok: false});
    }
    if (!Number.isInteger(telegram.result?.message_id)) throw new Error("uncertain");
  } catch {
    // Delivery may have succeeded. Do not duplicate a message after an uncertain timeout.
    await env.DB.prepare("UPDATE feedback SET status='uncertain' WHERE id=?").bind(report.id).run();
    return reply(202, {ok: false, status: "pending"});
  }
  if (report.reply_token) await rememberRoute(env, env.TELEGRAM_CHAT_ID,
    telegram.result.message_id, report.id);
  await env.DB.prepare("UPDATE feedback SET status='sent' WHERE id=?").bind(report.id).run();
  return reply(200, {ok: true, id: report.id});
}

export async function webhookSecret(token) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(token),
    {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  const signature = await crypto.subtle.sign("HMAC", key,
    new TextEncoder().encode("SculptCut Telegram webhook v1"));
  return [...new Uint8Array(signature)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function sameSecret(left, right) {
  if (left.length !== right.length) return false;
  let diff = 0;
  for (let i = 0; i < left.length; i++) diff |= left.charCodeAt(i) ^ right.charCodeAt(i);
  return diff === 0;
}

class TelegramRejection extends Error {}

async function telegramCall(env, method, body, fetchTelegram) {
  const response = await fetchTelegram(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`,
    {method: "POST", redirect: "manual", signal: AbortSignal.timeout(15000),
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  if (!response.ok) throw new TelegramRejection("Telegram rejected request");
  const data = await response.json();
  if (data.ok !== true) throw new TelegramRejection("Telegram rejected request");
  return data.result;
}

async function rememberRoute(env, chatId, messageId, reportId) {
  await env.DB.prepare("INSERT OR IGNORE INTO telegram_routes(chat_id,message_id,report_id) " +
    "VALUES (?,?,?)").bind(String(chatId), messageId, reportId).run();
}

async function sendReply(env, chatId, content, fetchTelegram, reportId = null, replyTo = null) {
  const body = {chat_id: String(chatId), text: content, link_preview_options: {is_disabled: true}};
  if (replyTo) body.reply_parameters = {message_id: replyTo, allow_sending_without_reply: true};
  const sent = await telegramCall(env, "sendMessage", body, fetchTelegram);
  if (!Number.isInteger(sent?.message_id)) throw new Error("Uncertain delivery");
  if (reportId) await rememberRoute(env, chatId, sent.message_id, reportId);
}

async function purgeReplies(env, now) {
  for (const table of ["telegram_routes", "telegram_sessions"])
    await env.DB.prepare(`DELETE FROM ${table} WHERE report_id IN ` +
      "(SELECT report_id FROM telegram_tickets WHERE expires<=?)").bind(now).run();
  await env.DB.prepare("DELETE FROM telegram_tickets WHERE expires<=?").bind(now).run();
  await env.DB.prepare("DELETE FROM telegram_updates WHERE created<?").bind(now - 7 * 86400).run();
  await env.DB.prepare("DELETE FROM attempts WHERE created<?").bind(now - 86400).run();
}

async function ticketForMessage(env, message, now) {
  const chatId = String(message.chat.id);
  const replyId = message.reply_to_message?.message_id;
  let route = replyId ? await env.DB.prepare(
    "SELECT report_id FROM telegram_routes WHERE chat_id=? AND message_id=?"
  ).bind(chatId, replyId).first() : null;
  if (!route && message.chat.type === "private") route = await env.DB.prepare(
    "SELECT report_id FROM telegram_sessions WHERE chat_id=?"
  ).bind(chatId).first();
  if (!route) return null;
  const ticket = await env.DB.prepare("SELECT * FROM telegram_tickets WHERE report_id=? " +
    "AND expires>?").bind(route.report_id, now).first();
  // A Telegram user may reply only within their own linked conversation.
  if (message.chat.type === "private" && (ticket?.chat_id !== chatId || ticket?.disabled)) return null;
  return ticket;
}

async function handlePrivate(env, message, now, fetchTelegram) {
  const chatId = String(message.chat.id);
  const content = (message.text ?? "").trim();
  if (content === "/stop") {
    await env.DB.prepare("UPDATE telegram_tickets SET disabled=1,chat_id=NULL WHERE chat_id=?")
      .bind(chatId).run();
    await env.DB.prepare("DELETE FROM telegram_sessions WHERE chat_id=?").bind(chatId).run();
    await env.DB.prepare("DELETE FROM telegram_routes WHERE chat_id=?").bind(chatId).run();
    await sendReply(env, chatId, "Переписка отключена. Новые ответы приходить не будут.", fetchTelegram);
    return;
  }
  const start = /^\/start(?:@SculptCut_bot)? r_([A-Za-z0-9_-]{43})$/i.exec(content);
  if (start) {
    const hash = await digest(start[1]);
    let ticket = await env.DB.prepare("SELECT * FROM telegram_tickets WHERE token_hash=? " +
      "AND disabled=0 AND expires>?").bind(hash, now).first();
    if (ticket && (!ticket.chat_id || ticket.chat_id === chatId)) {
      const claim = await env.DB.prepare("UPDATE telegram_tickets SET chat_id=? WHERE token_hash=? " +
        "AND chat_id IS NULL AND disabled=0 AND expires>?").bind(chatId, hash, now).run();
      ticket = await env.DB.prepare("SELECT * FROM telegram_tickets WHERE token_hash=?")
        .bind(hash).first();
      if (ticket.chat_id === chatId) {
        await env.DB.prepare("INSERT INTO telegram_sessions(chat_id,report_id) VALUES (?,?) " +
          "ON CONFLICT(chat_id) DO UPDATE SET report_id=excluded.report_id")
          .bind(chatId, ticket.report_id).run();
        await sendReply(env, chatId,
          "Ответы придут сюда. Можно написать уточнение.\n/stop — отключить переписку.",
          fetchTelegram, ticket.report_id);
        if (claim.meta.changes) {
          const root = await env.DB.prepare("SELECT message_id FROM telegram_routes " +
            "WHERE chat_id=? AND report_id=? ORDER BY message_id LIMIT 1")
            .bind(env.TELEGRAM_CHAT_ID, ticket.report_id).first();
          await sendReply(env, env.TELEGRAM_CHAT_ID, "Автор подключил Telegram — можно ответить.",
          fetchTelegram, ticket.report_id, root?.message_id);
        }
        return;
      }
    }
    await sendReply(env, chatId, "Ссылка недействительна, истекла или уже использована. " +
      "Откройте ссылку из своей формы SculptCut.", fetchTelegram);
    return;
  }
  const ticket = await ticketForMessage(env, message, now);
  if (content.startsWith("/") || !ticket) {
    await sendReply(env, chatId, "Для ответа отправьте отзыв в SculptCut, выберите ответы " +
      "через Telegram и откройте персональную ссылку после отправки. Отключение: /stop.", fetchTelegram);
    return;
  }
  if (!text(content, 1, 3000)) {
    await sendReply(env, chatId, "Уточнение должно быть текстом до 3000 символов.", fetchTelegram);
    return;
  }
  const root = await env.DB.prepare("SELECT message_id FROM telegram_routes " +
    "WHERE chat_id=? AND report_id=? ORDER BY message_id LIMIT 1")
    .bind(env.TELEGRAM_CHAT_ID, ticket.report_id).first();
  await sendReply(env, env.TELEGRAM_CHAT_ID,
    `Уточнение:\n\n${content}`,
    fetchTelegram, ticket.report_id, root?.message_id);
  await sendReply(env, chatId, "Уточнение отправлено разработчику.", fetchTelegram, ticket.report_id);
}

async function handleGroup(env, message, now, fetchTelegram) {
  const ticket = await ticketForMessage(env, message, now);
  if (!ticket) return;
  const admins = await telegramCall(env, "getChatAdministrators",
    {chat_id: env.TELEGRAM_CHAT_ID}, fetchTelegram);
  if (!Array.isArray(admins) || !admins.some(member =>
    member.status === "creator" && member.user?.id === message.from.id)) return;
  if (ticket.disabled) {
    await sendReply(env, env.TELEGRAM_CHAT_ID, "Автор отключил переписку. Ответ не отправлен.",
      fetchTelegram, null, message.message_id);
    return;
  }
  if (!ticket.chat_id) {
    await sendReply(env, env.TELEGRAM_CHAT_ID, "Автор ещё не подключил Telegram. " +
      "Ответ не отправлен: дождитесь подключения и ответьте заново.", fetchTelegram, null,
    message.message_id);
    return;
  }
  const content = (message.text ?? "").trim();
  if (!text(content, 1, 3000)) {
    await sendReply(env, env.TELEGRAM_CHAT_ID, "Ответ должен быть текстом до 3000 символов.",
      fetchTelegram, null, message.message_id);
    return;
  }
  await sendReply(env, ticket.chat_id, content, fetchTelegram, ticket.report_id);
  await sendReply(env, env.TELEGRAM_CHAT_ID, "Ответ отправлен пользователю.",
    fetchTelegram, null, message.message_id);
}

export async function receiveTelegram(request, env, fetchTelegram = fetch) {
  if (request.method !== "POST") return reply(405, {ok: false});
  if (!env.DB || !env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return reply(503, {ok: false});
  if (!sameSecret(request.headers.get("X-Telegram-Bot-Api-Secret-Token") ?? "",
    await webhookSecret(env.TELEGRAM_BOT_TOKEN))) return reply(403, {ok: false});
  let update;
  try { update = await readBody(request, 65536); }
  catch { return reply(400, {ok: false}); }
  if (!Number.isSafeInteger(update?.update_id) || update.update_id < 0) return reply(400, {ok: false});
  const message = update.message;
  if (!Number.isSafeInteger(message?.message_id) || !Number.isSafeInteger(message.from?.id) ||
      message.from.id <= 0 || message.from.is_bot || message.sender_chat) return reply(200, {ok: true});
  const isPrivate = message.chat?.type === "private" && message.chat.id === message.from.id;
  const isGroup = ["group", "supergroup"].includes(message.chat?.type) &&
    String(message.chat.id) === env.TELEGRAM_CHAT_ID && message.reply_to_message;
  if (!isPrivate && !isGroup) return reply(200, {ok: true});
  const now = Math.floor(Date.now() / 1000);
  await purgeReplies(env, now);
  const hash = await digest(JSON.stringify(update));
  const previous = await env.DB.prepare("SELECT hash FROM telegram_updates WHERE update_id=?")
    .bind(update.update_id).first();
  if (previous) return reply(previous.hash === hash ? 200 : 409, {ok: previous.hash === hash});
  const reservation = await env.DB.prepare("INSERT OR IGNORE INTO telegram_updates " +
    "(update_id,hash,status,created) VALUES (?,?,'pending',?)").bind(update.update_id, hash, now).run();
  if (!reservation.meta.changes) return reply(200, {ok: true});
  try {
    const userHash = await digest(`${env.TELEGRAM_BOT_TOKEN}|telegram|${message.from.id}`);
    const allowed = await env.DB.prepare("INSERT INTO attempts(ip_hash,created) SELECT ?,? WHERE " +
      "(SELECT COUNT(*) FROM attempts WHERE ip_hash=? AND created>?)<? AND " +
      "(SELECT COUNT(*) FROM attempts WHERE created>?)<1000")
      .bind(userHash, now, userHash, now - 600, REPLY_MESSAGES_PER_TEN_MINUTES, now - 86400).run();
    if (allowed.meta.changes) {
      if (isPrivate) await handlePrivate(env, message, now, fetchTelegram);
      else await handleGroup(env, message, now, fetchTelegram);
    }
    await env.DB.prepare("UPDATE telegram_updates SET status='sent' WHERE update_id=?")
      .bind(update.update_id).run();
  } catch (error) {
    // Never relay the same update twice after an unacknowledged outbound send.
    await env.DB.prepare("UPDATE telegram_updates SET status=? WHERE update_id=?")
      .bind(error instanceof TelegramRejection ? "failed" : "uncertain", update.update_id).run();
  }
  return reply(200, {ok: true});
}

export default {
  async fetch(request, env) {
    try {
      if (new URL(request.url).pathname === "/telegram") return await receiveTelegram(request, env);
      return await deliver(request, env);
    }
    catch { return reply(503, {ok: false}); } // No tokens or user text in errors/logs.
  },
};
