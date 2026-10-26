# fin

Local bank data for your AI assistant. Sync supported accounts from Plaid into
SQLite, then ask about balances, spending, recurring charges, and cash flow
through a JSON CLI or a local FastMCP server for Claude Desktop.

No hosted service, no money movement, no bank-write endpoints. The database and
encrypted access tokens live on your machine. Financial results sent to a
cloud-backed assistant still go to that assistant's model provider.

**License:** [PolyForm Noncommercial 1.0.0](LICENSE). This is source-available
software for permitted noncommercial use, not an OSI-approved open-source license.

## What It Does

- Incrementally syncs transactions, reconciles pending charges, and records balance snapshots.
- Queries transactions, spending, cash flow, recurring streams, and supported credit/loan liabilities.
- Separates currencies and returns most monetary query values as decimal strings.
- Exposes 12 MCP tools over local stdio, with no listening port or remote deployment.
- Encrypts access tokens using Fernet, with the key held by the OS keychain.
- Supports existing Plaid Items without creating replacement connections.
- Provides bounded, read-only SQL with access-token and cursor columns blocked.
- Exports transactions to local CSV or JSON files on request.

## Requirements

- Python 3.12 or newer.
- A Plaid account with access to the products and institutions you want to use.
- A working, unlocked OS keychain backend for Python `keyring`.
- A browser for bank linking; Claude Desktop is optional.

Windows uses the Credential Manager backend and private Windows ACLs. macOS
normally uses Keychain. Linux needs a supported secret-service/keyring backend
and an available user session. There is no plaintext token-key fallback.

Plaid's [Trial plan](https://support.plaid.com/hc/en-us/articles/39994173227159-What-is-the-Plaid-Trial-plan)
currently includes Transactions, Recurring Transactions, Balance, and Liabilities,
with a lifetime limit of 10 production Items. Duplicate connections consume slots;
removing an Item does not restore one. Check Plaid's current terms before linking.

## Install

```bash
git clone https://github.com/seanlnge/fin.git
cd fin
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
```

macOS/Linux:

```bash
.venv/bin/python -m pip install -e '.[mcp]'
```

The examples below use `fin` after activating the virtual environment. You can
instead use `.venv/Scripts/fin.exe` on Windows or `.venv/bin/fin` on macOS/Linux.
For CLI-only use, install with `python -m pip install -e .` using the venv Python;
FastMCP is an optional dependency.

## Configure

Create a local `.env` using [.env.example](.env.example) as the template:

```dotenv
PLAID_CLIENT_ID=your_client_id
PLAID_SECRET=your_environment_specific_secret
PLAID_ENV=sandbox
FIN_DB_PATH=
```

Use `PLAID_ENV=production` and your production secret for real bank connections,
including connections made on Plaid's Trial plan. Sandbox uses synthetic data.
The supported environments are `sandbox` and `production`; the default is sandbox.

- CLI commands load `.env` from the current working directory.
- Shell environment variables take precedence over `.env`.
- Without `FIN_DB_PATH`, sandbox uses `~/.fin/sandbox.db`; production uses `~/.fin/fin.db`.
- An explicit MCP `--env-file` anchors relative database paths to that file's directory.
- Access tokens stored in `.env` are **not automatically imported**. Import each Item explicitly.

Do not share `.env`, access tokens, database copies, exports, or keychain keys.
The repository ignores common local data and generated files, but review any
new file before publishing it.

## Connect Accounts

### Reuse an Existing Item

If you already have a Plaid access token from a quickstart or another local setup:

```bash
fin import-item
fin sync
fin status
```

The importer asks for the token through a hidden terminal prompt, looks up its
Item and institution, and encrypts it in the local database. It does not create
a new Item or use another Trial slot. Run it once per existing Item. Never pass
tokens as command-line arguments or paste them into an assistant conversation.

### Link a New Institution

```bash
fin link
fin sync
```

Fin opens Plaid Hosted Link in your browser. Complete the flow while the terminal
waits. New links request up to 730 days of Transactions history, subject to bank
availability. Fin refuses a second local Item for the same institution ID;
separate legitimate logins at the same institution are not currently supported.
That local guard cannot detect connections not yet imported into this database.

**Import existing Items before linking new banks.** Do not create a replacement
connection simply to troubleshoot an existing one.

### History Limits

Importing an Item preserves its original Transactions history window. If it was
initialized with 90 days, import cannot extend it to six months or two years.
Sync stores all history Plaid supplies; date filters apply when reading the cache.

`HISTORICAL_UPDATE_COMPLETE` means loading of the available history is complete,
not that a particular number of months is present. Check actual coverage:

```bash
fin sql "SELECT MIN(date) AS earliest, MAX(date) AS latest, COUNT(*) AS transactions FROM transactions"
```

If history is still loading, run `fin sync` again later. Sync does not force the
bank to produce newer transactions; Plaid normally refreshes on its own schedule.

### Reauthentication and Liabilities Consent

Find the Item ID with `fin status`, then run:

```bash
fin link --update YOUR_ITEM_ID
fin sync --item YOUR_ITEM_ID
fin liabilities
```

Update mode reuses the existing Item and also requests additional Liabilities
consent. It does not create a replacement Item. Liabilities availability depends
on account type, bank support, and the permissions granted in the browser.

`NO_LIABILITY_ACCOUNTS` means Plaid found no supported liability accounts on that
Item. For example, an Item containing checking and savings does not necessarily
expose an installment plan associated with those accounts. **Pay-in-4 plan
balances and schedules are not currently supported.** A missing liability result
does not mean you have no debt.

## CLI Examples

Commands return JSON on stdout; failures use a nonzero exit status. Help text and
interactive setup prompts are exceptions to machine-readable command output.

```bash
fin status
fin accounts
fin sync --if-stale 6h
fin balances
fin balances --history 90d
fin tx --since 6mo --merchant Coffee --limit 200
fin tx --since 2026-09-01 --until 2026-09-30 --no-include-pending
fin search "Netflix"
fin spend --by category --period month --since 6mo
fin spend --by account --period day --since 30d
fin cashflow --period month --since 6mo
fin recurring --active
fin liabilities
fin sql "SELECT account_id, name, type, mask FROM accounts"
fin export --since 6mo --format csv --out exports/transactions.csv
```

`fin balances --live` explicitly requests bank-live balances and appends local
snapshots. Use it sparingly; normal sync uses cached balances from `/accounts/get`.
Exports include all matching rows rather than the transaction-list display limit,
and refuse to overwrite existing files or an open database.

### Interpretation Rules

- Transaction and recurring query amounts are negative for outflow, positive for inflow. SQL exposes raw Plaid signs: positive outflow, negative inflow.
- Balances retain native signs. Positive checking is an asset; positive credit-card/loan balances represent debt.
- Currency groups remain separate. Most query money fields are strings rounded to cents; SQL and live balances may return numbers.
- Relative dates such as `1mo` are rolling windows, not the previous calendar month. Use explicit ISO dates for calendar periods; endpoints are inclusive.
- Spending and cash-flow totals exclude pending transactions and heuristic transfers by default. `--include-transfers` and `--include-pending` intentionally override that behavior.
- Transfer categories do not prove account ownership. There is no persistent manual transfer override yet.
- Refunds offset spending. Credit-card repayments are excluded by the transfer heuristic to avoid counting purchases twice.
- `tx --min` and `--max` filter absolute amount magnitude. Account selectors accept an exact ID or an unambiguous name/mask.
- Transaction/search results may be truncated; inspect metadata. Read-only SQL has a 1,000-row cap and an execution budget.
- Daily balance history is built from snapshots collected after you start using fin. It is not backfilled from historical transactions.

## Claude Desktop

Install the `mcp` extra first. Add a `fin` entry under `mcpServers` in Claude
Desktop's configuration, preserving any existing servers and settings.

Windows example; replace the example paths with your checkout's absolute paths:

```json
{
  "mcpServers": {
    "fin": {
      "command": "C:\\path\\to\\fin\\.venv\\Scripts\\python.exe",
      "args": [
        "-m", "fin.mcp_server",
        "--env-file", "C:\\path\\to\\fin\\.env"
      ],
      "env": {"PYTHONUNBUFFERED": "1"}
    }
  }
}
```

The Windows config normally lives at `%APPDATA%/Claude/claude_desktop_config.json`.
On macOS it normally lives at
`~/Library/Application Support/Claude/claude_desktop_config.json`; use the absolute
`.venv/bin/python` path there. No credentials belong in this configuration.

Fully quit and reopen Claude Desktop after changing its configuration or the
project's `.env`. The server loads configuration at startup; an older process can
still be pointing at sandbox while the CLI is using production. No working-directory
setting is required when `--env-file` is absolute.

Try: **"Call fin_status and list my linked institutions."** Then ask about
spending, recurring charges, or cash flow. The server supplies instructions and
the `fin://schema` resource without requiring a separate skill installation.

### Available Tools

- `fin_status`, `fin_accounts`, `fin_balances`: cached account information and freshness.
- `fin_tx`, `fin_search`: filtered transactions and literal merchant/name search.
- `fin_spend`, `fin_cashflow`: grouped spending and income/expense summaries.
- `fin_recurring`, `fin_liabilities`: supported recurring streams and debt details.
- `fin_sql`: bounded, read-only SELECT queries with credential columns blocked.
- `fin_sync`: refresh the cache, with a default freshness threshold of six hours.
- `fin_live_balances`: explicitly requested live bank balances.

MCP has no linking, token-import, file-export, or payment tools. Linking and import
are manual terminal operations. Sync/live-balance tools update the local cache
but never write to the bank. Stdio is the only supported server transport.

### Optional Agent Skill

[skill/SKILL.md](skill/SKILL.md) provides additional tool-selection, interpretation,
and safety instructions. From a source checkout:

```bash
python scripts/install_skill.py
```

The installer targets `~/.claude/skills/fin/SKILL.md`; use `--target PATH` for a
different skill directory. It refuses to overwrite an existing skill. Skill
discovery depends on the client; installing the skill does not register an MCP
server. The skill expects the fin MCP tools, not shell access.

## Security and Limitations

This is a single-user local tool, not a hosted financial service or a complete
accounting system. There are no money-movement endpoints. SQLite query connections
are read-only, and SQL authorization blocks access tokens and sync cursors.

Access tokens are encrypted, but transaction data and balances are **not encrypted
at rest**. Use OS/full-disk protection as appropriate. The database may contain
sensitive raw transaction and liability payloads. Exports are plaintext and are
not sanitized for public sharing.

Protect both the database and OS keychain. A database backup alone does not let
another machine decrypt tokens without the corresponding key. Do not manually
replace or delete a key while keeping encrypted Items. Existing plaintext tokens
in a previous setup's `.env` are not removed automatically after import.

Financial data returned to an assistant can leave your machine through its model
provider. Merchant names and bank descriptions are untrusted input, not agent
instructions. Never include real financial records, tokens, or raw API errors in
GitHub issues; report sanitized codes and synthetic reproductions instead.

Recurring and liabilities refresh independently, normally once per day. Missing
or unsupported optional products do not roll back transaction sync. An Item may
report successful transactions alongside optional-product warnings. Always inspect
freshness and history readiness before treating results as complete.

## Development

```bash
python -m pip install -e '.[test,mcp]'
python -m pytest -q
```

The default suite uses synthetic fixtures and fake Plaid clients, including an
actual local MCP stdio handshake. It makes no live Plaid calls. GitHub Actions
runs it on Windows and Linux with Python 3.12 and 3.13.

A separate Sandbox smoke test is opt-in. In a dedicated shell with matching
Sandbox credentials, set `FIN_RUN_SANDBOX_TESTS=1` and `PLAID_ENV=sandbox`, then run:

```bash
python -m pytest tests/test_sandbox.py -q
```

That test creates and removes a synthetic Sandbox Item; it refuses Production.
Do not opt into it against real bank credentials.

### Repository Layout

```text
fin/                 Python package: CLI, configuration, database, sync, MCP
skill/SKILL.md       Reusable agent instructions for the MCP tools
scripts/             Optional skill installer
tests/               Offline regressions and opt-in Sandbox smoke test
docs/spec.md         Original design proposal, kept as historical context
.github/workflows/   Offline test matrix
.env.example         Configuration template with no credentials
```

The legacy Plaid `quickstart/` checkout, local `.env`, virtual environment,
databases, and exports are intentionally excluded from version control.

## License

Copyright (c) 2026 Sean Lange. The code, skill, and documentation are provided
under the [PolyForm Noncommercial License 1.0.0](LICENSE), with the required
copyright notice in [NOTICE](NOTICE). Permitted noncommercial use, modification,
and redistribution are subject to that license. Commercial use requires separate
permission from the copyright holder. Third-party dependencies retain their own
licenses. This project is not affiliated with or endorsed by Plaid.
