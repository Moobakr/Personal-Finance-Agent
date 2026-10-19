from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from finance_agent.database import FinanceRepository, export_csv, parse_csv
from finance_agent.finance import transactions_frame
from finance_agent.storage import DataError, atomic_write_json


def transaction(**changes):
    return {"date": "2026-01-02", "merchant": "Uber", "amount": -12.34,
            "currency": "EGP", "description": "Trip", **changes}


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "finance.sqlite3"
        self.repo = FinanceRepository(self.path)

    def sources(self, transactions=None, goals=None):
        tx_path, goal_path = self.root / "transactions.json", self.root / "goals.json"
        atomic_write_json(tx_path, transactions if transactions is not None else [transaction(id="original")])
        atomic_write_json(goal_path, {"goals": goals if goals is not None else [
            {"name": "Fund", "currency": "EGP", "target_amount": 500, "current_amount": 20}]})
        return tx_path, goal_path

    def test_migration_preserves_sources_values_ids_and_snapshots(self):
        tx_path, goal_path = self.sources([transaction(id="original", bank_reference="ABC")])
        before = {"transactions": tx_path.read_bytes(), "goals": goal_path.read_bytes()}
        self.assertTrue(self.repo.migrate_json(tx_path, goal_path))
        rows = self.repo.list_transactions()
        self.assertEqual(rows[0]["id"], "original")
        self.assertEqual(rows[0]["amount"], -12.34)
        self.assertEqual(rows[0]["bank_reference"], "ABC")
        self.assertEqual(tx_path.read_bytes(), before["transactions"])
        self.assertEqual(goal_path.read_bytes(), before["goals"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT amount_cents FROM transactions").fetchone()[0], -1234)
            for name, content in db.execute("SELECT name,content FROM legacy_sources"):
                self.assertEqual(content, before[name])

    def test_migration_is_once_even_after_deletion_or_source_changes(self):
        tx_path, goal_path = self.sources()
        self.repo.migrate_json(tx_path, goal_path)
        self.repo.delete_transaction("original")
        tx_path.write_text("broken", encoding="utf-8")
        self.assertFalse(FinanceRepository(self.path).migrate_json(tx_path, goal_path))
        self.assertEqual(self.repo.list_transactions(), [])
        self.assertEqual(len(self.repo.list_goals()), 1)

    def test_migration_invalid_goals_rolls_back_and_can_retry(self):
        tx_path, goal_path = self.sources(goals=[{"name": "Invalid", "target_amount": -1}])
        with self.assertRaises(DataError):
            self.repo.migrate_json(tx_path, goal_path)
        self.assertEqual(self.repo.list_transactions(), [])
        atomic_write_json(goal_path, {"goals": []})
        self.assertTrue(self.repo.migrate_json(tx_path, goal_path))
        self.assertEqual(len(self.repo.list_transactions()), 1)

    def test_migration_duplicate_goal_ids_is_atomic(self):
        goal = {"id": "same", "name": "Fund", "target_amount": 100}
        tx_path, goal_path = self.sources(goals=[goal, goal])
        with self.assertRaises(DataError):
            self.repo.migrate_json(tx_path, goal_path)
        self.assertEqual(self.repo.list_transactions(), [])
        self.assertEqual(self.repo.list_goals(), [])

    def test_transaction_edit_delete_and_restart(self):
        row = self.repo.add_transaction(transaction())
        updated = self.repo.update_transaction(row["id"], {"amount": -20.01, "merchant": "New shop"})
        self.assertEqual(updated["id"], row["id"])
        restarted = FinanceRepository(self.path)
        self.assertEqual(restarted.list_transactions()[0]["amount"], -20.01)
        with self.assertRaises(DataError):
            restarted.update_transaction(row["id"], {"amount": "NaN"})
        self.assertEqual(restarted.list_transactions()[0]["amount"], -20.01)
        restarted.delete_transaction(row["id"])
        self.assertEqual(self.repo.list_transactions(), [])
        with self.assertRaises(DataError):
            restarted.delete_transaction(row["id"])

    def test_duplicate_add_requires_explicit_choice(self):
        self.repo.add_transaction(transaction())
        with self.assertRaises(DataError):
            self.repo.add_transaction(transaction(merchant="  UBER  "))
        self.repo.add_transaction(transaction(), allow_duplicate=True)
        self.assertEqual(len(self.repo.list_transactions()), 2)

    def test_multiple_goals_are_independent(self):
        first = self.repo.add_goal({"name": "Fund", "currency": "EGP", "target_amount": 500, "current_amount": 20})
        second = self.repo.add_goal({"name": "Laptop", "currency": "USD", "target_amount": 1000, "current_amount": 0})
        self.repo.update_goal(second["id"], {"current_amount": 300})
        goals = FinanceRepository(self.path).list_goals()
        self.assertEqual([goal["current_amount"] for goal in goals], [20, 300])
        self.repo.delete_goal(first["id"])
        self.assertEqual(self.repo.list_goals()[0]["id"], second["id"])
        with self.assertRaises(DataError):
            self.repo.update_goal(second["id"], {"target_amount": 0})

    def test_csv_round_trip_with_unicode_quotes_and_categories(self):
        row = self.repo.add_transaction(transaction(merchant='Shop, "one" متجر', description="Line 1\nLine 2", category="Travel"))
        exported = export_csv(self.repo.list_transactions())
        parsed = parse_csv(exported)
        self.assertEqual(parsed[0]["merchant"], row["merchant"])
        self.assertEqual(parsed[0]["description"], row["description"])
        self.assertEqual(parsed[0]["amount"], -12.34)
        self.assertEqual(transactions_frame(parsed).iloc[0]["category"], "Travel")
        self.assertEqual(self.repo.import_csv(exported), {"imported": 0, "duplicates": 1})

    def test_csv_duplicate_detection_and_optional_identical_purchases(self):
        content = b'date,amount,currency,merchant,description\n2026-01-02,-12.34,EGP,Uber,Trip\n2026-01-02,-12.34,EGP,UBER,Trip\n'
        preview = self.repo.preview_csv(content)
        self.assertEqual((preview["new"], preview["duplicates"]), (1, 1))
        self.assertEqual(self.repo.list_transactions(), [])
        self.assertEqual(self.repo.import_csv(content), {"imported": 1, "duplicates": 1})
        self.assertEqual(self.repo.import_csv(content), {"imported": 0, "duplicates": 2})
        self.assertEqual(self.repo.import_csv(content, skip_matching=False)["imported"], 2)

    def test_csv_conflicting_id_aborts_entire_import(self):
        self.repo.add_transaction(transaction(id="existing"))
        content = export_csv([transaction(id="new", amount=100), transaction(id="existing", amount=-99)])
        with self.assertRaisesRegex(DataError, "different transaction"):
            self.repo.import_csv(content)
        self.assertEqual(len(self.repo.list_transactions()), 1)

    def test_csv_invalid_late_row_imports_nothing(self):
        content = b'date,amount,currency\n2026-01-01,10,USD\n2026-01-02,NaN,USD\n'
        with self.assertRaisesRegex(DataError, "line 3"):
            self.repo.import_csv(content)
        self.assertEqual(self.repo.list_transactions(), [])

    def test_csv_insert_failure_rolls_back_all_new_rows(self):
        content = export_csv([transaction(amount=1), transaction(amount=2)])
        real_insert = self.repo._insert_tx
        calls = []
        def fail_second(db, row):
            calls.append(row)
            if len(calls) == 2:
                raise sqlite3.IntegrityError("simulated write failure")
            real_insert(db, row)
        with patch.object(self.repo, "_insert_tx", side_effect=fail_second), self.assertRaises(DataError):
            self.repo.import_csv(content)
        self.assertEqual(self.repo.list_transactions(), [])

    def test_csv_bad_headers_encoding_and_shape(self):
        for content in [b'date,amount\n2026-01-01,1\n', b'date,date,amount,currency\n',
                        b'date,amount,currency\n2026-01-01,1,USD,extra\n',
                        b'\xff', b'date,amount,currency\n', b'x' * (2 * 1024 * 1024 + 1)]:
            with self.subTest(content=content[:35]), self.assertRaises(DataError):
                parse_csv(content)

    def test_concurrent_writers_do_not_lose_transactions(self):
        def add(index):
            FinanceRepository(self.path).add_transaction(transaction(amount=index + 1))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(add, range(12)))
        self.assertEqual(len(self.repo.list_transactions()), 12)

    def test_unsupported_database_version_fails_explicitly(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(DataError, "schema version"):
            FinanceRepository(self.path)


if __name__ == "__main__":
    unittest.main()
