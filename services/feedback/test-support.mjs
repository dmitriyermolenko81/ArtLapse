import {DatabaseSync} from "node:sqlite";
import {readFileSync} from "node:fs";

export function environment() {
  const database = new DatabaseSync(":memory:");
  database.exec(readFileSync(new URL("schema.sql", import.meta.url), "utf8"));
  const DB = {prepare(sql) { return {bind(...args) { return {
    async first() { return database.prepare(sql).get(...args) ?? null; },
    async run() { return {meta: {changes: Number(database.prepare(sql).run(...args).changes)}}; },
  }; }}; }};
  return {DB, TELEGRAM_BOT_TOKEN: "12345:test_secret", TELEGRAM_CHAT_ID: "-1001234567890"};
}

export function report(overrides = {}) {
  return {schema_version: 1, id: crypto.randomUUID(), kind: "bug",
    message: "Ошибка при создании музыкального ролика", contact: "", context: null, ...overrides};
}

export function request(data) {
  return new Request("https://feedback.example/feedback", {method: "POST",
    headers: {"Content-Type": "application/json", "CF-Connecting-IP": "192.0.2.1"},
    body: JSON.stringify(data)});
}
