"""Public-demo isolation, credential boundaries, limits and source-only packaging."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from streamlit.testing.v1 import AppTest

from finance_agent.config import Settings, load_settings
from finance_agent.database import FinanceRepository
from finance_agent.demo import DemoRequestLimit, DemoWorkspace, sample_data
from finance_agent.storage import atomic_write_json
from scripts.release import build_release, release_files

ROOT = Path(__file__).resolve().parents[1]


class DemoUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_sample_dates_are_current_and_never_future(self):
        rows, goals = sample_data(date(2027, 2, 1))
        self.assertEqual(len(rows), 10)
        self.assertEqual({row["date"] for row in rows}, {"2027-02-01"})
        self.assertEqual(len(goals["goals"]), 2)

    def test_workspace_overrides_private_data_paths_and_key_use_is_explicit(self):
        original = Settings(root=self.root, api_key="owner-test-key", database_path=self.root / "private.sqlite3",
                            transactions_path=self.root / "private.json", goals_path=self.root / "private-goals.json")
        workspace = DemoWorkspace()
        self.addCleanup(workspace.close)
        isolated = workspace.settings(original)
        self.assertNotEqual(isolated.db_path, original.db_path)
        self.assertTrue(isolated.db_path.is_relative_to(workspace.root))
        self.assertTrue(isolated.transactions_path.is_relative_to(workspace.root))
        self.assertEqual(isolated.api_key, "")
        shared = workspace.settings(replace(original, demo_shared_ai=True))
        self.assertEqual(shared.api_key, "owner-test-key")
        self.assertEqual(workspace.settings(original, "visitor-test-key").api_key, "visitor-test-key")
        self.assertEqual(isolated.embedding_backend, "hash")
        self.assertFalse(isolated.allow_model_download)

    def test_host_secrets_and_environment_precedence(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = load_settings(self.root, host_secrets={"OPENROUTER_API_KEY": "cloud-key", "DEMO_SHARED_AI": True})
        self.assertEqual(settings.api_key, "cloud-key")
        self.assertTrue(settings.demo_shared_ai)
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "env-key"}, clear=True):
            self.assertEqual(load_settings(self.root, host_secrets={"OPENROUTER_API_KEY": "cloud-key"}).api_key, "env-key")

    def test_global_gate_is_atomic_and_expires(self):
        gate = DemoRequestLimit(20, 60)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: gate.acquire(now=100), range(100)))
        self.assertEqual(sum(results), 20)
        self.assertFalse(gate.acquire(now=159))
        self.assertTrue(gate.acquire(now=160))

    def test_demo_session_cooldown_and_attempt_limit(self):
        from finance_agent.ui import permit_demo_ai
        state = {}
        with patch("finance_agent.ui.st.session_state", state), patch("finance_agent.ui.time.monotonic", return_value=100) as clock:
            self.assertTrue(permit_demo_ai()[0])
            self.assertFalse(permit_demo_ai()[0])
            for step in range(1, 5):
                clock.return_value = 100 + step * 16
                self.assertTrue(permit_demo_ai()[0])
            clock.return_value = 1000
            self.assertFalse(permit_demo_ai()[0])

    def test_shared_gate_does_not_charge_session_when_global_limit_rejects(self):
        from finance_agent.ui import permit_demo_ai
        state = {}
        with patch("finance_agent.ui.st.session_state", state), patch("finance_agent.ui.shared_demo_ai_limit") as limit:
            limit.return_value.acquire.return_value = False
            allowed, reason = permit_demo_ai(shared=True)
        self.assertFalse(allowed)
        self.assertIn("shared demo AI allowance", reason)
        self.assertEqual(state["demo_ai_budget"]["count"], 0)

    def test_release_excludes_private_records_and_credentials(self):
        for name in ("app.py", "streamlit_app.py", "requirements.txt", "README.md", "DEPLOYMENT.md"):
            (self.root / name).write_text("# public source", encoding="utf-8")
        (self.root / ".streamlit").mkdir()
        (self.root / ".streamlit/secrets.toml").write_text('OPENROUTER_API_KEY="private-key"', encoding="utf-8")
        (self.root / "data").mkdir()
        (self.root / "data/transactions.json").write_text("private records", encoding="utf-8")
        (self.root / "data/finance.sqlite3").write_bytes(b"private database")
        (self.root / "data/fx_fallback.json").write_text('{"rates": {}}', encoding="utf-8")
        output = self.root / "release.zip"
        build_release(self.root, output)
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            self.assertTrue(any(name.endswith("fx_fallback.json") for name in names))
            self.assertFalse(any(name.endswith("secrets.toml") or name.endswith("transactions.json") or ".sqlite3" in name for name in names))

    def test_release_rejects_embedded_credential_without_echoing_it(self):
        credential = "sk-or-v1-" + "a" * 64
        (self.root / "app.py").write_text(credential, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Potential embedded credential") as caught:
            release_files(self.root)
        self.assertNotIn(credential, str(caught.exception))


class PublicUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(root=root, api_key="owner-test-key", demo_shared_ai=True,
                                 transactions_path=root / "private.json", goals_path=root / "private-goals.json",
                                 knowledge_dir=root / "knowledge", store_dir=root / "private-index")
        self.settings.knowledge_dir.mkdir()
        (self.settings.knowledge_dir / "budget.md").write_text("Budget income savings", encoding="utf-8")
        self.settings.transactions_path.write_text("private invalid source that must never be migrated", encoding="utf-8")
        self.settings.goals_path.write_text("private goals", encoding="utf-8")
        config = patch("finance_agent.ui.load_settings", side_effect=lambda **kwargs: self.settings)
        config.start()
        self.addCleanup(config.stop)
        secrets = patch("finance_agent.ui.st.secrets.to_dict", return_value={})
        secrets.start()
        self.addCleanup(secrets.stop)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected live request"))
        network.start()
        self.addCleanup(network.stop)
        from finance_agent.ui import shared_demo_ai_limit
        shared_demo_ai_limit.clear()
        self.workspaces = []
        self.addCleanup(lambda: [workspace.close() for workspace in self.workspaces])

    def start(self):
        app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30).run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertFalse(app.error, str([error.value for error in app.error]))
        self.workspaces.append(app.session_state["demo_workspace"])
        return app

    def navigate(self, app, section):
        next(item for item in app.radio if item.label == "Section").set_value(section).run()

    def button(self, app, label):
        return next(item for item in app.button if item.label == label)

    def repo(self, app):
        return FinanceRepository(app.session_state["demo_workspace"].root / "finance.sqlite3")

    def test_two_visitors_have_separate_edits_and_private_sources_are_untouched(self):
        first, second = self.start(), self.start()
        self.assertNotEqual(self.repo(first).path, self.repo(second).path)
        self.navigate(first, "Transactions")
        next(item for item in first.text_input if item.label == "Merchant").set_value("Visitor One Only")
        self.button(first, "Save Transaction").click().run()
        self.assertFalse(first.exception)
        self.button(second, "Refresh data").click().run()
        self.assertEqual(len(self.repo(first).list_transactions()), 11)
        self.assertEqual(len(self.repo(second).list_transactions()), 10)
        self.assertFalse(self.settings.db_path.exists())
        self.assertEqual(self.settings.transactions_path.read_text(), "private invalid source that must never be migrated")

    def test_reset_clears_data_and_history_but_preserves_ai_attempt_counter(self):
        app = self.start()
        old_root = app.session_state["demo_workspace"].root
        self.repo(app).delete_transaction("salary")
        app.session_state["chat_history"] = [{"question": "q"}]
        app.session_state["demo_ai_budget"] = {"count": 3, "last": None}
        self.button(app, "Reset demo data").click().run()
        self.assertFalse(app.exception)
        self.workspaces.append(app.session_state["demo_workspace"])
        self.assertFalse(old_root.exists())
        self.assertEqual(len(self.repo(app).list_transactions()), 10)
        self.assertEqual(app.session_state["demo_ai_budget"]["count"], 3)
        self.assertNotIn("chat_history", app.session_state)

    def test_shared_ai_uses_owner_key_but_it_never_enters_prompt_or_exports(self):
        app = self.start()
        self.navigate(app, "Assistant")
        with patch("finance_agent.api.OpenRouterClient.answer", return_value="Verified sample advice") as answer:
            self.button(app, "Run").click().run()
            self.assertFalse(app.exception)
            answer.assert_called_once()
            self.button(app, "Run").click().run()
            answer.assert_called_once()
        self.assertTrue(any("15 seconds" in warning.value for warning in app.warning))
        self.assertNotIn("owner-test-key", str(answer.call_args))
        self.assertNotIn("owner-test-key", str(self.repo(app).list_transactions()))

    def test_public_demo_without_shared_key_still_runs_local_tools(self):
        self.settings = replace(self.settings, demo_shared_ai=False)
        app = self.start()
        self.navigate(app, "Assistant")
        app.text_area[0].set_value("Summary this month")
        self.button(app, "Run").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("net" in frame.value.columns for frame in app.dataframe))
        app.text_area[0].set_value("Suggest a savings plan")
        self.button(app, "Run").click().run()
        self.assertTrue(any("Demo AI" in caption.value for caption in app.caption))


if __name__ == "__main__":
    unittest.main()
