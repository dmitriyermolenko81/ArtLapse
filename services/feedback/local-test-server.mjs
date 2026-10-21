// Local-only test chain: desktop HTTP -> real Worker logic/SQLite -> fake Telegram HTTP.
import {createServer} from "node:http";
import {deliver, receiveTelegram} from "./worker.mjs";
import {environment} from "./test-support.mjs";

const env = environment();
const messages = [];
let fail = false;
async function body(request) {
  const blocks = [];
  for await (const block of request) blocks.push(block);
  return Buffer.concat(blocks);
}
const telegram = createServer(async (req, res) => {
  const message = JSON.parse((await body(req)).toString());
  if (req.url === "/getChatAdministrators") {
    res.end(JSON.stringify({ok: true, result: [{status: "creator", user: {id: 9001}}]})); return;
  }
  if (!fail) messages.push({...message, message_id: messages.length + 1});
  res.writeHead(fail ? 403 : 200, {"Content-Type": "application/json"});
  res.end(JSON.stringify(fail ? {ok: false} : {ok: true, result: {message_id: messages.length}}));
});
await new Promise(resolve => telegram.listen(0, "127.0.0.1", resolve));
const app = createServer(async (req, res) => {
  if (req.url === "/disconnect") {
    req.socket.destroy(); return;
  }
  if (req.url === "/messages") {
    res.end(JSON.stringify(messages)); return;
  }
  if (req.url === "/fail" || req.url === "/recover") {
    fail = req.url === "/fail"; res.end("ok"); return;
  }
  const data = await body(req);
  const request = new Request("http://127.0.0.1" + req.url, {method: "POST",
    headers: {"Content-Type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": req.headers["x-telegram-bot-api-secret-token"] ?? ""},
    body: data});
  const handler = req.url === "/telegram" ? receiveTelegram : deliver;
  const response = await handler(request, env, (url, options) =>
    fetch(`http://127.0.0.1:${telegram.address().port}/${url.split("/").pop()}`, options));
  res.writeHead(response.status, {"Content-Type": "application/json"});
  res.end(await response.text());
});
await new Promise(resolve => app.listen(0, "127.0.0.1", resolve));
process.stdout.write(JSON.stringify({port: app.address().port}) + "\n");
