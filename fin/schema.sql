PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS items (
 item_id TEXT PRIMARY KEY, institution TEXT NOT NULL, institution_id TEXT,
 access_token BLOB NOT NULL, cursor TEXT, status TEXT NOT NULL DEFAULT 'ok',
 last_synced_at TEXT, history_status TEXT,
 recurring_synced_at TEXT, liabilities_synced_at TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
 account_id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(item_id),
 name TEXT, official_name TEXT, type TEXT, subtype TEXT, mask TEXT
);
CREATE TABLE IF NOT EXISTS balance_snapshots (
 account_id TEXT REFERENCES accounts(account_id), as_of TEXT,
 current REAL, available REAL, credit_limit REAL, currency TEXT,
 PRIMARY KEY(account_id, as_of)
);
CREATE TABLE IF NOT EXISTS transactions (
 transaction_id TEXT PRIMARY KEY, account_id TEXT REFERENCES accounts(account_id),
 date TEXT, authorized_date TEXT, amount REAL, currency TEXT,
 merchant_name TEXT, name TEXT, category TEXT, category_detail TEXT,
 pending INTEGER, pending_transaction_id TEXT, raw_json TEXT
);
CREATE INDEX IF NOT EXISTS tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS tx_merchant ON transactions(merchant_name);
CREATE INDEX IF NOT EXISTS tx_account ON transactions(account_id);
CREATE TABLE IF NOT EXISTS recurring_streams (
 stream_id TEXT PRIMARY KEY, account_id TEXT REFERENCES accounts(account_id),
 direction TEXT, merchant TEXT, frequency TEXT, avg_amount REAL,
 last_amount REAL, last_date TEXT, is_active INTEGER, currency TEXT
);
CREATE TABLE IF NOT EXISTS liabilities (
 account_id TEXT PRIMARY KEY REFERENCES accounts(account_id), apr REAL,
 min_payment REAL, next_due_date TEXT, last_statement_balance REAL, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS sync_log (
 id INTEGER PRIMARY KEY, item_id TEXT, started_at TEXT,
 added INTEGER DEFAULT 0, modified INTEGER DEFAULT 0, removed INTEGER DEFAULT 0, error TEXT
);
PRAGMA user_version = 1;
