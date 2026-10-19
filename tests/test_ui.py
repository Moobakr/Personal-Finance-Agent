"""Actual Streamlit interactions on temporary data; external calls are mocked."""
from pathlib import Path
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from finance_agent.config import Settings
from finance_agent.database import FinanceRepository, export_csv
from finance_agent.storage import atomic_write_json, load_goals, load_transactions

ROOT = Path(__file__).resolve().parents[1]


class UiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(root=root, transactions_path=root / "transactions.json",
                                 goals_path=root / "goals.json", fx_path=root / "fx.json",
                                 knowledge_dir=root / "knowledge", store_dir=root / "index")
        self.settings.knowledge_dir.mkdir()
        (self.settings.knowledge_dir / "budget.md").write_text("Budget income expenses savings", encoding="utf-8")
        atomic_write_json(self.settings.transactions_path, [
            {"id": "one", "date": "2026-01-01", "merchant": "Employer", "description": "Salary", "amount": 1000, "currency": "EGP"},
            {"id": "two", "date": "2026-01-02", "merchant": "Uber", "description": "Trip", "amount": -100, "currency": "EGP"},
            {"id": "three", "date": "2026-01-02", "merchant": "Shop", "description": "Expense", "amount": -5, "currency": "USD"},
        ])
        atomic_write_json(self.settings.goals_path, {"goals": [
            {"name": "Fund", "currency": "EGP", "target_amount": 5000, "current_amount": 1000},
            {"name": "Laptop", "currency": "USD", "target_amount": 1000, "current_amount": 50},
        ]})
        config = patch("finance_agent.ui.load_settings", side_effect=lambda: self.settings)
        config.start()
        self.addCleanup(config.stop)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected live network request"))
        network.start()
        self.addCleanup(network.stop)

    def start(self, section="Assistant"):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.assertFalse(app.exception, str(app.exception))
        if any(item.label == "Section" for item in app.radio):
            self.navigate(app, section)
        return app

    def navigate(self, app, section):
        next(item for item in app.radio if item.label == "Section").set_value(section).run()
        return app

    def button(self, app, label):
        return next(item for item in app.button if item.label == label)

    def input(self, app, label):
        return next(item for item in app.text_input if item.label == label)

    def assert_clean(self, app):
        self.assertFalse(app.exception, str(app.exception))
        self.assertFalse(app.error, str([error.value for error in app.error]))

    def test_startup_without_transformer_or_api_key(self):
        with patch.dict("sys.modules", {"sentence_transformers": None}):
            app = self.start()
        self.assert_clean(app)
        self.assertFalse(self.settings.store_dir.exists(), "startup must not eagerly build embeddings")

    def test_absolute_path_launch_from_another_directory(self):
        code = ("import sys; from streamlit.testing.v1 import AppTest; "
                "app=AppTest.from_file(sys.argv[1], default_timeout=30).run(); "
                "assert not app.exception, str(app.exception); "
                "assert not app.error, str([e.value for e in app.error])")
        env = {**os.environ, "OPENROUTER_API_KEY": "", "EMBEDDING_BACKEND": "hash",
               "DATABASE_PATH": str(self.settings.db_path),
               "TRANSACTIONS_PATH": str(self.settings.transactions_path),
               "GOALS_PATH": str(self.settings.goals_path),
               "KNOWLEDGE_DIR": str(self.settings.knowledge_dir),
               "RAG_STORE_DIR": str(self.settings.store_dir)}
        result = subprocess.run([sys.executable, "-c", code, str(ROOT / "app.py")],
                                cwd=self.settings.root, env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_month_summary_separates_currencies(self):
        app = self.start()
        app.text_area[0].set_value("Show monthly summary")
        self.button(app, "Run").click().run()
        self.assert_clean(app)
        tables = [frame.value for frame in app.dataframe]
        summary = next(frame for frame in tables if "net" in frame.columns)
        self.assertEqual(set(summary.currency), {"EGP", "USD"})
        self.assertEqual(summary.set_index("currency").loc["EGP", "net"], 900)

    def test_invalid_month_disables_run(self):
        app = self.start()
        self.input(app, "Month (YYYY-MM, blank for all)").set_value("2026-13").run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertTrue(self.button(app, "Run").disabled)

    def test_save_transaction_persists_and_refreshes(self):
        app = self.start("Transactions")
        self.input(app, "Merchant").set_value("Test shop")
        self.button(app, "Save Transaction").click().run()
        self.assert_clean(app)
        rows = FinanceRepository(self.settings.db_path).list_transactions()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[-1]["merchant"], "Test shop")
        self.assertEqual(rows[-1]["amount"], -100)
        restarted = self.start("Transactions")
        self.assert_clean(restarted)
        self.assertTrue(any(len(frame.value) == 4 for frame in restarted.dataframe))

    def test_goal_update_preserves_other_goals(self):
        app = self.start("Goals")
        goals = FinanceRepository(self.settings.db_path).list_goals()
        next(item for item in app.selectbox if item.label == "Goal to manage").set_value(goals[0]["id"]).run()
        self.input(app, "Goal name").set_value("Updated fund")
        self.button(app, "Save goal").click().run()
        self.assert_clean(app)
        goals = FinanceRepository(self.settings.db_path).list_goals()
        self.assertEqual(len(goals), 2)
        self.assertEqual(goals[0]["name"], "Updated fund")
        self.assertEqual(goals[1]["name"], "Laptop")

    def test_corrupt_transactions_do_not_enable_save(self):
        self.settings.transactions_path.write_text("broken", encoding="utf-8")
        app = self.start()
        self.assertTrue(app.error)
        self.assertFalse(any(item.label == "Save Transaction" for item in app.button))
        self.assertEqual(FinanceRepository(self.settings.db_path).list_transactions(), [])
        self.assertEqual(self.settings.transactions_path.read_text(), "broken")

    def test_index_build_and_ai_flow_with_mocked_provider(self):
        from dataclasses import replace
        self.settings = replace(self.settings, api_key="test-only")
        app = self.start()
        self.button(app, "Build / Rebuild RAG Index").click().run()
        self.assert_clean(app)
        self.assertTrue((self.settings.store_dir / "kb_index.json").exists())
        app.text_area[0].set_value("Suggest a budget plan")
        with patch("finance_agent.api.OpenRouterClient.answer", return_value="Verified example advice") as answer:
            self.button(app, "Run").click().run()
        self.assert_clean(app)
        answer.assert_called_once()
        self.assertTrue(any("Verified example advice" in item.value for item in app.markdown))

    def test_ai_missing_key_is_clear(self):
        app = self.start()
        app.text_area[0].set_value("Give me budgeting advice")
        self.button(app, "Run").click().run()
        self.assert_clean(app)
        self.assertTrue(any("OPENROUTER_API_KEY" in item.value for item in app.info))

    def test_edit_and_confirmed_delete_transaction(self):
        app = self.start("Transactions")
        next(item for item in app.selectbox if item.label == "Transaction to manage").set_value("two").run()
        self.input(app, "Edit merchant").set_value("Updated ride")
        next(item for item in app.number_input if item.label == "Signed amount (expense negative)").set_value(-150.25)
        self.button(app, "Save changes").click().run()
        self.assert_clean(app)
        row = next(row for row in FinanceRepository(self.settings.db_path).list_transactions() if row["id"] == "two")
        self.assertEqual((row["merchant"], row["amount"]), ("Updated ride", -150.25))
        self.assertTrue(self.button(app, "Delete transaction").disabled)
        next(item for item in app.checkbox if item.label == "Delete selected transaction permanently").check().run()
        self.button(app, "Delete transaction").click().run()
        self.assert_clean(app)
        self.assertNotIn("two", [row["id"] for row in FinanceRepository(self.settings.db_path).list_transactions()])

    def test_add_edit_delete_multiple_goals(self):
        app = self.start("Goals")
        self.input(app, "Goal name").set_value("Holiday")
        self.button(app, "Save goal").click().run()
        self.assert_clean(app)
        repo = FinanceRepository(self.settings.db_path)
        goals = repo.list_goals()
        self.assertEqual(len(goals), 3)
        holiday = next(goal for goal in goals if goal["name"] == "Holiday")
        next(item for item in app.selectbox if item.label == "Goal to manage").set_value(holiday["id"]).run()
        next(item for item in app.number_input if item.label == "Current amount").set_value(500.0)
        self.button(app, "Save goal").click().run()
        self.assert_clean(app)
        self.assertEqual(next(goal for goal in repo.list_goals() if goal["id"] == holiday["id"])["current_amount"], 500)
        self.assertTrue(self.button(app, "Delete goal").disabled)
        next(item for item in app.checkbox if item.label == "Delete selected goal permanently").check().run()
        self.button(app, "Delete goal").click().run()
        self.assert_clean(app)
        self.assertEqual([goal["name"] for goal in repo.list_goals()], ["Fund", "Laptop"])

    def test_csv_preview_import_and_repeat_skip(self):
        content = b'date,amount,currency,merchant,description\n2026-02-01,-20,EGP,Restaurant,Food\n2026-02-01,-20,EGP,Restaurant,Food\n'
        with patch("streamlit.file_uploader", return_value=io.BytesIO(content)):
            app = self.start("Transactions")
            self.assertTrue(any("1 new transactions; 1 duplicates" in item.value for item in app.info))
            self.button(app, "Import transactions").click().run()
            self.assert_clean(app)
            self.assertEqual(len(FinanceRepository(self.settings.db_path).list_transactions()), 4)
            self.assertTrue(self.button(app, "Import transactions").disabled)
        self.assertEqual(len(load_transactions(self.settings.transactions_path)), 3)

    def test_invalid_csv_ui_keeps_database_unchanged(self):
        content = b'date,amount,currency\n2026-01-01,10,USD\n2026-01-02,invalid,USD\n'
        with patch("streamlit.file_uploader", return_value=io.BytesIO(content)):
            app = self.start("Transactions")
            self.assertFalse(app.exception)
            self.assertTrue(app.error)
            self.assertFalse(any(item.label == "Import transactions" for item in app.button))
        self.assertEqual(len(FinanceRepository(self.settings.db_path).list_transactions()), 3)

    def test_json_changes_after_migration_do_not_replace_database(self):
        self.start()
        self.settings.transactions_path.write_text("legacy edited outside app", encoding="utf-8")
        app = self.start()
        self.assert_clean(app)
        self.assertEqual(len(FinanceRepository(self.settings.db_path).list_transactions()), 3)

    def test_chat_conversion_history_followup_and_clear(self):
        app = self.start()
        repo = FinanceRepository(self.settings.db_path)
        original = repo.list_transactions()
        with patch("finance_agent.agent.convert_currency", return_value={"converted": 5000, "rate": 50, "source": "live API", "note": "Test timestamp"}) as convert:
            app.text_area[0].set_value("Convert 100 USD to EGP")
            self.button(app, "Run").click().run()
            self.assert_clean(app)
            self.assertEqual(convert.call_count, 1)
            self.assertTrue(any("5,000.00 EGP" in item.value for item in app.markdown))
            self.navigate(app, "Transactions")
            self.input(app, "Merchant").set_value("Unsaved draft").run()
            self.navigate(app, "Assistant")
            self.assertEqual(convert.call_count, 1, "rerender must not call the tool again")
            app.text_area[0].set_value("And 50?")
            self.button(app, "Run").click().run()
            self.assertEqual(convert.call_args.kwargs["amount"], 50)
            self.assertEqual(len(app.session_state["chat_history"]), 2)
            self.button(app, "Clear conversation").click().run()
            self.assertEqual(app.session_state["chat_history"], [])
            self.assertIsNone(app.session_state["chat_context"])
        self.assertEqual(repo.list_transactions(), original)

    def test_chat_missing_details_clarifies_without_service_call(self):
        app = self.start()
        app.text_area[0].set_value("Convert my money")
        with patch("finance_agent.agent.convert_currency") as convert:
            self.button(app, "Run").click().run()
        self.assert_clean(app)
        convert.assert_not_called()
        self.assertTrue(any("Convert 100 USD to EGP" in info.value for info in app.info))

    def test_chat_history_limit_and_filter_change_resets_context(self):
        app = self.start()
        app.text_area[0].set_value("Summary 2026-01 in EGP")
        self.button(app, "Run").click().run()
        for _ in range(10):
            self.button(app, "Run").click().run()
        self.assert_clean(app)
        self.assertEqual(len(app.session_state["chat_history"]), 10)
        self.assertEqual(app.session_state["chat_context"]["currency"], "EGP")
        self.input(app, "Month (YYYY-MM, blank for all)").set_value("2026-02").run()
        self.assertIsNone(app.session_state["chat_context"])
        self.assertEqual(len(app.session_state["chat_history"]), 10)

    def test_chat_stock_and_provider_failure_keep_app_usable(self):
        from finance_agent.api import ServiceError
        app = self.start()
        app.text_area[0].set_value("Stock price AAPL")
        with patch("finance_agent.agent.fetch_stock_price", return_value={"ticker": "AAPL", "close": 150.0, "market_date": "2026-10-16"}) as fetch:
            self.button(app, "Run").click().run()
        self.assert_clean(app)
        fetch.assert_called_once_with("AAPL")
        self.assertTrue(any("2026-10-16" in caption.value for caption in app.caption))
        app.text_area[0].set_value("What about MSFT?")
        with patch("finance_agent.agent.fetch_stock_price", side_effect=ServiceError("Price unavailable")):
            self.button(app, "Run").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("Price unavailable" in error.value for error in app.error))
        self.navigate(app, "Transactions")
        self.assertTrue(any(button.label == "Save Transaction" for button in app.button))

    def test_chat_ai_failure_keeps_summary_and_context_is_sent(self):
        from dataclasses import replace
        from finance_agent.api import ServiceError
        self.settings = replace(self.settings, api_key="test-only")
        app = self.start()
        app.text_area[0].set_value("Summary 2026-01 in EGP")
        self.button(app, "Run").click().run()
        app.text_area[0].set_value("Summarize 2026-01 and suggest a plan")
        with patch("finance_agent.api.OpenRouterClient.answer", side_effect=ServiceError("AI timed out")) as answer:
            self.button(app, "Run").click().run()
        self.assert_clean(app)
        self.assertTrue(any("AI timed out" in warning.value for warning in app.warning))
        # History is passed as a snapshot; the current turn must not enter itself.
        self.assertEqual(len(answer.call_args.kwargs["history"]), 1)
        self.assertEqual(answer.call_args.kwargs["history"][0]["question"], "Summary 2026-01 in EGP")
        self.assertTrue(any("net" in frame.value.columns for frame in app.dataframe))
        self.assertEqual(len(FinanceRepository(self.settings.db_path).list_transactions()), 3)

    def test_overview_currency_cards_and_filters(self):
        app = self.start("Overview")
        self.assert_clean(app)
        values = [(item.label, item.value) for item in app.metric]
        self.assertIn(("Net balance", "900.00"), values)
        self.assertIn(("Net balance", "-5.00"), values)
        next(item for item in app.selectbox if item.label == "Currency filter").set_value("USD").run()
        self.assertEqual(len(app.metric), 4)
        self.assertTrue(any(item.label == "Savings rate" and item.value == "—" for item in app.metric))
        self.input(app, "Month (YYYY-MM, blank for all)").set_value("2030-01").run()
        self.assert_clean(app)
        self.assertEqual(len(app.metric), 0)
        self.assertTrue(any("No transactions match" in info.value for info in app.info))

    def test_sections_are_lazy_and_one_snapshot_is_read_per_refresh(self):
        original_snapshot = FinanceRepository.snapshot
        original_migrate = FinanceRepository.migrate_json
        with patch.object(FinanceRepository, "snapshot", autospec=True, side_effect=original_snapshot) as snapshot, \
             patch.object(FinanceRepository, "migrate_json", autospec=True, side_effect=original_migrate) as migrate, \
             patch("finance_agent.ui.export_csv", wraps=export_csv) as export, \
             patch.object(FinanceRepository, "list_transactions", side_effect=AssertionError("Redundant read")), \
             patch.object(FinanceRepository, "list_goals", side_effect=AssertionError("Redundant read")):
            app = self.start("Overview")
            self.assert_clean(app)
            export.assert_not_called()
            self.assertFalse(any(button.label == "Save Transaction" for button in app.button))
            count = snapshot.call_count
            app.run()
            self.assertEqual(snapshot.call_count, count + 1)
            self.assertEqual(migrate.call_count, 1)
            self.navigate(app, "Transactions")
            self.assert_clean(app)
            self.assertEqual(export.call_count, 2)
            self.navigate(app, "Goals")
            self.assert_clean(app)
            self.assertEqual(export.call_count, 2)

    def test_transaction_search_pagination_and_page_reset(self):
        app = self.start("Transactions")
        repo = FinanceRepository(self.settings.db_path)
        for i in range(55):
            repo.add_transaction({"date": "2026-01-03", "amount": -1, "currency": "EGP",
                                  "merchant": f"Test [{i}]", "description": "Page test"})
        self.button(app, "Refresh data").click().run()
        page = next(item for item in app.number_input if item.label == "Page")
        page.set_value(3).run()
        frames = [frame.value for frame in app.dataframe if "merchant" in frame.value.columns]
        self.assertEqual(len(frames[0]), 8)
        self.input(app, "Search transactions").set_value("Test [4]").run()
        self.assert_clean(app)
        self.assertEqual(next(item for item in app.number_input if item.label == "Page").value, 1)
        frame = next(frame.value for frame in app.dataframe if "merchant" in frame.value.columns)
        self.assertEqual(frame.iloc[0]["merchant"], "Test [4]")
        self.assertEqual(len(frame), 1)

    def test_tools_keep_last_result_across_navigation_without_refetching(self):
        app = self.start("Tools")
        with patch("finance_agent.ui.convert_currency", return_value={"converted": 5000, "rate": 50, "source": "offline fallback", "note": "Estimate only; saved rates may be outdated."}) as convert, \
             patch("finance_agent.ui.fetch_stock_price", return_value={"ticker": "AAPL", "close": 100, "market_date": "2026-10-16"}) as stock:
            self.button(app, "Convert").click().run()
            self.button(app, "Fetch").click().run()
            self.navigate(app, "Overview")
            self.navigate(app, "Tools")
            self.assert_clean(app)
            convert.assert_called_once()
            stock.assert_called_once()
            self.assertTrue(any("5,000.00 EGP" in item.value for item in app.success))
            self.assertTrue(any("2026-10-16" in item.value for item in app.caption))

    def test_external_database_changes_are_visible_on_refresh(self):
        app = self.start("Overview")
        repo = FinanceRepository(self.settings.db_path)
        repo.update_transaction("two", {"amount": -250})
        self.button(app, "Refresh data").click().run()
        self.assert_clean(app)
        self.assertTrue(any(item.label == "Net balance" and item.value == "750.00" for item in app.metric))

    def test_goal_rename_keeps_selection_for_following_delete(self):
        app = self.start("Goals")
        goal = FinanceRepository(self.settings.db_path).list_goals()[1]
        next(item for item in app.selectbox if item.label == "Goal to manage").set_value(goal["id"]).run()
        self.input(app, "Goal name").set_value("Renamed laptop")
        self.button(app, "Save goal").click().run()
        self.assert_clean(app)
        self.assertEqual(next(item for item in app.selectbox if item.label == "Goal to manage").value, goal["id"])

    def test_stock_form_uses_price_client_and_clears_old_result_after_failure(self):
        from unittest.mock import Mock
        from tests.test_stocks import chart_payload
        session = Mock()
        session.get.return_value.ok = True
        payload = chart_payload()
        payload["chart"]["result"][0]["meta"]["currency"] = "GBp"
        session.get.return_value.json.return_value = payload
        app = self.start("Tools")
        with patch("finance_agent.api.requests.Session", return_value=session):
            self.button(app, "Fetch").click().run()
            self.assert_clean(app)
            self.assertTrue(any("123.45 GBp (pence)" in item.value for item in app.success))
            self.assertTrue(any("Source: Yahoo Finance" in item.value for item in app.caption))
            self.assertTrue(any("2026-01-05" in item.value for item in app.caption))
            session.get.return_value.ok = False
            session.get.return_value.status_code = 429
            self.button(app, "Fetch").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("rate limiting" in item.value for item in app.error))
        self.assertFalse(any("latest daily close" in item.value for item in app.success))
        self.assertNotIn("stock_result", app.session_state)

    def test_stock_chat_displays_quote_unit_and_source(self):
        app = self.start("Assistant")
        app.text_area[0].set_value("Stock price AAPL")
        with patch("finance_agent.agent.fetch_stock_price", return_value={"ticker": "AAPL", "close": 123.45,
                   "currency": "USD", "market_date": "2026-10-16", "source": "Yahoo Finance",
                   "retrieved_at": "2026-10-19T18:00:00+00:00"}):
            self.button(app, "Run").click().run()
        self.assert_clean(app)
        self.assertTrue(any("123.45 USD" in item.value for item in app.markdown))
        self.assertTrue(any("Source: Yahoo Finance" in item.value for item in app.caption))


if __name__ == "__main__":
    unittest.main()
