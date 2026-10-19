"""SQLite persistence, one-time JSON migration and validated CSV exchange."""
from contextlib import contextmanager
import csv
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .storage import (DataError, load_goals, load_transactions, money,
                      validate_goals, validate_transaction)

CSV_COLUMNS = ["id", "date", "merchant", "amount", "currency", "description", "category"]
MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 10000


def cents(value):
    return int(money(value) * 100)


def fingerprint(tx):
    """Ignore formatting differences when suggesting duplicate purchases."""
    normalize = lambda value: " ".join(str(value).casefold().split())
    fields = [tx["date"], cents(tx["amount"]), tx["currency"],
              normalize(tx["merchant"]), normalize(tx["description"])]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()


def parse_csv(content):
    if isinstance(content, str):
        content = content.encode("utf-8")
    if len(content) > MAX_CSV_BYTES:
        raise DataError("CSV must be no larger than 2 MB.")
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = [str(name).strip().lower() for name in (reader.fieldnames or [])]
        if len(headers) != len(set(headers)):
            raise DataError("CSV has duplicate column names.")
        if not {"date", "amount", "currency"}.issubset(headers):
            raise DataError("CSV needs date, amount and currency columns.")
        reader.fieldnames = headers
        rows = []
        for row in reader:
            line = reader.line_num
            if len(rows) >= MAX_CSV_ROWS:
                raise DataError("CSV supports at most 10,000 transactions.")
            if None in row or any(value is None for value in row.values()):
                raise DataError(f"CSV line {line}: column count does not match the header.")
            values = {key: value.strip() for key, value in row.items() if key in CSV_COLUMNS}
            try:
                rows.append(validate_transaction(values))
            except DataError as exc:
                raise DataError(f"CSV line {line}: {exc}") from exc
        if not rows:
            raise DataError("CSV contains no transactions.")
        return rows
    except (UnicodeError, csv.Error) as exc:
        raise DataError("CSV must be valid UTF-8 with comma-separated columns.") from exc


def export_csv(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, "amount": f"{money(row['amount']):.2f}"})
    return stream.getvalue().encode("utf-8-sig")


class FinanceRepository:
    def __init__(self, path):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DataError("Cannot create the database folder. Check its location and permissions.") from exc
        with self.connection(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise DataError("This database uses an unsupported schema version.")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY, date TEXT NOT NULL, merchant TEXT NOT NULL,
                amount_cents INTEGER NOT NULL, currency TEXT NOT NULL,
                description TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS tx_date_currency ON transactions(date, currency)")
            db.execute("CREATE INDEX IF NOT EXISTS tx_fingerprint ON transactions(fingerprint)")
            db.execute("""CREATE TABLE IF NOT EXISTS goals (
                id TEXT PRIMARY KEY, position INTEGER NOT NULL, name TEXT NOT NULL,
                currency TEXT NOT NULL, target_cents INTEGER NOT NULL CHECK(target_cents > 0),
                current_cents INTEGER NOT NULL CHECK(current_cents >= 0),
                deadline TEXT NOT NULL, payload TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS legacy_sources (
                name TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL)""")
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def connection(self, write=False):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            db.row_factory = sqlite3.Row
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except sqlite3.Error as exc:
            if db is not None and db.in_transaction:
                db.rollback()
            raise DataError("Cannot read or update the finance database. Check its file, permissions and available disk space.") from exc
        except Exception:
            if db is not None and db.in_transaction:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _insert_tx(db, tx):
        db.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", (
            tx["id"], tx["date"], tx["merchant"], cents(tx["amount"]), tx["currency"],
            tx["description"], fingerprint(tx), json.dumps(tx, ensure_ascii=False, allow_nan=False)))

    @staticmethod
    def _tx(row):
        return {**json.loads(row["payload"]), "id": row["id"], "date": row["date"],
                "merchant": row["merchant"], "amount": row["amount_cents"] / 100,
                "currency": row["currency"], "description": row["description"]}

    @staticmethod
    def _goal(row):
        return {**json.loads(row["payload"]), "id": row["id"], "name": row["name"],
                "currency": row["currency"], "target_amount": row["target_cents"] / 100,
                "current_amount": row["current_cents"] / 100, "deadline": row["deadline"]}

    @staticmethod
    def _insert_goal(db, goal, position):
        db.execute("INSERT INTO goals VALUES (?,?,?,?,?,?,?,?)", (
            goal["id"], position, goal["name"], goal["currency"], cents(goal["target_amount"]),
            cents(goal["current_amount"]), goal.get("deadline", ""),
            json.dumps(goal, ensure_ascii=False, allow_nan=False)))

    def migrate_json(self, transactions_path, goals_path):
        """Import once, in one transaction, retaining the source files and snapshots."""
        with self.connection(write=True) as db:
            if db.execute("SELECT value FROM metadata WHERE key='json_migrated'").fetchone():
                return False
            if db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] or db.execute("SELECT COUNT(*) FROM goals").fetchone()[0]:
                raise DataError("Database already contains data without a migration record; automatic JSON migration was stopped.")
            transactions = load_transactions(transactions_path)
            goals = load_goals(goals_path)["goals"]
            for tx in transactions:
                tx["id"] = tx["id"] or f"tx_{uuid4().hex}"
                self._insert_tx(db, tx)
            for position, goal in enumerate(goals):
                goal["id"] = str(goal.get("id") or f"goal_{uuid4().hex}")
                self._insert_goal(db, goal, position)
            for name, path in [("transactions", transactions_path), ("goals", goals_path)]:
                path = Path(path)
                if path.exists():
                    raw = path.read_bytes()
                    db.execute("INSERT INTO legacy_sources VALUES (?,?,?,?)", (
                        name, str(path.resolve()), hashlib.sha256(raw).hexdigest(), raw))
            db.execute("INSERT INTO metadata VALUES ('json_migrated','1')")
            return True

    def list_transactions(self):
        with self.connection() as db:
            return [self._tx(row) for row in db.execute("SELECT * FROM transactions ORDER BY rowid")]

    def snapshot(self):
        """Read transactions and goals from one consistent SQLite snapshot."""
        with self.connection() as db:
            db.execute("BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise DataError("This database uses an unsupported schema version.")
            transactions = [self._tx(row) for row in db.execute("SELECT * FROM transactions ORDER BY rowid")]
            goals = [self._goal(row) for row in db.execute("SELECT * FROM goals ORDER BY position, rowid")]
            return transactions, goals

    def add_transaction(self, transaction, allow_duplicate=False):
        tx = validate_transaction(transaction)
        tx["id"] = tx["id"] or f"tx_{uuid4().hex}"
        with self.connection(write=True) as db:
            if db.execute("SELECT 1 FROM transactions WHERE id=?", (tx["id"],)).fetchone():
                raise DataError("A transaction with this ID already exists.")
            if not allow_duplicate and db.execute("SELECT 1 FROM transactions WHERE fingerprint=?", (fingerprint(tx),)).fetchone():
                raise DataError("A matching transaction already exists. Confirm that this is a separate purchase to save it.")
            self._insert_tx(db, tx)
        return tx

    def update_transaction(self, transaction_id, changes):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            if row is None:
                raise DataError("Transaction no longer exists. Refresh the list.")
            tx = validate_transaction({**self._tx(row), **changes, "id": transaction_id})
            db.execute("""UPDATE transactions SET date=?,merchant=?,amount_cents=?,currency=?,
                          description=?,fingerprint=?,payload=? WHERE id=?""", (
                tx["date"], tx["merchant"], cents(tx["amount"]), tx["currency"], tx["description"],
                fingerprint(tx), json.dumps(tx, ensure_ascii=False, allow_nan=False), transaction_id))
        return tx

    def delete_transaction(self, transaction_id):
        with self.connection(write=True) as db:
            if db.execute("DELETE FROM transactions WHERE id=?", (transaction_id,)).rowcount != 1:
                raise DataError("Transaction no longer exists. Refresh the list.")

    @staticmethod
    def _plan_import(db, rows, skip_matching):
        existing = [FinanceRepository._tx(row) for row in db.execute("SELECT * FROM transactions")]
        ids = {row["id"]: fingerprint(row) for row in existing}
        fingerprints = {fingerprint(row) for row in existing}
        accepted, duplicates = [], 0
        for index, row in enumerate(rows, 2):
            row = dict(row)
            digest = fingerprint(row)
            if row["id"] and row["id"] in ids:
                if ids[row["id"]] != digest:
                    raise DataError(f"CSV record {index}: ID already belongs to a different transaction. No rows were imported.")
                duplicates += 1
                continue
            if skip_matching and digest in fingerprints:
                duplicates += 1
                continue
            row["id"] = row["id"] or f"tx_{uuid4().hex}"
            ids[row["id"]] = digest
            fingerprints.add(digest)
            accepted.append(row)
        return accepted, duplicates

    def preview_csv(self, content, skip_matching=True):
        rows = parse_csv(content)
        with self.connection() as db:
            accepted, duplicates = self._plan_import(db, rows, skip_matching)
        return {"rows": rows, "new": len(accepted), "duplicates": duplicates}

    def import_csv(self, content, skip_matching=True):
        rows = parse_csv(content)
        with self.connection(write=True) as db:
            accepted, duplicates = self._plan_import(db, rows, skip_matching)
            for row in accepted:
                self._insert_tx(db, row)
        return {"imported": len(accepted), "duplicates": duplicates}

    def list_goals(self):
        with self.connection() as db:
            return [self._goal(row) for row in db.execute("SELECT * FROM goals ORDER BY position,rowid")]

    def add_goal(self, goal):
        validated = validate_goals({"goals": [goal]})["goals"][0]
        validated["id"] = f"goal_{uuid4().hex}"
        with self.connection(write=True) as db:
            position = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM goals").fetchone()[0]
            self._insert_goal(db, validated, position)
        return validated

    def update_goal(self, goal_id, changes):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
            if row is None:
                raise DataError("Goal no longer exists. Refresh the list.")
            goal = validate_goals({"goals": [{**self._goal(row), **changes, "id": goal_id}]})["goals"][0]
            db.execute("UPDATE goals SET name=?,currency=?,target_cents=?,current_cents=?,deadline=?,payload=? WHERE id=?", (
                goal["name"], goal["currency"], cents(goal["target_amount"]), cents(goal["current_amount"]),
                goal.get("deadline", ""), json.dumps(goal, ensure_ascii=False, allow_nan=False), goal_id))
        return goal

    def delete_goal(self, goal_id):
        with self.connection(write=True) as db:
            if db.execute("DELETE FROM goals WHERE id=?", (goal_id,)).rowcount != 1:
                raise DataError("Goal no longer exists. Refresh the list.")
