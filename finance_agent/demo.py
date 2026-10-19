"""Disposable, session-owned sample data for the public demo entry point."""
from dataclasses import replace
from collections import deque
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
import time

from .storage import atomic_write_json


def sample_data(today=None):
    today = today or date.today()
    first = today.replace(day=1)
    entries = [
        ("salary", 1, "Example Employer", "Monthly salary", 25000, "EGP", ""),
        ("rent", 2, "Example Landlord", "Monthly rent", -6500, "EGP", "Bills"),
        ("groceries", 3, "Example Market", "Weekly groceries", -1450, "EGP", "Food & Dining"),
        ("transport", 4, "Uber", "Commute", -220, "EGP", "Transport"),
        ("internet", 5, "Example Internet", "Home internet", -400, "EGP", "Bills"),
        ("health", 6, "Example Pharmacy", "Health essentials", -320, "EGP", "Health"),
        ("coffee", 7, "Example Cafe", "Coffee and lunch", -90, "EGP", "Food & Dining"),
        ("freelance", 1, "Example Client", "Freelance income", 600, "USD", ""),
        ("subscription", 5, "Example Streaming", "Monthly subscription", -15, "USD", "Subscriptions"),
        ("books", 8, "Example Bookshop", "Books", -35, "USD", "Shopping"),
    ]
    transactions = [{"id": ident, "date": first.replace(day=min(day, today.day)).isoformat(),
                     "merchant": merchant, "description": description, "amount": amount,
                     "currency": currency, "category": category}
                    for ident, day, merchant, description, amount, currency, category in entries]
    goals = {"goals": [
        {"name": "Emergency fund", "currency": "EGP", "target_amount": 30000,
         "current_amount": 9000, "deadline": (today + timedelta(days=180)).isoformat()},
        {"name": "New laptop", "currency": "USD", "target_amount": 1200,
         "current_amount": 300, "deadline": (today + timedelta(days=120)).isoformat()},
    ]}
    return transactions, goals


class DemoWorkspace:
    def __init__(self, today=None):
        self._directory = TemporaryDirectory(prefix="personal_finance_demo_")
        self.root = Path(self._directory.name)
        transactions, goals = sample_data(today)
        atomic_write_json(self.root / "transactions.json", transactions)
        atomic_write_json(self.root / "goals.json", goals)

    def settings(self, original, visitor_key=""):
        """Ignore private path overrides and require explicit shared-key opt-in."""
        return replace(original, root=self.root,
                       transactions_path=self.root / "transactions.json",
                       goals_path=self.root / "goals.json",
                       database_path=self.root / "finance.sqlite3", store_dir=self.root / "index",
                       embedding_backend="hash", allow_model_download=False,
                       api_key=visitor_key.strip() or (original.api_key if original.demo_shared_ai else ""))

    def close(self):
        self._directory.cleanup()


class DemoRequestLimit:
    """Thread-safe rolling limit; only counters are shared between visitors."""
    def __init__(self, max_calls=20, seconds=86400):
        self.max_calls, self.seconds = max_calls, seconds
        self._calls = deque()
        self._lock = Lock()

    def acquire(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            while self._calls and self._calls[0] <= now - self.seconds:
                self._calls.popleft()
            if len(self._calls) >= self.max_calls:
                return False
            self._calls.append(now)
            return True
