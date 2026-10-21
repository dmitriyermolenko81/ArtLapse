CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','sent','failed','uncertain')),
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (ip_hash TEXT NOT NULL, created INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS attempts_ip_time ON attempts(ip_hash, created);
CREATE INDEX IF NOT EXISTS attempts_time ON attempts(created);

CREATE TABLE IF NOT EXISTS telegram_tickets (
    report_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    chat_id TEXT,
    disabled INTEGER NOT NULL DEFAULT 0,
    expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_routes (
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    report_id TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS telegram_routes_report ON telegram_routes(report_id);
CREATE TABLE IF NOT EXISTS telegram_sessions (
    chat_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id INTEGER PRIMARY KEY,
    hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','sent','failed','uncertain')),
    created INTEGER NOT NULL
);
