"""Deterministic financial calculations; every total is separated by currency."""
from collections import defaultdict
from datetime import date
from decimal import Decimal
import json
import re

import pandas as pd

from .storage import DataError, currency_code, money

CATEGORY_RULES = {
    "Food & Dining": [r"\bkfc\b", r"\brestaurant\b", r"\bfood\b", r"\bpizza\b", r"\bdinner\b", r"\blunch\b", r"\bdelivery\b"],
    "Bills": [r"\bvodafone\b", r"\borange\b", r"\betisalat\b", r"\binternet\b", r"\belectric", r"\bwater\b", r"\brent\b", r"\bgas\b"],
    "Transport": [r"\buber\b", r"\bcareem\b", r"\bfuel\b", r"\bmetro\b", r"\btaxi\b"],
    "Shopping": [r"\bamazon\b", r"\bjumia\b", r"\bmall\b", r"\bstore\b", r"\bclothes\b", r"\bshoes\b"],
    "Health": [r"\bpharmacy\b", r"\bclinic\b", r"\bhospital\b", r"\bdoctor\b", r"\bmed\b"],
    "Subscriptions": [r"\bnetflix\b", r"\bspotify\b", r"\byoutube\b", r"\bsubscription\b", r"\bprime\b"],
}
TX_COLUMNS = ["id", "date", "merchant", "amount", "currency", "description", "month", "category"]


def categorize(merchant, description, amount):
    if money(amount) > 0:
        return "Income"
    text = f"{merchant} {description}".lower()
    for category, patterns in CATEGORY_RULES.items():
        if any(re.search(pattern, text) for pattern in patterns):
            return category
    return "Uncategorized"


def transactions_frame(transactions):
    rows = []
    for tx in transactions:
        category = str(tx.get("category") or "").strip() or categorize(tx["merchant"], tx["description"], tx["amount"])
        rows.append({**tx, "month": tx["date"][:7],
                     "category": "Income" if money(tx["amount"]) > 0 else category})
    return pd.DataFrame(rows, columns=TX_COLUMNS)


def selected_transactions(df, month=None, currency=None):
    if month:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}", month):
                raise ValueError()
            date.fromisoformat(month + "-01")
        except ValueError as exc:
            raise DataError("Month must be valid and use YYYY-MM, for example 2026-01.") from exc
    if currency:
        currency = currency_code(currency)
    result = df.copy()
    if month:
        result = result[result["month"] == month]
    if currency:
        result = result[result["currency"] == currency]
    return result


def monthly_summary(df, month=None, currency=None):
    totals = defaultdict(lambda: {"income": Decimal(0), "expenses": Decimal(0)})
    for row in selected_transactions(df, month, currency).to_dict("records"):
        entry = totals[(row["month"], row["currency"])]
        value = money(row["amount"])
        entry["income" if value > 0 else "expenses"] += abs(value)
    rows = [{"month": period, "currency": code,
             "income": float(total["income"]), "expenses": float(total["expenses"]),
             "net": float(total["income"] - total["expenses"])}
            for (period, code), total in sorted(totals.items())]
    return pd.DataFrame(rows, columns=["month", "currency", "income", "expenses", "net"])


def category_breakdown(df, month=None, currency=None):
    totals = defaultdict(Decimal)
    for row in selected_transactions(df, month, currency).to_dict("records"):
        amount = money(row["amount"])
        if amount < 0:
            totals[(row["currency"], row["category"])] -= amount
    rows = [{"currency": code, "category": category, "total": float(total)}
            for (code, category), total in sorted(totals.items(), key=lambda pair: (pair[0][0], -pair[1]))]
    return pd.DataFrame(rows, columns=["currency", "category", "total"])


def budget_plan(income):
    value = money(income, "Income", nonnegative=True)
    needs = (value * Decimal("0.50")).quantize(Decimal("0.01"))
    wants = (value * Decimal("0.30")).quantize(Decimal("0.01"))
    savings = value - needs - wants
    return {"Needs (50%)": float(needs), "Wants (30%)": float(wants),
            "Savings/Debt (20%)": float(savings)}


def goal_status(goal):
    target = money(goal["target_amount"], nonnegative=True)
    current = money(goal["current_amount"], nonnegative=True)
    deadline = goal.get("deadline")
    return {"name": goal["name"], "currency": goal["currency"],
            "current": float(current), "target": float(target),
            "progress_pct": round(float(current / target * 100), 2) if target else 0,
            "remaining": float(max(Decimal(0), target - current)),
            "deadline": deadline or "", "days_left":
            (date.fromisoformat(deadline) - date.today()).days if deadline else None}


def summarize_user_data(df, month=None, currency=None):
    selected = selected_transactions(df, month, currency)
    if selected.empty:
        return "No transactions match the selected period and currency."
    # Aggregate ALL matching rows, including those older than the UI's recent list.
    totals = defaultdict(lambda: {"income": Decimal(0), "expenses": Decimal(0), "count": 0})
    for row in selected.to_dict("records"):
        total = totals[row["currency"]]
        amount = money(row["amount"])
        total["income" if amount > 0 else "expenses"] += abs(amount)
        total["count"] += 1
    payload = {"period": month or "all available dates", "transaction_count": len(selected),
               "start_date": str(selected.date.min()), "end_date": str(selected.date.max()),
               "totals_by_currency": [
                   {"currency": code, "income": str(total["income"]),
                    "expenses": str(total["expenses"]),
                    "net": str(total["income"] - total["expenses"]), "count": total["count"]}
                   for code, total in sorted(totals.items())],
               "expense_categories": category_breakdown(selected).to_dict("records")}
    return json.dumps(payload, ensure_ascii=False)
