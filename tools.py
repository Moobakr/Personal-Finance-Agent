# tools.py

import math
from datetime import datetime, timedelta
from typing import Tuple, Dict

import pandas as pd
import requests
import yfinance as yf

# =========================
# TOOLS
# =========================

# 1) Budget Calculator
def calculate_budget(monthly_income: float, fixed_expenses: float, variable_expenses: float) -> Dict:
    total_expenses = fixed_expenses + variable_expenses
    savings = monthly_income - total_expenses
    savings_rate = (savings / monthly_income) * 100 if monthly_income > 0 else 0

    recommended_needs = monthly_income * 0.50
    recommended_wants = monthly_income * 0.30
    recommended_savings = monthly_income * 0.20

    return {
        "total_expenses": total_expenses,
        "savings": savings,
        "savings_rate": savings_rate,
        "recommended_needs": recommended_needs,
        "recommended_wants": recommended_wants,
        "recommended_savings": recommended_savings,
    }


# 2) Expense Categorizer
DEFAULT_CATEGORY_KEYWORDS = {
    "Housing": ["rent", "mortgage", "house", "apartment"],
    "Groceries": ["grocery", "supermarket", "food"],
    "Transport": ["bus", "taxi", "fuel", "uber", "train"],
    "Entertainment": ["netflix", "cinema", "movie", "game", "spotify"],
    "Utilities": ["electricity", "water", "gas", "internet", "phone"],
    "Dining Out": ["restaurant", "cafe", "coffee"],
}


def guess_category(description: str) -> str:
    desc_lower = str(description).lower()
    for cat, keywords in DEFAULT_CATEGORY_KEYWORDS.items():
        if any(k in desc_lower for k in keywords):
            return cat
    return "Other"


def categorize_expenses(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Expected columns:
      - amount (numeric)
      - category (optional)
      - description (optional)
    """
    df = df.copy()

    if "amount" not in df.columns:
        raise ValueError("CSV must have a column named 'amount'.")

    if "category" not in df.columns:
        # Try to infer categories from description
        if "description" in df.columns:
            df["category"] = df["description"].apply(guess_category)
        else:
            df["category"] = "Uncategorized"

    grouped = df.groupby("category")["amount"].sum().reset_index()
    grouped = grouped.sort_values("amount", ascending=False)
    return df, grouped


# 3) Currency Converter (API, but not AI)
def convert_currency(amount: float, from_code: str, to_code: str) -> float:
    url = f"https://open.er-api.com/v6/latest/{from_code.upper()}"
    response = requests.get(url).json()

    if response["result"] != "success":
        return "API Error: Could not fetch exchange rates."

    rate = response["rates"].get(to_code.upper())

    if not rate:
        return f"Currency {to_code} not found."

    return amount * rate


# 4) Stock Price Fetcher (yfinance)
def fetch_stock_price(symbol: str):
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period="1d")
    if hist.empty:
        return None
    last_row = hist.iloc[-1]
    return {
        "symbol": symbol.upper(),
        "date": last_row.name,
        "open": float(last_row["Open"]),
        "high": float(last_row["High"]),
        "low": float(last_row["Low"]),
        "close": float(last_row["Close"]),
    }


# 5) Savings Goal Tracker
def months_to_goal(
    current_savings: float,
    monthly_contribution: float,
    annual_return_pct: float,
    goal_amount: float,
) -> float:
    """
    Compound monthly:
        FV = current_savings * (1 + r)^n + monthly_contribution * [((1 + r)^n - 1) / r]
    Solve for n by brute-force.
    """
    r = (annual_return_pct / 100) / 12 if annual_return_pct > 0 else 0.0

    if monthly_contribution <= 0 and r == 0:
        return math.inf  # never reach

    max_months = 600  # 50 years

    for n in range(1, max_months + 1):
        if r == 0:
            value = current_savings + monthly_contribution * n
        else:
            value = current_savings * (1 + r) ** n + monthly_contribution * (
                ((1 + r) ** n - 1) / r
            )

        if value >= goal_amount:
            return n

    return math.inf


# =========================
# SIMPLE INTENT DETECTION FOR THE AGENT
# =========================

def detect_intent(msg: str) -> str:
    msg_l = msg.lower()

    if any(w in msg_l for w in ["convert", "exchange rate", "currency", "usd", "eur", "sar", "qar"]):
        return "currency"
    if any(w in msg_l for w in ["stock", "share", "ticker", "price of", "market"]):
        return "stock"
    if any(w in msg_l for w in ["budget", "spend", "expenses", "income"]):
        return "budget"
    if any(w in msg_l for w in ["save", "savings goal", "reach my goal", "goal amount"]):
        return "savings"
    # default: general advice
    return "advice"
