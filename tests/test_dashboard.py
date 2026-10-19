"""Dashboard correctness and cache invalidation on temporary data."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from finance_agent.dashboard import overview_totals, search_transactions
from finance_agent.database import FinanceRepository
from finance_agent.finance import transactions_frame
from finance_agent.retrieval import KnowledgeSearch
from finance_agent.storage import DataError


def transaction(amount, code="EGP", **extra):
    return {"date": "2026-01-01", "merchant": "Shop [local]", "description": "Food",
            "amount": amount, "currency": code, **extra}


class DashboardTests(unittest.TestCase):
    def test_metrics_use_all_rows_decimal_totals_and_separate_currencies(self):
        rows = [transaction(-0.1) for _ in range(90)] + [transaction(10), transaction(-5, "USD")]
        totals = {row["currency"]: row for row in overview_totals(transactions_frame(rows))}
        self.assertEqual(totals["EGP"]["count"], 91)
        self.assertEqual(totals["EGP"]["expenses"], 9)
        self.assertEqual(totals["EGP"]["net"], 1)
        self.assertEqual(totals["EGP"]["savings_rate"], 10)
        self.assertIsNone(totals["USD"]["savings_rate"])
        self.assertEqual(totals["USD"]["net"], -5)

    def test_empty_metrics_and_search(self):
        frame = transactions_frame([])
        self.assertEqual(overview_totals(frame), [])
        self.assertTrue(search_transactions(frame, "Shop").empty)

    def test_search_is_literal_case_insensitive_and_includes_category(self):
        frame = transactions_frame([transaction(-10), transaction(-20, merchant="Uber", description="Trip"), transaction(50)])
        self.assertEqual(len(search_transactions(frame, "[local]")), 2)
        self.assertEqual(len(search_transactions(frame, "TRANSPORT")), 1)
        self.assertEqual(len(search_transactions(frame, "   ")), 3)
        self.assertTrue(search_transactions(frame, ".*").empty)

    def test_savings_rate_is_negative_for_deficit(self):
        total = overview_totals(transactions_frame([transaction(100), transaction(-150)]))[0]
        self.assertEqual(total["savings_rate"], -50)


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_database_snapshot_sees_external_writes_without_stale_cache(self):
        repo = FinanceRepository(self.root / "finance.sqlite3")
        self.assertEqual(repo.snapshot(), ([], []))
        external = FinanceRepository(repo.path)
        first = external.add_transaction(transaction(-10))
        external.add_goal({"name": "Fund", "currency": "EGP", "target_amount": 100, "current_amount": 10})
        transactions, goals = repo.snapshot()
        self.assertEqual(transactions[0]["id"], first["id"])
        self.assertEqual(goals[0]["name"], "Fund")
        external.update_transaction(first["id"], {"amount": -20})
        self.assertEqual(repo.snapshot()[0][0]["amount"], -20)

    def test_snapshot_rejects_unknown_schema(self):
        repo = FinanceRepository(self.root / "finance.sqlite3")
        with repo.connection(write=True) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(DataError):
            repo.snapshot()

    def test_snapshot_reads_transactions_and_goals_in_one_transaction(self):
        repo = FinanceRepository(self.root / "finance.sqlite3")
        traces = []
        original = repo.connection
        from contextlib import contextmanager

        @contextmanager
        def traced(*args, **kwargs):
            with original(*args, **kwargs) as db:
                db.set_trace_callback(traces.append)
                yield db

        with patch.object(repo, "connection", side_effect=traced) as connection:
            repo.snapshot()
        connection.assert_called_once()
        self.assertEqual(traces[0], "BEGIN")
        self.assertTrue(any("FROM transactions" in sql for sql in traces))
        self.assertTrue(any("FROM goals" in sql for sql in traces))

    def test_repeated_retrieval_skips_index_reads_but_detects_source_and_index_changes(self):
        knowledge = self.root / "knowledge"
        knowledge.mkdir()
        source = knowledge / "budget.md"
        source.write_text("Budget income savings", encoding="utf-8")
        search = KnowledgeSearch(knowledge, self.root / "index")
        first = search.retrieve("budget")
        original = Path.read_text
        reads = []

        def counted(path, *args, **kwargs):
            reads.append(path)
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", counted):
            second = search.retrieve("budget")
            search.retrieve("income")
        self.assertFalse(second["rebuilt"])
        self.assertEqual(first["hits"], second["hits"])
        self.assertNotIn(search.index_path, reads)
        source.write_text("Budget income updated advice", encoding="utf-8")
        changed = search.retrieve("updated")
        self.assertTrue(changed["rebuilt"])
        self.assertIn("updated", changed["hits"][0]["text"])
        search.index_path.write_text("invalid", encoding="utf-8")
        self.assertTrue(search.retrieve("budget")["rebuilt"])
        search.index_path.unlink()
        self.assertTrue(search.retrieve("budget")["rebuilt"])

    def test_manual_rebuild_bypasses_memory_cache(self):
        knowledge = self.root / "knowledge"
        knowledge.mkdir()
        (knowledge / "one.md").write_text("Budget", encoding="utf-8")
        search = KnowledgeSearch(knowledge, self.root / "index")
        search.retrieve("budget")
        self.assertTrue(search.rebuild()["rebuilt"])


if __name__ == "__main__":
    unittest.main()
