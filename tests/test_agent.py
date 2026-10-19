"""Chat tool execution, scope, context and API boundaries with no live calls."""
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from finance_agent.agent import Clarification, parse_fx, parse_stock, question_scope, run_question
from finance_agent.api import OpenRouterClient, ServiceError
from finance_agent.config import Settings
from finance_agent.finance import transactions_frame
from finance_agent.storage import DataError


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(root=Path('.'))
        self.search = Mock()
        self.search.retrieve.return_value = {"hits": [{"id": "budget.md", "text": "Budget guidance"}], "warning": ""}
        self.df = transactions_frame([
            {"id": "1", "date": "2026-01-01", "merchant": "Private employer", "description": "", "amount": 1000, "currency": "EGP"},
            {"id": "2", "date": "2026-01-02", "merchant": "Uber", "description": "", "amount": -100, "currency": "EGP"},
            {"id": "3", "date": "2026-02-01", "merchant": "Private shop", "description": "", "amount": -20, "currency": "USD"},
        ])
        self.goals = [{"name": "Fund", "target_amount": 5000, "current_amount": 1000, "currency": "EGP"}]
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network"))
        network.start()
        self.addCleanup(network.stop)

    def run_query(self, question, **kwargs):
        return run_question(self.settings, self.search, question, self.df, self.goals, **kwargs)

    def test_explicit_fx_and_rate_parsing(self):
        self.assertEqual(parse_fx("Please convert 1,250.50 usd to egp?"),
                         {"amount": 1250.5, "from_code": "USD", "to_code": "EGP"})
        self.assertEqual(parse_fx("What is the exchange rate from EUR to USD?")["amount"], 1)
        for question in ["Convert $100 to EGP", "Convert USD to EGP", "Convert 1,00 USD to EGP",
                         "Convert 100 USD to EGP and EUR", "Convert NaN USD to EGP"]:
            with self.subTest(question=question), self.assertRaises(Clarification):
                parse_fx(question)
        for question in ["Convert -1 USD to EGP", "Convert 1.234 USD to EGP", "Convert 1000000000001 USD to EGP"]:
            with self.subTest(question=question), self.assertRaises(DataError):
                parse_fx(question)

    def test_fx_executes_without_ai_and_followup_reuses_codes(self):
        with patch("finance_agent.agent.convert_currency", return_value={"converted": 5000, "rate": 50, "source": "live API", "note": "updated"}) as convert:
            result = self.run_query("Convert 100 USD to EGP")
            convert.assert_called_once_with(amount=100.0, from_code="USD", to_code="EGP", fallback_path=self.settings.fx_path)
            self.assertIn("5,000.00 EGP", result["answer"])
            follow = self.run_query("And 50?", previous=result["context"])
            self.assertEqual(convert.call_args.kwargs["amount"], 50)
            self.assertEqual(follow["context"]["to_code"], "EGP")
        self.search.retrieve.assert_not_called()

    def test_missing_fx_parameters_never_call_provider(self):
        with patch("finance_agent.agent.convert_currency") as convert:
            response = self.run_query("Convert my money")
        self.assertTrue(response["clarification"])
        convert.assert_not_called()

    def test_fx_fallback_and_failure_are_explicit(self):
        with patch("finance_agent.agent.convert_currency", return_value={"converted": 5000, "rate": 50, "source": "offline fallback", "note": "old rates"}):
            response = self.run_query("100 USD in EGP")
        self.assertIn("outdated", response["warnings"][0])
        self.assertIn("offline fallback", response["notes"][0])
        with patch("finance_agent.agent.convert_currency", side_effect=ServiceError("No saved rate")):
            response = self.run_query("Convert 100 USD to EGP")
        self.assertEqual(response["error"], "No saved rate")
        self.assertIsNone(response["context"])

    def test_stock_parsing_and_execution(self):
        for question, expected in [("Stock price AAPL", "AAPL"), ("What is the price of BRK-B?", "BRK-B"),
                                   ("Price for ticker msft", "MSFT"), ("AAPL stock price", "AAPL")]:
            self.assertEqual(parse_stock(question), expected)
        for question in ["Stock price Apple", "Price of AAPL and MSFT", "Stock price", "Ticker AAPL; rm file"]:
            with self.subTest(question=question), self.assertRaises(Clarification):
                parse_stock(question)
        with patch("finance_agent.agent.fetch_stock_price", return_value={"ticker": "AAPL", "close": 123.45, "market_date": "2026-10-16"}) as fetch:
            response = self.run_query("Stock price AAPL")
            fetch.assert_called_once_with("AAPL")
            self.run_query("What about MSFT?", previous=response["context"])
            self.assertEqual(fetch.call_args.args, ("MSFT",))
        self.assertIn("2026-10-16", response["notes"][0])
        self.assertIn("not a real-time", response["notes"][0])
        self.search.retrieve.assert_not_called()

    def test_stock_provider_failure_is_displayed(self):
        with patch("finance_agent.agent.fetch_stock_price", side_effect=ServiceError("Yahoo Finance is temporarily unavailable")):
            response = self.run_query("Stock price AAPL")
        self.assertIn("Yahoo Finance", response["error"])

    def test_question_filters_override_sidebar(self):
        response = self.run_query("Summary 2026-02 in USD", month="2026-01", currency="EGP")
        rows = response["tables"][0]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["month"], rows[0]["currency"], rows[0]["net"]), ("2026-02", "USD", -20))
        self.assertEqual(question_scope("summary last month", self.df, today=date(2026, 1, 15))[0], "2025-12")
        self.assertEqual(question_scope("summary this month", self.df, today=date(2026, 1, 15))[0], "2026-01")
        self.assertEqual(question_scope("all dates all currencies", self.df, "2026-01", "EGP"), (None, None))

    def test_ambiguous_or_unsupported_periods_are_not_ignored(self):
        for question in ["Summary 2026-01 and 2026-02", "Summary January", "Summary this year",
                         "Summary 2026-01-15", "Summary USD and EGP", "Summary all dates 2026-01"]:
            with self.subTest(question=question):
                response = self.run_query(question)
                self.assertTrue(response["clarification"])
                self.assertEqual(response["tables"], [])
        self.assertTrue(self.run_query("Summary 2026-13")["error"])

    def test_scope_followups_and_no_matching_data(self):
        initial = self.run_query("Summary 2026-01 in EGP")
        response = self.run_query("What about 2026-02?", previous=initial["context"])
        self.assertEqual(response["context"]["currency"], "EGP")
        self.assertIn("No transactions", response["answer"])
        response = self.run_query("And USD?", previous=response["context"])
        self.assertEqual(response["tables"][0]["rows"][0]["expenses"], 20)
        self.assertTrue(self.run_query("What about transport spending?", previous=initial["context"])["clarification"])

    def test_goal_progress_and_category_advice_receive_tool_results(self):
        self.settings = replace(self.settings, api_key="test-only")
        with patch("finance_agent.api.OpenRouterClient.answer", return_value="Advice") as answer:
            result = self.run_query("Show goal progress and suggest a plan")
            goals = answer.call_args.kwargs["tool_results"]["goals"]
            self.assertEqual(goals[0]["remaining"], 4000)
            self.assertEqual(goals[0]["progress_pct"], 20)
            self.assertEqual(result["sources"][0]["id"], "budget.md")
            self.run_query("Categorize expenses and suggest a plan")
            self.assertIn("expense_categories", answer.call_args.kwargs["tool_results"])

    def test_advice_failure_preserves_calculated_results(self):
        self.settings = replace(self.settings, api_key="test-only")
        with patch("finance_agent.api.OpenRouterClient.answer", side_effect=ServiceError("rate limited")), patch("finance_agent.api.OpenRouterClient.close") as close:
            response = self.run_query("Summarize my month and suggest a plan")
        self.assertTrue(response["tables"][0]["rows"])
        self.assertIn("rate limited", response["warnings"][0])
        close.assert_called_once()

    def test_missing_key_skips_retrieval_and_keeps_summary(self):
        result = self.run_query("Summarize my month and suggest a plan")
        self.assertTrue(result["tables"][0]["rows"])
        self.assertTrue(any("OPENROUTER_API_KEY" in note for note in result["notes"]))
        self.search.retrieve.assert_not_called()

    def test_chat_mutations_are_not_executed(self):
        for question in ["Delete all transactions", "Update my goal to 100", "Import transactions"]:
            response = self.run_query(question)
            self.assertIn("forms", response["answer"])
            self.assertIsNone(response["context"])
        self.search.retrieve.assert_not_called()

    def test_saving_advice_is_not_mistaken_for_a_database_write(self):
        self.settings = replace(self.settings, api_key="test-only")
        with patch("finance_agent.api.OpenRouterClient.answer", return_value="Savings advice") as answer:
            result = self.run_query("How can I save more money?")
        answer.assert_called_once()
        self.assertEqual(result["answer"], "Savings advice")

    def test_api_context_is_bounded_and_does_not_include_raw_rows(self):
        session = Mock()
        session.post.return_value = Mock(ok=True)
        session.post.return_value.json.return_value = {"choices": [{"message": {"content": "Advice"}}]}
        history = [{"question": str(index) + "q" * 5000, "answer": "a" * 5000,
                    "response": {"internal": "not sent"}} for index in range(9)]
        client = OpenRouterClient("test-key", "model", session)
        client.answer("Help", "current totals", [], history=history, tool_results={"goals": [{"remaining": 4000}]})
        payload = session.post.call_args.kwargs["json"]
        user = json.loads(payload["messages"][1]["content"])
        self.assertEqual(len(user["recent_conversation"]), 4)
        self.assertTrue(user["recent_conversation"][0]["question"].startswith("5"))
        self.assertEqual(len(user["recent_conversation"][0]["question"]), 4000)
        self.assertNotIn("not sent", json.dumps(user))
        self.assertEqual(user["tool_results"]["goals"][0]["remaining"], 4000)
        self.assertEqual(len(payload["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
