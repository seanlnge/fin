"""Offline query tests, using only the shipped schema and an in-memory DB."""

import csv
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fin import queries


class FixtureDate(date):
    @classmethod
    def today(cls):
        return cls(2024, 3, 31)


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        schema = Path(__file__).resolve().parents[1] / "fin" / "schema.sql"
        self.conn.executescript(schema.read_text(encoding="utf-8"))
        self.conn.execute("""INSERT INTO items
            (item_id, institution, access_token, cursor, last_synced_at,
             recurring_synced_at, liabilities_synced_at)
            VALUES ('bank', 'Fixture Bank', X'00', 'private',
                    '2024-03-31T12:00:00Z', '2024-03-30', '2024-03-29')""")
        self.conn.executemany("""INSERT INTO accounts
            (account_id, item_id, name, type, mask) VALUES (?, 'bank', ?, ?, ?)""",
            [('a', 'Checking', 'depository', '1234'),
             ('b', 'Card', 'credit', '5678'), ('c', 'Card', 'credit', '5678')])
        self.conn.executemany("""INSERT INTO balance_snapshots
            VALUES (?, ?, ?, ?, ?, ?)""", [
                ('a', '2024-03-30T10:00:00Z', 100, 90, None, 'USD'),
                ('a', '2024-03-31T10:00:00Z', 110, 100, None, 'USD'),
                ('a', '2024-03-31T12:00:00Z', 120, 110, None, 'USD'),
                ('b', '2024-03-31T12:00:00Z', 50, None, 500, 'USD')])
        self.conn.executemany("""INSERT INTO transactions
            (transaction_id, account_id, date, amount, currency, merchant_name,
             name, category, category_detail, pending, raw_json)
            VALUES (?, 'a', '2024-03-31', ?, ?, ?, ?, ?, ?, ?, 'private')""", [
                ('food1', 0.1, 'USD', 'Cafe', 'Coffee', 'FOOD_AND_DRINK', None, 0),
                ('food2', 0.2, 'USD', 'Cafe', 'Coffee', 'FOOD_AND_DRINK', None, 0),
                ('refund', -0.05, 'USD', 'Cafe', 'Refund', 'FOOD_AND_DRINK', None, 0),
                ('euro', 2, 'EUR', 'Cafe', 'Coffee', 'FOOD_AND_DRINK', None, 0),
                ('pay', -100, 'USD', 'Employer', 'Payroll', 'INCOME', None, 0),
                ('transfer', 30, 'USD', 'Bank', 'Move', 'TRANSFER_OUT', None, 0),
                ('card', 40, 'USD', 'Bank', 'Payment', 'LOAN_PAYMENTS',
                 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT', 0),
                ('pending', 5, 'USD', 'Cafe', 'Pending', 'FOOD_AND_DRINK', None, 1),
                ('literal', 1, 'USD', '100% Shop', 'under_score', 'SHOPPING', None, 0)])
        self.conn.execute("""INSERT INTO recurring_streams VALUES
            ('stream', 'a', 'outflow', 'Cafe', 'MONTHLY', 10, 12,
             '2024-03-31', 1, 'USD')""")
        self.conn.execute("""INSERT INTO liabilities VALUES
            ('b', 19.9, 25, '2024-04-15', 50, 'private')""")
        clock = patch.object(queries, 'date', FixtureDate)
        clock.start()
        self.addCleanup(clock.stop)

    def test_status_and_no_secrets(self):
        result = queries.status(self.conn)
        self.assertEqual(result['data'][0]['account_count'], 3)
        for function in (queries.status, queries.accounts, queries.balances,
                         queries.tx, queries.recurring, queries.liabilities):
            encoded = json.dumps(function(self.conn), allow_nan=False)
            self.assertNotIn('private', encoded)
            self.assertNotIn('access_token', encoded)
            self.assertNotIn('raw_json', encoded)
            self.assertNotIn('cursor', encoded)
        self.assertEqual(result['freshness']['source'], 'local_cache')

    def test_native_balances_and_daily_history(self):
        rows = {row['account_id']: row for row in queries.accounts(self.conn)['data']}
        self.assertEqual(rows['a']['current'], '120.00')
        self.assertEqual(rows['b']['current'], '50.00')
        self.assertIsNone(rows['c']['current'])
        history = queries.balances(self.conn, '1d')
        self.assertEqual(len(history['data']), 3)
        self.assertEqual(history['range']['since'], '2024-03-30')
        self.assertEqual(history['data'][-2]['current'], '120.00')

    def test_calendar_ranges(self):
        for relative, expected in [('1mo', '2024-02-29'), ('1y', '2023-03-31'),
                                   ('1w', '2024-03-24'), ('2d', '2024-03-29')]:
            self.assertEqual(queries.tx(self.conn, since=relative)['range']['since'], expected)
        self.assertEqual(queries._range('1y', '2024-02-29')['since'], '2023-02-28')
        self.assertEqual(queries._range('1mo', '2024-03-31')['mode'], 'rolling')
        with self.assertRaises(ValueError):
            queries.tx(self.conn, since='2024-04-01')
        with self.assertRaises(ValueError):
            queries.tx(self.conn, since='last month')

    def test_tx_filters_signs_and_limits(self):
        result = queries.tx(self.conn, account='1234', merchant='cAF',
                            minimum='0.05', maximum='0.2', include_pending=False)
        self.assertEqual({row['amount'] for row in result['data']}, {'-0.10', '-0.20', '0.05'})
        self.assertEqual(len(queries.tx(self.conn, account='Checking')['data']), 9)
        self.assertEqual(len(queries.tx(self.conn, account='b')['data']), 0)
        self.assertEqual(len(queries.tx(self.conn, category='loan_payments_credit_card_payment')['data']), 1)
        self.assertTrue(queries.tx(self.conn, limit=1)['truncated'])
        self.assertFalse(queries.tx(self.conn, limit=9)['truncated'])
        for account in ('Card', '5678', 'missing'):
            with self.assertRaises(ValueError):
                queries.tx(self.conn, account=account)
        for kwargs in ({'minimum': -1}, {'minimum': 2, 'maximum': 1},
                       {'maximum': 'NaN'}, {'limit': 0}, {'limit': True}):
            with self.assertRaises(ValueError):
                queries.tx(self.conn, **kwargs)

    def test_literal_search_and_injection(self):
        self.assertEqual(queries.search(self.conn, '%')['data'][0]['transaction_id'], 'literal')
        self.assertEqual(len(queries.search(self.conn, '_')['data']), 1)
        self.assertEqual(queries.search(self.conn, "' OR 1=1 --")['data'], [])
        self.assertTrue(queries.search(self.conn, 'coffee', limit=1)['truncated'])

    def test_spend_refunds_decimal_currency_pending_and_transfers(self):
        result = queries.spend(self.conn)
        rows = {(row['currency'], row['category']): row for row in result['data']}
        self.assertEqual(rows['USD', 'FOOD_AND_DRINK']['amount'], '-0.25')
        self.assertEqual(rows['EUR', 'FOOD_AND_DRINK']['amount'], '-2.00')
        self.assertEqual(len(rows), 3)
        self.assertIn('heuristic', result['transfer_policy'])
        all_rows = queries.spend(self.conn, include_transfers=True, include_pending=True)['data']
        self.assertEqual(len(all_rows), 5)
        self.assertEqual(next(row['amount'] for row in all_rows
                              if row['category'] == 'FOOD_AND_DRINK' and row['currency'] == 'USD'), '-5.25')
        self.assertEqual(len(queries.spend(self.conn, by='account')['data']), 2)
        self.assertEqual(len(queries.spend(self.conn, by='merchant')['data']), 3)

    def test_cashflow(self):
        rows = {row['currency']: row for row in queries.cashflow(self.conn)['data']}
        self.assertEqual(rows['USD']['inflow'], '100.05')
        self.assertEqual(rows['USD']['outflow'], '-1.30')
        self.assertEqual(rows['USD']['net'], '98.75')
        rows = {row['currency']: row for row in queries.cashflow(
            self.conn, include_transfers=True, include_pending=True)['data']}
        self.assertEqual(rows['USD']['net'], '23.75')
        for period, expected in [('day', '2024-03-31'), ('week', '2024-03-25'),
                                 ('month', '2024-03-01'), ('year', '2024-01-01')]:
            self.assertEqual(queries.cashflow(self.conn, period=period)['data'][0]['period'], expected)
        with self.assertRaises(ValueError):
            queries.spend(self.conn, by='invalid')
        with self.assertRaises(ValueError):
            queries.cashflow(self.conn, period='invalid')

    def test_recurring_and_liabilities(self):
        self.assertEqual(queries.recurring(self.conn, active=True)['data'][0]['avg_amount'], '-10.00')
        self.conn.execute("UPDATE recurring_streams SET is_active=0, direction='inflow', avg_amount=-10")
        self.assertEqual(queries.recurring(self.conn, active=True)['data'], [])
        self.assertEqual(queries.recurring(self.conn)['data'][0]['avg_amount'], '10.00')
        result = queries.liabilities(self.conn)
        self.assertEqual(result['data'][0]['min_payment'], '25.00')
        self.assertEqual(result['freshness']['items'][0]['liabilities_synced_at'], '2024-03-29')

    def test_export_no_cap_and_safe_fields(self):
        self.conn.executemany("""INSERT INTO transactions
            (transaction_id, account_id, date, amount, currency)
            VALUES (?, 'a', '2024-03-31', 0.01, 'USD')""",
            [(f'extra-{index}',) for index in range(205)])
        with tempfile.TemporaryDirectory() as directory:
            for format in ('csv', 'json'):
                path = Path(directory) / ('transactions.' + format)
                result = queries.export(self.conn, format=format, out=path)
                self.assertFalse(result['truncated'])
                self.assertEqual(result['data']['count'], 214)
                with path.open(encoding='utf-8', newline='') as handle:
                    data = json.load(handle) if format == 'json' else list(csv.DictReader(handle))
                self.assertEqual(len(data), 214)
                self.assertNotIn('raw_json', data[0])
                self.assertTrue(all(row['amount'] for row in data))
            with patch.object(queries.Path, 'home', return_value=Path(directory)):
                result = queries.export(self.conn)
                self.assertEqual(Path(result['data']['path']), Path(directory) / '.fin' / 'transactions.csv')
        with self.assertRaises(ValueError):
            queries.export(self.conn, format='xml')

    def test_empty_database_and_rounding(self):
        self.conn.execute('DELETE FROM transactions')
        self.assertEqual(queries.spend(self.conn)['data'], [])
        self.assertEqual(queries.cashflow(self.conn)['data'], [])
        self.assertEqual(queries._money('1.005'), '1.01')
        self.assertEqual(queries._money('-0.001'), '0.00')

    def test_aggregate_rounds_only_after_summing_and_keeps_unknown_currency(self):
        self.conn.execute('DELETE FROM transactions')
        self.conn.executemany("""INSERT INTO transactions
            (transaction_id, account_id, date, amount, currency, category, pending)
            VALUES (?, 'a', ?, ?, ?, 'SHOPPING', 0)""", [
                ('first', '2024-03-30', 0.005, 'USD'),
                ('second', '2024-03-31', 0.005, 'USD'),
                ('unknown', '2024-03-31', 1, None),
                ('earlier', '2024-03-29', 50, 'USD'),
                ('later', '2024-04-01', 50, 'USD')])
        result = queries.spend(self.conn, since='2024-03-30', until='2024-03-31')
        self.assertEqual({row['currency']: row['amount'] for row in result['data']},
                         {'USD': '-0.01', None: '-1.00'})
        self.assertEqual(queries.cashflow(self.conn, since='1d')['range']['until'], '2024-03-31')
        with self.assertRaises(ValueError):
            queries.spend(self.conn, by=None)

    def test_queries_never_read_sensitive_columns_or_write_database(self):
        def authorize(action, table, column, database, source):
            if action == sqlite3.SQLITE_READ and column in {'access_token', 'cursor', 'raw_json'}:
                return sqlite3.SQLITE_DENY
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.conn.set_authorizer(authorize)
        self.addCleanup(self.conn.set_authorizer, None)
        self.conn.row_factory = sqlite3.Row
        for function in (queries.status, queries.accounts, queries.balances, queries.tx,
                         queries.spend, queries.cashflow, queries.recurring, queries.liabilities):
            json.dumps(function(self.conn), allow_nan=False)
        queries.search(self.conn, 'Cafe')
        queries.balances(self.conn, '1mo')
        with tempfile.TemporaryDirectory() as directory:
            queries.export(self.conn, out=Path(directory) / 'safe.csv')

    def test_history_status_in_all_result_freshness(self):
        self.conn.execute("UPDATE items SET history_status='incomplete'")
        for function in (queries.status, queries.accounts, queries.balances, queries.tx,
                         queries.spend, queries.cashflow, queries.recurring, queries.liabilities):
            with self.subTest(function=function.__name__):
                self.assertEqual(function(self.conn)['freshness']['items'][0]['history_status'],
                                 'incomplete')
        self.assertEqual(queries.search(self.conn, 'Cafe')['freshness']['items'][0]['history_status'],
                         'incomplete')
        with tempfile.TemporaryDirectory() as directory:
            result = queries.export(self.conn, out=Path(directory) / 'history.csv')
            self.assertEqual(result['freshness']['items'][0]['history_status'], 'incomplete')

    def test_spend_excludes_legacy_and_v2_income_but_cashflow_keeps_it(self):
        self.conn.executemany("""INSERT INTO transactions
            (transaction_id, account_id, date, amount, currency, category, pending)
            VALUES (?, 'a', '2024-03-31', ?, 'USD', ?, 0)""", [
                ('wages', -200, 'EARNED_INCOME'),
                ('interest', -10, 'UNEARNED_INCOME'),
                ('wage_reversal', 5, 'EARNED_INCOME')])
        for by in ('category', 'merchant', 'account'):
            with self.subTest(by=by):
                # Removing all income must leave the spending results unchanged.
                before = queries.spend(self.conn, by=by, include_transfers=True)
                self.conn.execute('SAVEPOINT income_test')
                self.conn.execute("""DELETE FROM transactions WHERE category IN
                    ('INCOME', 'EARNED_INCOME', 'UNEARNED_INCOME')""")
                after = queries.spend(self.conn, by=by, include_transfers=True)
                self.conn.execute('ROLLBACK TO income_test')
                self.conn.execute('RELEASE income_test')
                self.assertEqual(before['data'], after['data'])
        usd = next(row for row in queries.cashflow(self.conn)['data'] if row['currency'] == 'USD')
        self.assertEqual(usd['inflow'], '310.05')
        self.assertEqual(usd['outflow'], '-6.30')
        self.assertEqual(usd['net'], '303.75')

    def test_export_refuses_existing_file_without_changing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'existing.csv'
            path.write_bytes(b'preserve this file')
            with self.assertRaisesRegex(FileExistsError, 'Choose a new --out path'):
                queries.export(self.conn, out=path)
            self.assertEqual(path.read_bytes(), b'preserve this file')
            new_path = Path(directory) / 'new.json'
            queries.export(self.conn, format='json', out=new_path)
            original = new_path.read_bytes()
            with self.assertRaises(FileExistsError):
                queries.export(self.conn, format='json', out=new_path)
            self.assertEqual(new_path.read_bytes(), original)

    def test_export_protects_database_and_sidecars_including_attached(self):
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / 'main.db'
            attached = Path(directory) / 'attached.db'
            conn = sqlite3.connect(main)
            try:
                conn.execute('CREATE TABLE sentinel (value TEXT)')
                conn.execute("INSERT INTO sentinel VALUES ('keep')")
                conn.commit()
                conn.execute('ATTACH DATABASE ? AS other', (str(attached),))
                for database in (main, attached):
                    for suffix in ('', '-wal', '-shm', '-journal'):
                        path = Path(str(database) + suffix)
                        with self.subTest(path=path):
                            existed = path.exists()
                            with self.assertRaisesRegex(FileExistsError, 'SQLite database or sidecar'):
                                queries.export(conn, out=path)
                            self.assertEqual(path.exists(), existed)
                self.assertEqual(conn.execute('SELECT value FROM sentinel').fetchone()[0], 'keep')
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            finally:
                conn.close()

    @unittest.skipUnless(os.name == 'posix', 'POSIX mode bits required')
    def test_export_permissions_private_without_modifying_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / 'shared'
            parent.mkdir(mode=0o750)
            parent.chmod(0o750)
            restrictive = Path(directory) / 'restricted'
            restrictive.mkdir(mode=0o700)
            restrictive.chmod(0o700)
            previous_umask = os.umask(0)
            try:
                for destination in (parent, restrictive):
                    before = stat.S_IMODE(destination.stat().st_mode)
                    for format in ('csv', 'json'):
                        path = destination / ('private.' + format)
                        queries.export(self.conn, format=format, out=path)
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(destination.stat().st_mode), before)
            finally:
                os.umask(previous_umask)


if __name__ == '__main__':
    unittest.main()
