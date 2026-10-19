"""Validation and legacy JSON helpers used by SQLite migration and demo seeding.

JSON writes replace whole files atomically; invalid existing data is never
silently replaced with an empty list.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4


class DataError(ValueError):
    """Data cannot be safely interpreted or stored."""


def money(value, label="Amount", *, nonnegative=False) -> Decimal:
    try:
        if isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or result.copy_abs() > Decimal("1000000000000"):
            raise ValueError()
        if nonnegative and result < 0:
            raise ValueError()
        if result != result.quantize(Decimal("0.01")):
            raise DataError(f"{label} supports at most two decimal places.")
        return result
    except (InvalidOperation, ValueError) as exc:
        if isinstance(exc, DataError):
            raise
        qualifier = "nonnegative " if nonnegative else ""
        raise DataError(f"{label} must be a finite {qualifier}number, at most 1 trillion.") from exc


def currency_code(value):
    result = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", result):
        raise DataError("Currency must be a three-letter code, such as EGP or USD.")
    return result


def iso_date(value, label="Date"):
    text = str(value)
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError()
        date.fromisoformat(text)
        return text
    except ValueError as exc:
        raise DataError(f"{label} must be a valid date in YYYY-MM-DD format.") from exc


def read_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataError(f"Cannot read {path.name}; repair the file before saving changes.") from exc


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_transaction(tx, index=None):
    prefix = f"Transaction {index}: " if index is not None else ""
    if not isinstance(tx, dict):
        raise DataError(prefix + "expected a JSON object.")
    try:
        if "date" not in tx or "amount" not in tx:
            raise DataError("date and amount are required.")
        return {
            **tx,
            "id": str(tx.get("id", "")).strip(),
            "date": iso_date(tx["date"]),
            "amount": float(money(tx["amount"])),
            "currency": currency_code(tx.get("currency", "USD")),
            "merchant": str(tx.get("merchant", "Unknown")).strip() or "Unknown",
            "description": str(tx.get("description", "")).strip(),
        }
    except DataError as exc:
        raise DataError(prefix + str(exc)) from exc


def load_transactions(path):
    raw = read_json(path, [])
    if not isinstance(raw, list):
        raise DataError("Transactions file must contain a JSON list.")
    rows = [validate_transaction(tx, i + 1) for i, tx in enumerate(raw)]
    ids = [tx["id"] for tx in rows if tx["id"]]
    if len(ids) != len(set(ids)):
        raise DataError("Transactions contain duplicate IDs; repair them before saving.")
    return rows


def append_transaction(path, tx):
    rows = load_transactions(path)
    row = validate_transaction(tx)
    row["id"] = row["id"] or f"tx_{uuid4().hex}"
    if any(old["id"] == row["id"] for old in rows):
        raise DataError("A transaction with this ID already exists.")
    atomic_write_json(path, rows + [row])
    return row


def validate_goals(data):
    if not isinstance(data, dict) or not isinstance(data.get("goals"), list):
        raise DataError("Goals file must contain a goals list.")
    goals = []
    for i, goal in enumerate(data["goals"], 1):
        if not isinstance(goal, dict) or not str(goal.get("name", "")).strip():
            raise DataError(f"Goal {i} needs a name.")
        target = money(goal.get("target_amount", 0), "Goal target", nonnegative=True)
        if target == 0:
            raise DataError("Goal target must be greater than zero.")
        current = money(goal.get("current_amount", 0), "Current savings", nonnegative=True)
        deadline = goal.get("deadline") or ""
        if deadline:
            deadline = iso_date(deadline, "Goal deadline")
        goals.append({**goal, "name": str(goal["name"]).strip(),
                      "currency": currency_code(goal.get("currency", "USD")),
                      "target_amount": float(target), "current_amount": float(current),
                      "deadline": deadline})
    return {**data, "goals": goals}


def load_goals(path):
    return validate_goals(read_json(path, {"goals": []}))


def save_goals(path, data):
    # Prevent an edit from silently replacing an existing unreadable file.
    load_goals(path)
    atomic_write_json(path, validate_goals(data))
