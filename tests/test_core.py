import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import requests

from finance_agent.agent import decide_route
from finance_agent.api import OpenRouterClient, ServiceError, convert_currency
from finance_agent.config import load_settings
from finance_agent.finance import (budget_plan, category_breakdown, monthly_summary,
                                   selected_transactions, summarize_user_data, transactions_frame)
from finance_agent.retrieval import KnowledgeSearch, get_embedder
from finance_agent.storage import (DataError, append_transaction, atomic_write_json,
                                  load_goals, load_transactions, money, save_goals, validate_transaction)

ROOT = Path(__file__).resolve().parents[1]


def tx(amount=-10, currency="EGP", day="2026-01-01", **kwargs):
    return validate_transaction({"date": day, "amount": amount, "currency": currency,
                                 "merchant": "Uber", "description": "Trip", **kwargs})


class TemporaryProject(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)


class StorageTests(TemporaryProject):
    def test_transaction_round_trip_and_unique_ids(self):
        path = self.root / "data/transactions.json"
        first = append_transaction(path, tx())
        second = append_transaction(path, tx(100))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(load_transactions(path)), 2)

    def test_corrupted_file_is_not_overwritten(self):
        path = self.root / "transactions.json"
        path.write_text("broken JSON", encoding="utf-8")
        with self.assertRaises(DataError):
            append_transaction(path, tx())
        self.assertEqual(path.read_text(), "broken JSON")

    def test_invalid_amount_date_currency_and_missing_fields(self):
        for updates in [{"amount": "NaN"}, {"amount": float("inf")}, {"amount": True},
                        {"amount": "1e1000000"}, {"amount": 1.234}, {"date": "2026-02-30"}, {"currency": "$$$"}]:
            with self.subTest(updates=updates), self.assertRaises(DataError):
                validate_transaction({**tx(), **updates})
        with self.assertRaises(DataError):
            validate_transaction({"amount": 10})

    def test_invalid_existing_transaction_blocks_append(self):
        path = self.root / "transactions.json"
        atomic_write_json(path, [{"date": "invalid", "amount": 10}])
        original = path.read_bytes()
        with self.assertRaises(DataError):
            append_transaction(path, tx())
        self.assertEqual(path.read_bytes(), original)

    def test_duplicate_id_rejected(self):
        path = self.root / "transactions.json"
        append_transaction(path, tx(id="fixed"))
        with self.assertRaises(DataError):
            append_transaction(path, tx(id="fixed"))

    def test_failed_atomic_write_keeps_original_file(self):
        path = self.root / "transactions.json"
        atomic_write_json(path, [tx()])
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            atomic_write_json(path, {"invalid": float("nan")})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_goal_validation_and_round_trip(self):
        path = self.root / "goals.json"
        goal = {"name": "Fund", "target_amount": 100, "current_amount": 10, "currency": "EGP"}
        save_goals(path, {"goals": [goal]})
        self.assertEqual(load_goals(path)["goals"][0]["name"], "Fund")
        for update in [{"current_amount": -1}, {"target_amount": 0}, {"deadline": "2026-13-01"}]:
            with self.subTest(update=update), self.assertRaises(DataError):
                save_goals(path, {"goals": [{**goal, **update}]})
        self.assertEqual(load_goals(path)["goals"][0]["current_amount"], 10)


class FinanceTests(unittest.TestCase):
    def setUp(self):
        self.df = transactions_frame([tx(-10) for _ in range(70)] + [tx(1000), tx(50, "USD"), tx(-5, "USD"), tx(90, day="2025-12-01")])

    def test_all_selected_rows_used_beyond_sixty(self):
        summary = json.loads(summarize_user_data(self.df, "2026-01", "EGP"))
        self.assertEqual(summary["transaction_count"], 71)
        totals = summary["totals_by_currency"][0]
        self.assertEqual(float(totals["income"]), 1000)
        self.assertEqual(float(totals["expenses"]), 700)
        self.assertEqual(float(totals["net"]), 300)

    def test_currencies_never_combined(self):
        summary = monthly_summary(self.df, "2026-01").set_index("currency")
        self.assertEqual(summary.loc["EGP", "net"], 300)
        self.assertEqual(summary.loc["USD", "net"], 45)
        categories = category_breakdown(self.df, "2026-01").set_index("currency")
        self.assertEqual(categories.loc["EGP", "total"], 700)
        self.assertEqual(categories.loc["USD", "total"], 5)

    def test_decimal_arithmetic(self):
        frame = transactions_frame([tx(0.1), tx(0.2), tx(-0.3)])
        self.assertEqual(monthly_summary(frame).iloc[0]["net"], 0)
        self.assertEqual(sum(money(v) for v in budget_plan(0.03).values()), money(0.03))

    def test_empty_and_unmatched_period(self):
        empty = transactions_frame([])
        self.assertTrue(monthly_summary(empty).empty)
        self.assertTrue(category_breakdown(self.df, "2030-01").empty)
        self.assertIn("No transactions", summarize_user_data(self.df, "2030-01"))

    def test_invalid_month(self):
        for month in ["2026-13", "2026-1", "bad"]:
            with self.subTest(month=month), self.assertRaises(DataError):
                selected_transactions(self.df, month)

    def test_word_boundaries_in_routing(self):
        self.assertEqual(decide_route("What is my savings rate?"), "rag")
        self.assertEqual(decide_route("Categorize my expenses"), "categorize")
        self.assertEqual(decide_route("Summarize my month and suggest a plan"), "budget_plus_advice")


class RetrievalTests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.knowledge = self.root / "knowledge"
        self.knowledge.mkdir()
        (self.knowledge / "budget.md").write_text("Budget income expenses savings budget", encoding="utf-8")
        (self.knowledge / "debt.md").write_text("Debt credit interest repayment", encoding="utf-8")
        self.search = KnowledgeSearch(self.knowledge, self.root / "index")
        get_embedder.cache_clear()
        self.addCleanup(get_embedder.cache_clear)

    def test_legacy_index_rebuilt_and_then_reused(self):
        atomic_write_json(self.search.index_path, {"docs": [{"id": "old", "embedding": [0] * 384}]})
        result = self.search.retrieve("budget")
        self.assertTrue(result["rebuilt"])
        self.assertEqual(result["hits"][0]["id"], "budget.md")
        self.assertFalse(self.search.retrieve("budget")["rebuilt"])
        index = json.loads(self.search.index_path.read_text())
        self.assertEqual(index["metadata"]["dimensions"], 768)

    def test_corpus_change_and_corrupted_vector_rebuilt(self):
        self.search.retrieve("budget")
        (self.knowledge / "budget.md").write_text("Budget monthly planning", encoding="utf-8")
        self.assertTrue(self.search.retrieve("budget")["rebuilt"])
        index = json.loads(self.search.index_path.read_text())
        index["docs"][0]["embedding"] = [0] * 384
        atomic_write_json(self.search.index_path, index)
        self.assertTrue(self.search.retrieve("budget")["rebuilt"])
        self.search.index_path.write_text("{invalid", encoding="utf-8")
        self.assertTrue(self.search.retrieve("budget")["rebuilt"])

    def test_model_load_failure_falls_back(self):
        with patch("finance_agent.retrieval.TransformerEmbedder", side_effect=RuntimeError("missing model")):
            search = KnowledgeSearch(self.knowledge, self.root / "index", backend="sentence_transformers")
            result = search.retrieve("budget")
        self.assertEqual(result["backend"], "stable_hash")
        self.assertTrue(result["warning"])
        self.assertTrue(result["hits"])

    def test_runtime_encoder_failure_falls_back(self):
        failing = Mock(backend="sentence_transformers", model="fake", dimensions=384)
        failing.encode.side_effect = RuntimeError("encoder unavailable")
        with patch("finance_agent.retrieval.get_embedder", return_value=(failing, "")):
            result = self.search.retrieve("budget")
        self.assertEqual(result["backend"], "stable_hash")
        self.assertTrue(result["hits"])

    def test_backend_switch_rebuilds(self):
        self.search.retrieve("budget")
        fake = Mock(backend="sentence_transformers", model="test-encoder-v1", dimensions=3)
        fake.encode.side_effect = lambda texts: np.ones((len(texts), 3), dtype=np.float32)
        with patch("finance_agent.retrieval.get_embedder", return_value=(fake, "")):
            result = self.search.retrieve("budget")
        self.assertTrue(result["rebuilt"])
        self.assertTrue(self.search.retrieve("budget")["rebuilt"])

    def test_restart_hash_seed_preserves_index_and_results(self):
        expected = self.search.retrieve("budget")
        code = ("import json,sys; from finance_agent.retrieval import KnowledgeSearch; "
                "print(json.dumps(KnowledgeSearch(sys.argv[1],sys.argv[2]).retrieve('budget'))) ")
        for seed in ["1", "2"]:
            output = subprocess.check_output([sys.executable, "-c", code, str(self.knowledge), str(self.search.index_path.parent)],
                                             cwd=ROOT, env={**os.environ, "PYTHONHASHSEED": seed}, text=True)
            actual = json.loads(output)
            self.assertFalse(actual["rebuilt"])
            self.assertEqual(actual["hits"], expected["hits"])

    def test_empty_knowledge_and_removed_documents(self):
        self.search.retrieve("budget")
        for path in self.knowledge.iterdir():
            path.unlink()
        result = self.search.retrieve("budget")
        self.assertEqual(result["hits"], [])
        self.assertTrue(result["rebuilt"])


class ConfigurationTests(TemporaryProject):
    def test_environment_override_and_project_relative_paths(self):
        (self.root / ".streamlit").mkdir()
        (self.root / ".streamlit/secrets.toml").write_text('OPENROUTER_API_KEY = "file-key"\nOPENROUTER_MODEL = "file-model"', encoding="utf-8")
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "env-key", "TRANSACTIONS_PATH": "custom/tx.json"}, clear=True):
            settings = load_settings(self.root)
        self.assertEqual(settings.api_key, "env-key")
        self.assertEqual(settings.model, "file-model")
        self.assertEqual(settings.transactions_path, self.root / "custom/tx.json")
        self.assertNotIn("env-key", repr(settings))

    def test_missing_key_allowed_and_bad_config_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_settings(self.root).api_key, "")
        with patch.dict(os.environ, {"EMBEDDING_BACKEND": "unknown"}, clear=True), self.assertRaises(ValueError):
            load_settings(self.root)


class ApiTests(TemporaryProject):
    def test_ai_payload_contains_complete_currency_separated_summary(self):
        session = Mock()
        session.post.return_value = Mock(ok=True)
        session.post.return_value.json.return_value = {"choices": [{"message": {"content": "Advice"}}]}
        client = OpenRouterClient("test-only", "configured/model", session=session)
        frame = transactions_frame([tx(-10) for _ in range(70)] + [tx(100, "USD")])
        self.assertEqual(client.answer("Help", summarize_user_data(frame), []), "Advice")
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "configured/model")
        user = json.loads(payload["messages"][1]["content"])
        summary = json.loads(user["calculated_summary"])
        self.assertEqual(summary["transaction_count"], 71)
        self.assertEqual(len(summary["totals_by_currency"]), 2)
        self.assertNotIn("Uber", payload["messages"][1]["content"])
        self.assertEqual(session.post.call_args.kwargs["timeout"], (10, 60))

    def test_missing_key_and_http_failures(self):
        session = Mock()
        with self.assertRaises(ServiceError):
            OpenRouterClient("", "model", session).answer("q", "summary", [])
        session.post.assert_not_called()
        for status in [401, 402, 404, 429, 500]:
            session.post.return_value = Mock(ok=False, status_code=status)
            with self.subTest(status=status), self.assertRaises(ServiceError) as caught:
                OpenRouterClient("secret-placeholder", "model", session).answer("q", "summary", [])
            self.assertNotIn("secret-placeholder", str(caught.exception))

    def test_timeout_and_empty_answer(self):
        session = Mock()
        session.post.side_effect = requests.Timeout()
        with self.assertRaisesRegex(ServiceError, "timed out"):
            OpenRouterClient("key", "model", session).answer("q", "summary", [])
        session.post.side_effect = None
        session.post.return_value = Mock(ok=True)
        session.post.return_value.json.return_value = {"choices": []}
        with self.assertRaisesRegex(ServiceError, "invalid answer"):
            OpenRouterClient("key", "model", session).answer("q", "summary", [])

    def test_fx_fallback_and_same_currency(self):
        path = self.root / "fx.json"
        atomic_write_json(path, {"base": "USD", "rates": {"EGP": 50, "EUR": 0.9}})
        session = Mock()
        session.get.side_effect = requests.Timeout()
        result = convert_currency(100, "USD", "EGP", path, session=session)
        self.assertEqual(result["converted"], 5000)
        self.assertEqual(result["source"], "offline fallback")
        self.assertEqual(convert_currency(20, "USD", "USD", path, session=session)["converted"], 20)
        with self.assertRaises(ServiceError):
            convert_currency(10, "USD", "XYZ", path, session=session)


if __name__ == "__main__":
    unittest.main()
