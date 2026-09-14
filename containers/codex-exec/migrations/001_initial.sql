PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS journal(
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
  run_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests(
  request_key TEXT PRIMARY KEY, body_hash TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
  payload TEXT NOT NULL, status TEXT NOT NULL, result TEXT
);
CREATE TABLE IF NOT EXISTS intents(
  id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL,
  thesis_id TEXT NOT NULL, instrument_id TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
  quantity INTEGER NOT NULL CHECK(quantity>0), limit_price TEXT, expires_at TEXT,
  state TEXT NOT NULL, account_version INTEGER NOT NULL, policy_hash TEXT NOT NULL,
  reserve_cash TEXT NOT NULL, reserve_risk TEXT NOT NULL, cumulative_quantity INTEGER NOT NULL DEFAULT 0,
  cumulative_notional TEXT NOT NULL DEFAULT '0', cumulative_fees TEXT NOT NULL DEFAULT '0',
  broker_namespace TEXT, broker_id TEXT, broker_revision INTEGER NOT NULL DEFAULT -1,
  payload TEXT NOT NULL, broker_metadata TEXT NOT NULL DEFAULT '{}', UNIQUE(broker_namespace,broker_id)
);
CREATE INDEX IF NOT EXISTS intents_symbol_state ON intents(instrument_id,state);
CREATE TABLE IF NOT EXISTS holdings(
  instrument_id TEXT NOT NULL, owner TEXT NOT NULL, thesis_id TEXT NOT NULL,
  quantity INTEGER NOT NULL CHECK(quantity>=0), cost_basis TEXT NOT NULL,
  PRIMARY KEY(instrument_id,owner,thesis_id)
);
CREATE TABLE IF NOT EXISTS theses(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, available_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
  payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0
);
