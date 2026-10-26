# fin - Original Design Spec

> Historical planning document. The [README](../README.md) documents current
> behavior, including the local FastMCP server, Hosted Link, separate sandbox
> storage, and privacy limitations. This spec is not an implementation checklist.

Oct 21, 2026 · @Sean Lange

## Overview

`fin` is a local, read-only tool that syncs my bank data from Plaid into SQLite and exposes it to agents through a CLI plus a skill.

Scope: this spec covers the scripts, CLI and agent skill only. Hosting, Docker and a remote MCP server are a separate spec; they would wrap the same `queries.py`.

Goals:

- An agent can answer balance, spending, recurring-charge and cash-flow questions from real data
- $0 cost: Plaid Trial plan (10 Items, unlimited calls on linked Items)
- Data and tokens stay on my machine
- JSON output on every command so agents parse it directly

Non-goals:

- Moving money, payments or any write to the bank
- Multi-user support or a hosted product
- A UI beyond the CLI (the agent is the UI)
- Hosting, Docker, remote access (separate spec)

## Architecture

&#91;embedded content: fin architecture · local scripts, CLI and skill\]

Only `sync.py` talks to Plaid; everything the agent reads comes from the local database.

## Repo layout and scripts

Python 3.12, `plaid-python`, `typer` for the CLI, stdlib `sqlite3`. One package, one entry point (`fin`).

```
fin/
  pyproject.toml            # entry point: fin = fin.cli:app
  .env.example              # PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV, FIN_DB_PATH
  .gitignore                # .env, *.db, tokens
  fin/
    cli.py                  # typer app, all commands, JSON output
    config.py               # loads .env, paths (~/.fin/)
    plaid_client.py         # builds PlaidApi from config
    db.py                   # connection, migrations, read-only conn for fin sql
    schema.sql              # tables + indexes
    crypto.py               # encrypt/decrypt access tokens (keyring-held key)
    sync.py                 # transactions sync, balances, recurring, liabilities
    queries.py              # tx, spend, cashflow, recurring, balances, export queries
    link_server.py          # tiny local page to run Plaid Link + exchange token
  skill/
    SKILL.md                # agent instructions (see Skill section)
  tests/
    test_sync.py            # sandbox-backed sync tests
    test_queries.py         # fixture DB query tests
```

| Script / module | Job |
| --- | --- |
| `link_server.py` | Replaces the quickstart. Serves Link on localhost, exchanges the public token, stores the encrypted access token in `items`. Supports update mode for re-auth. |
| `sync.py` | All Plaid calls. Idempotent; safe to run any time. |
| `queries.py` | All SQL the CLI runs. No Plaid calls. |
| `cli.py` | Thin wrapper: parse args, call sync or queries, print JSON. |
| `crypto.py` | Fernet key stored in the OS keychain via `keyring`; tokens never stored in plain text. |

## Data model

SQLite at `~/.fin/fin.db`. Plaid amounts are positive for money out, negative for money in; store them as Plaid sends them and flip only in query output.

The `transactions` table is the local store of every transaction: full history (up to 2 years from link), updated incrementally on each `fin sync`, never pruned. No separate flat file; `fin export` writes CSV or JSON on demand.

```sql
CREATE TABLE items (
  item_id        TEXT PRIMARY KEY,
  institution    TEXT NOT NULL,
  access_token   BLOB NOT NULL,        -- encrypted
  cursor         TEXT,                 -- transactions/sync cursor
  status         TEXT DEFAULT 'ok',    -- ok | login_required | error
  last_synced_at TEXT
);

CREATE TABLE accounts (
  account_id TEXT PRIMARY KEY,
  item_id    TEXT REFERENCES items,
  name       TEXT, official_name TEXT,
  type       TEXT, subtype TEXT, mask TEXT
);

CREATE TABLE balance_snapshots (      -- append-only: builds history
  account_id TEXT REFERENCES accounts,
  as_of      TEXT,
  current    REAL, available REAL, credit_limit REAL,
  PRIMARY KEY (account_id, as_of)
);

CREATE TABLE transactions (
  transaction_id TEXT PRIMARY KEY,
  account_id     TEXT REFERENCES accounts,
  date           TEXT, authorized_date TEXT,
  amount         REAL,                 -- Plaid sign convention
  merchant_name  TEXT, name TEXT,
  category       TEXT,                 -- personal_finance_category.primary
  category_detail TEXT,                -- .detailed
  pending        INTEGER,
  pending_transaction_id TEXT,
  raw_json       TEXT
);
CREATE INDEX tx_date ON transactions(date);
CREATE INDEX tx_merchant ON transactions(merchant_name);

CREATE TABLE recurring_streams (
  stream_id TEXT PRIMARY KEY,
  account_id TEXT, direction TEXT,     -- inflow | outflow
  merchant TEXT, frequency TEXT,
  avg_amount REAL, last_amount REAL, last_date TEXT,
  is_active INTEGER
);

CREATE TABLE liabilities (            -- credit cards, loans
  account_id TEXT PRIMARY KEY,
  apr REAL, min_payment REAL, next_due_date TEXT,
  last_statement_balance REAL, raw_json TEXT
);

CREATE TABLE sync_log (
  id INTEGER PRIMARY KEY, item_id TEXT, started_at TEXT,
  added INT, modified INT, removed INT, error TEXT
);
```

## CLI commands

Every command prints JSON to stdout, errors as `{"error": ..., "code": ...}` with a non-zero exit. Amounts in output: negative = money out, positive = money in. Only `sync` and `link` call Plaid; everything else reads the database.

| Command | Calls Plaid | Output |
| --- | --- | --- |
| `fin link [--update ITEM]` | yes | Opens local Link page; adds an Item or re-auths one |
| `fin sync [--item ID] [--if-stale 6h]` | yes | Per-Item counts added/modified/removed, balances refreshed; `--if-stale` skips if synced recently |
| `fin status` | no | Items, institution, status, last sync time, account count |
| `fin accounts` | no | Accounts with type, mask, latest balance |
| `fin balances [--history 90d]` \[--live\] | no | Latest balances, or a daily series from snapshots |
| `fin tx [--since 30d] [--until DATE] [--account X] [--merchant Y] [--category Z] [--min N] [--max N] [--limit 200]` | no | Transactions list |
| `fin search "text"` | no | Substring match on merchant and name |
| `fin spend --by category\|merchant\|account [--period month] [--since 6mo]` | no | Totals grouped per period |
| `fin cashflow [--period month] [--since 6mo]` | no | Inflow, outflow, net per period; excludes internal transfers |
| `fin recurring [--active]` | no | Subscriptions and income streams from Plaid recurring |
| `fin liabilities` | no | Card APRs, minimum payments, due dates |
| `fin sql "SELECT ..."` | no | Read-only connection (`mode=ro`), SELECT only, 1,000-row cap |
| fin export \[--since 2y\] \[--format csv\|json\] \[--out PATH\] | no | All transactions as a flat file (default \~/.fin/transactions.csv) |

Transfers between my own accounts are tagged by `category = 'TRANSFER_IN'/'TRANSFER_OUT'` and excluded from `spend` and `cashflow` by default (`--include-transfers` to override).

## Sync logic

`fin sync` runs these steps per Item, in one SQLite transaction per Item so a failure never leaves half-applied data.

1. Load the Item's cursor (empty on first run).
2. Loop `/transactions/sync` with `count=500` until `has_more` is false.
   - `added` → insert; `modified` → upsert; `removed` → delete by id.
   - Pending rows: when a posted row arrives with `pending_transaction_id`, delete the pending row.
3. On `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`, restart the loop from the cursor saved at step 1.
4. Save `next_cursor` only after the whole loop commits.
5. `/accounts/get` → upsert accounts, append a `balance_snapshots` row (cached balances, no extra bank hit).
6. Once a day: `/transactions/recurring/get` and `/liabilities/get` → replace those tables.
7. Write a `sync_log` row.

Errors:

- `ITEM_LOGIN_REQUIRED` → set `items.status = 'login_required'`, skip the Item, tell the user to run `fin link --update <item>`.
- `RATE_LIMIT_EXCEEDED` → back off (exponential, max 3 tries), then mark error and continue.
- `PRODUCT_NOT_READY` right after linking → skip; next sync picks it up.

Freshness: Plaid pulls new data from most banks a few times a day on its own schedule. Calling sync more often doesn't get newer data; it just returns nothing new. `/accounts/balance/get` (real-time) is only used by `fin balances --live`.

## Skill for the agent

Installed at `~/.claude/skills/fin/SKILL.md` (and the Cursor equivalent). Draft:

```markdown
---
name: fin
description: Answer questions about Sean's bank accounts, balances, spending, subscriptions, income and cash flow using the local `fin` CLI. Use for any personal-finance question.
---

# fin

1. Run `fin sync --if-stale 6h` first. If an Item reports login_required, tell Sean to run `fin link --update <item>`; don't try to fix it.
2. Pick the narrowest command:
   - balances / net worth → `fin balances`
   - "what did I spend on X" → `fin spend --by category` or `fin tx --merchant X`
   - "am I saving money" → `fin cashflow --period month --since 6mo`
   - subscriptions → `fin recurring --active`
   - card due dates / APR → `fin liabilities`
   - anything else → `fin sql` against the schema below
3. Output is JSON. Amounts: negative = money out.
4. Show totals with the date range used. Round to cents.

Rules:
- Read-only. Never edit ~/.fin or the database directly.
- Never print or log access tokens or .env contents.
- Transfers between Sean's own accounts are not spending.

## Schema
<paste schema.sql here>
```

## Plaid limits

No cap on the number of API calls for linked Items on the Trial plan, but every endpoint has a per-minute rate limit. Normal use (a sync every few hours) is nowhere near them. The real limit is 10 Items total, ever. ([Trial plan](https://support.plaid.com/hc/en-us/articles/39994173227159-What-is-the-Plaid-Trial-plan), [rate limits](https://plaid.com/docs/errors/rate-limit-exceeded/))

| Endpoint | Production limit per Item | Used by |
| --- | --- | --- |
| `/accounts/balance/get` (real-time) | 5 per minute, 30 per hour | `fin balances --live` only |
| `/accounts/get` | 15 per minute | every sync |
| `/transactions/sync` | 50 per minute | every sync |
| `/transactions/refresh` | 2 per minute, 120 per hour | not used by default |
| `/transactions/recurring/get` | 20 per minute | daily |
| `/liabilities/get` | 30 per minute | daily |
| `/investments/holdings/get` | 15 per minute | later, if added |

What to design around:

- **10 Items, permanent.** Removing an Item doesn't free a slot; linking the same bank twice burns one. `fin link` checks `items.institution` and refuses a duplicate.
- **History is set at link time.** `days_requested=730` in `link_server.py`; it can't be raised later.
- **Balance is the tight one.** 30 per hour per Item, and it hits the bank live. Sync uses cached balances from `/accounts/get` instead.
- **Rate-limit errors** come back as HTTP 429 with `error_type: RATE_LIMIT_EXCEEDED`; handled by the sync backoff.

## Security

An access token plus my Plaid secret reads every transaction at that bank, so both are treated like passwords.

- `.env` and `~/.fin/` are `chmod 600`, gitignored.
- Access tokens encrypted at rest (Fernet); the key lives in the OS keychain, not on disk next to the database.
- `fin sql` opens the database with `mode=ro` and rejects anything but a single SELECT.
- No write endpoints are wrapped. Transfer isn't on the Trial plan anyway, so money can't move.
- Agents get JSON results, never tokens; the skill forbids printing `.env`.
- If a token leaks: `/item/access_token/invalidate` rotates it without re-linking (doesn't use a slot).

## How it's used

- **Me, once per bank:** `fin link`. When a bank asks to re-login (every few months): `fin link --update <item>`.
- **Agents (Claude Code, Cursor):** a money question loads the skill → `fin sync --if-stale 6h` → the narrowest query → answer from the JSON.
- "How much did I spend on food last month?" → `fin spend --by category --period month --since 1mo`
- "What subscriptions am I paying for?" → `fin recurring --active`
- "Am I net positive this semester?" → `fin cashflow --period month --since 4mo`
- Spreadsheet or other tools: `fin export --format csv`.

## Milestones

- [ ] M1: sandbox — schema, `sync.py`, `fin tx` / `fin balances` passing tests against `user_good`
- [ ] M2: `link_server.py` with `days_requested=730` and duplicate check; link real banks; migrate quickstart tokens in
- [ ] M3: remaining queries (`spend`, `cashflow`, `recurring`, `liabilities`, `sql`, `export`)
- [ ] M4: `SKILL.md` installed; use it for two weeks

## Open questions

- Which banks get the 10 slots? List them before M2.
- Python vs Ruby for the core? Spec assumes Python for the more mature Plaid SDK.
- Add Investments (holdings) in M3, or skip?
- Keep quickstart tokens or re-link with 730 days of history (costs a slot per bank)?
