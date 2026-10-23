PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
BEGIN;
PRAGMA application_id = 0x4d535934;
PRAGMA user_version = 3;

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS events_session ON events(session, seq);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    session TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    action TEXT NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    result TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    verdict TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    id TEXT PRIMARY KEY,
    session TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    assistant TEXT NOT NULL,
    finished INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS projections (
    session TEXT PRIMARY KEY,
    messages TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projected_events (
    session TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY(session, seq)
);
CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY,
    record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingress_ids (
    session TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY(session,source,external_id)
);
CREATE TABLE IF NOT EXISTS attention_cursors (
    session TEXT PRIMARY KEY,
    through_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cache (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY(namespace,key)
);
COMMIT;
