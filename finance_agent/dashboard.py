"""Small dashboard calculations with no Streamlit or persistence side effects."""
from collections import defaultdict
from decimal import Decimal

from .storage import money


def overview_totals(frame):
    totals = defaultdict(lambda: {"income": Decimal(0), "expenses": Decimal(0), "count": 0})
    for row in frame.to_dict("records"):
        entry = totals[row["currency"]]
        amount = money(row["amount"])
        entry["income" if amount > 0 else "expenses"] += abs(amount)
        entry["count"] += 1
    return [{"currency": code, "income": float(values["income"]),
             "expenses": float(values["expenses"]),
             "net": float(values["income"] - values["expenses"]), "count": values["count"],
             "savings_rate": float((values["income"] - values["expenses"]) / values["income"] * 100)
             if values["income"] else None}
            for code, values in sorted(totals.items())]


def search_transactions(frame, query):
    """Literal, case-insensitive search over labels, without interpreting regex."""
    query = query.strip()
    if not query:
        return frame
    labels = frame[["merchant", "description", "category"]].fillna("").astype(str)
    match = labels.apply(lambda col: col.str.contains(query, case=False, regex=False)).any(axis=1)
    return frame[match]
